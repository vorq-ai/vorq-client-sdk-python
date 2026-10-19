"""The sealed batch surface: submit, read back, cancel.

The property every test here is really about is the same one that gated batches
in the first place — **nothing leaves this process in the clear**. The uploaded
file is a manifest of containers, `custom_id` is inside them, and the results come
back sealed and are read from the storage gateway by the name each row gives.
"""

import base64
import json
import os
from decimal import Decimal

import httpx
import pytest
from vorq._crypto import SealedBoxCipher, WalletSigner
from vorq._money import parse_usd
from vorq._results import JobError, TextResult
from vorq.errors import BatchFailed, EscrowKeyUnverified, ValidationError, VorqError, WaitTimeout

from .conftest import fake_cid, json_keys, json_response, make_client
from .test_client import BOX_PUBLIC, _open_complete, auth_router, escrow_verifier, quote_body

needs_wallet = pytest.mark.skipif(
    not os.environ.get("VORQ_WALLET_KEY") or not os.environ.get("VORQ_CIPHER_KEY"),
    reason="throwaway key material lives in .env.test",
)

MODEL = "deepseek-ai/deepseek-v4-pro:fp8"

# The ceilings every line of the shared fixture names.
PRICE = {"max_rate_in": "30", "max_rate_out": "90"}

REQUESTS = [
    {
        "custom_id": f"req-{i}",
        "method": "POST",
        "url": "/v1/responses",
        "body": {"model": MODEL, "input": f"haiku {i}", "max_output_tokens": 64, **PRICE},
    }
    for i in range(3)
]


def _sealing_client(handler, **kwargs):
    return make_client(handler, signer=WalletSigner(), cipher=SealedBoxCipher(), **kwargs)


def _uploaded_lines(content: bytes) -> list[dict]:
    """The JSONL rows, pulled back out of a captured multipart upload body."""
    return [
        json.loads(line)
        for line in content.decode("utf-8", "replace").splitlines()
        if line.strip().startswith("{")
    ]


def _open_line(line: dict) -> dict:
    """Open one uploaded line's container — the same reader the single-job tests use."""
    return _open_complete(line)


def batch_object(**over) -> dict:
    body = {
        "id": "batch_1",
        "object": "batch",
        "endpoint": "/v1/responses",
        "input_file_id": "file-in",
        "completion_window": "24h",
        "status": "completed",
        "errors": None,
        "output_file_id": "file-out",
        "error_file_id": None,
        "created_at": 1_800_000_000,
        "expires_at": 1_800_086_400,
        "in_progress_at": None,
        "finalizing_at": None,
        "completed_at": None,
        "failed_at": None,
        "expired_at": None,
        "cancelling_at": None,
        "cancelled_at": None,
        "request_counts": {"completed": 3, "failed": 0, "total": 3},
        "metadata": {},
        "vorq": {"sla": "24h"},
    }
    body.update(over)
    return body


def batch_router(state, *, batch=None, files=None, blobs=None, fee_bps: int | None = 100, gas_fee="0",
                 allocation=None):
    """Everything a batch submit and drain reads, captured into ``state``.

    ``allocation`` is what a plan names per entry — a list, or a function of the
    entry; unset, provider 1 — or the lowest id of the entry's ``providers`` —
    takes every line at 0.001/0.002.
    """
    files = files or {}
    blobs = blobs or {}

    def handler(request):
        path = request.url.path
        method = request.method
        if path == "/v1/models" or path.startswith("/v1/models/"):
            # No published schema: local param validation is skipped, which is the
            # documented behaviour and keeps these tests about the sealing.
            if path != "/v1/models":
                return json_response(404, {"error": {"type": "not_found_error", "message": "x"}})
        if path == "/v1/jobs" and method == "POST":
            body = json.loads(request.content)
            state.setdefault("quotes", []).append(body)
            answer = quote_body(body, amount="0.00021", gas_fee=gas_fee, fee_bps=fee_bps)
            state.setdefault("quotes_answered", []).append(answer["quote"])
            return json_response(402, answer)
        if path == "/v1/files" and method == "POST":
            state["upload"] = request.content
            state["upload_type"] = request.headers.get("content-type", "")
            return json_response(200, {"id": "file-in", "object": "file", "purpose": "batch"})
        if path == "/v1/batches" and method == "POST" and "input_file_id" not in json.loads(request.content):
            ask = json.loads(request.content)
            state.setdefault("plans", []).append(ask)
            return json_response(402, {"plan": [
                {"model_id": m["model_id"], "lines": m["lines"],
                 "allocation": allocation(m) if callable(allocation) else
                 allocation if allocation is not None else [
                     {"provider_id": min(m.get("providers", [1])), "box_key": BOX_PUBLIC, "rate_in": "0.001",
                      "rate_out": "0.002", "lines": m["lines"]}]}
                for m in ask["models"]]})
        if path == "/v1/batches" and method == "POST":
            state["create"] = json.loads(request.content)
            return json_response(200, batch or batch_object(status="validating"))
        if path.startswith("/v1/batches/") and path.endswith("/cancel"):
            state["cancelled"] = path
            return json_response(200, batch_object(status="cancelling"))
        if path.startswith("/v1/batches/"):
            state.setdefault("polls", 0)
            state["polls"] += 1
            return json_response(200, batch or batch_object())
        if path.endswith("/content"):
            state.setdefault("content_reads", []).append(str(request.url))
            return httpx.Response(200, text=files.get(path.split("/")[3], ""))
        if "/ipfs/" in path:
            return httpx.Response(200, content=blobs[path.rsplit("/", 1)[1]])
        raise AssertionError(f"unexpected {method} {path}")

    return auth_router(handler)


def _sealed_result(client, text: str) -> tuple[str, bytes]:
    """A settled result as a provider seals one, plus the name it is filed under."""
    body = {
        "id": "resp_1",
        "object": "response",
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    raw = json.dumps(body).encode()
    return fake_cid(raw), raw


def output_row(n: int, job_id: str, cid: str) -> str:
    return json.dumps({
        "id": f"batch_req_1_{n}",
        "custom_id": None,
        "response": {"status_code": 200, "request_id": job_id, "body": None},
        "error": None,
        "vorq": {"job_id": job_id, "result_cid": cid, "provider": 7,
                 "rate_in": "0.03", "rate_out": "0.09", "gas_fee": "0.03", "fee": "0",
                 "completion_tok": 20},
    })


def error_row(n: int, job_id: str | None, code: str) -> str:
    return json.dumps({
        "id": f"batch_req_1_{n}",
        "custom_id": None,
        "response": None,
        "error": {"code": code, "message": "it did not deliver"},
        "vorq": {"job_id": job_id, "line": n},
    })


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


@needs_wallet
class TestSubmit:
    async def test_every_line_is_sealed_before_the_file_leaves_the_process(self):
        """The one property batches were gated on.

        The uploaded file carries containers and routing terms. Asserting on keys
        rather than on a substring of the body is deliberate: a container is
        base64, and base64 of random bytes contains almost any short string often
        enough to make a substring test pass for the wrong reason.
        """
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.submit(REQUESTS, providers=[7])

        lines = _uploaded_lines(state["upload"])
        assert len(lines) == 3
        assert "multipart/form-data" in state["upload_type"]
        keys = json_keys(lines)
        # Flat, like the single-job form: nothing nested under a `vorq` or a
        # `payment` key.
        assert keys == {"url", "container"} | {
            "c", "owner", "job_id", "model_id", "sla_secs", "rate_in", "rate_out",
            "units_in", "units_out", "designated", "expires_at", "signature",
            "auth_sig", "amount",
        }
        # The prompt is inside the container and nowhere else.
        assert "input" not in keys
        assert "haiku 0" not in state["upload"].decode("utf-8", "replace")
        await client.aclose()

    async def test_a_line_past_the_inline_cap_is_still_written_inline(self):
        """There is no cap on a container's size any more, in a single job or in
        a batch line: past `INLINE_MAX_BYTES` a line still carries its container
        as inline base64 in the manifest — never refused, and never
        `container_cid`, which only a single job's own upload-first path uses."""
        from vorq._container import INLINE_MAX_BYTES

        state = {}
        client = _sealing_client(batch_router(state))
        big = [{**REQUESTS[0], "body": {**REQUESTS[0]["body"],
                                        "input": "x" * (INLINE_MAX_BYTES + 1024)}}]

        await client.batches.submit(big, providers=[7])

        lines = _uploaded_lines(state["upload"])
        assert len(lines) == 1
        assert "container_cid" not in lines[0]
        container = base64.b64decode(lines[0]["container"])
        assert len(container) > INLINE_MAX_BYTES
        await client.aclose()

    async def test_custom_id_rides_inside_the_container_and_never_beside_it(self):
        """A caller's own label is client-chosen text.

        On the line it would be readable by the coordinator and, once the manifest
        is pinned, by anyone who fetches that CID. Inside the container only the
        provider holding the line — and the client — ever see it.
        """
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.submit(REQUESTS, providers=[7])

        lines = _uploaded_lines(state["upload"])
        assert "custom_id" not in json_keys(lines)
        assert _open_line(lines[0])["custom_id"] == "req-0"
        assert _open_line(lines[2])["custom_id"] == "req-2"
        await client.aclose()

    async def test_the_whole_batch_is_priced_by_one_quote(self):
        """One `402`, not one per line — the reason a 50 000-line batch is possible.

        `cap` is arithmetic over terms this client signed, so the only thing the
        network has to answer is the chain's `gas_fee`, and that is one number for
        every line in the same block.
        """
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.submit(REQUESTS, providers=[7])

        assert len(state["quotes"]) == 1
        # The quote is asked about a real line's real order, not an invented one.
        assert state["quotes"][0]["job_id"] == (
            _uploaded_lines(state["upload"])[0]["job_id"]
        )
        assert "container" not in state["quotes"][0]
        await client.aclose()

    async def test_batch_authorizations_cover_the_protocol_fee(self):
        """The fee rides on top of the cap, floored, with the gas fee beside it."""
        state = {}
        client = _sealing_client(batch_router(state, gas_fee="0.000013", fee_bps=250))
        await client.batches.submit(REQUESTS, providers=[7])
        line = _uploaded_lines(state["upload"])[0]
        gas_fee = parse_usd(state["quotes_answered"][0]["gas_fee"], 6)
        # The bare cap, from the line's own signed terms, as JobRegistry computes it.
        scaled = (parse_usd(line["rate_in"], 6) * line["units_in"]
                  + parse_usd(line["rate_out"], 6) * line["units_out"])
        cap = max(1, -(-scaled // 1_000_000))
        assert cap * 250 % 10000  # this cap does not divide, so the floor is exercised
        assert parse_usd(line["amount"], 6) == cap + cap * 250 // 10000 + gas_fee
        await client.aclose()

    async def test_batch_refuses_a_quote_without_fee_bps(self):
        state = {}
        client = _sealing_client(batch_router(state, fee_bps=None))
        with pytest.raises(VorqError, match="carries no fee_bps"):
            await client.batches.submit(REQUESTS, providers=[7])
        await client.aclose()

    async def test_a_batch_list_is_planned_only_onto_its_providers(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=lambda entry: [
            {"provider_id": 3, "box_key": BOX_PUBLIC, "rate_in": "0.001", "rate_out": "0.002",
             "lines": 1},
            {"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": "0.002", "rate_out": "0.002",
             "lines": entry["lines"] - 1}]))
        await client.batches.submit(REQUESTS, providers=[7, 3])

        [plan] = state["plans"]
        # One entry, naming the allowed set.
        assert [(m["lines"], m["providers"]) for m in plan["models"]] == [(3, [3, 7])]
        rows = _uploaded_lines(state["upload"])
        assert [(r["designated"], r["rate_in"]) for r in rows] == [
            (3, "0.001"), (7, "0.002"), (7, "0.002"),
        ]
        await client.aclose()

    async def test_each_distinct_provider_record_is_read_once_per_submit(self):
        def submit_counting(lines):
            reads: list[str] = []
            router = batch_router(state := {})

            def counting(request):
                reads.append(request.url.path)
                return router(request)

            return _sealing_client(counting), reads, state

        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        lines = [{"body": {**body, "provider": pid}} for pid in (3, 7, 3, 7, 3)]
        client, reads, state = submit_counting(lines)
        await client.batches.submit(lines)
        assert sorted(p for p in reads if p.startswith("/evm/providers/")) == [
            "/evm/providers/3", "/evm/providers/7",
        ]
        assert [r["designated"] for r in _uploaded_lines(state["upload"])] == [3, 7, 3, 7, 3]
        # And the catalog reads do not grow with the file: the same for one line
        # as for five — the model id is resolved once and shared too.
        one, one_reads, _ = submit_counting(lines[:1])
        await one.batches.submit(lines[:1])
        assert reads.count("/v1/models") == one_reads.count("/v1/models")
        await client.aclose()
        await one.aclose()

    async def test_a_line_s_own_provider_overrides_the_batch_list(self):
        state = {}
        client = _sealing_client(batch_router(state))
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        lines = [{**REQUESTS[0], "body": body},
                 {**REQUESTS[1], "body": {**body, "provider": 3}}]
        await client.batches.submit(lines, providers=[7])

        [plan] = state["plans"]
        assert [(m["lines"], m["providers"]) for m in plan["models"]] == [(1, [7]), (1, [3])]
        rows = _uploaded_lines(state["upload"])
        assert [r["designated"] for r in rows] == [7, 3]
        # `provider` is a routing key: it never reaches the sealed model input.
        assert "provider" not in _open_line(rows[1])["input"]
        await client.aclose()

    async def test_a_restricted_line_its_providers_do_not_clear_refuses_with_no_market_replan(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[]))
        sealed: list = []
        client._seal_line = lambda **kw: sealed.append(kw)  # never reached
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        with pytest.raises(ValidationError, match=(
            r"^1 of the 1 .* lines restricted to provider 9 cannot be placed in the 24h "
            r"window: .*nothing was signed"
        )):
            await client.batches.submit(
                [{**REQUESTS[0], "body": {**body, "max_rate_in": "0.000001"}}], providers=[9],
            )
        # A restricted line never rests, so no second plan asks for a market rate.
        [plan] = state["plans"]
        assert plan["models"][0]["providers"] == [9]
        assert sealed == [] and "quotes" not in state and "upload" not in state
        await client.aclose()

    async def test_partly_placed_restricted_lines_refuse_the_whole_batch(self):
        """The list takes one of its three lines and the line pinned to provider 1
        beside them is placed in full: nothing is sealed, signed or uploaded."""
        state = {}
        client = _sealing_client(batch_router(state, allocation=lambda entry: [
            {"provider_id": entry["providers"][0], "box_key": BOX_PUBLIC, "rate_in": "0.001",
             "rate_out": "0.002", "lines": 1 if entry["providers"] == [3, 7] else entry["lines"]}]))
        sealed: list = []
        client._seal_line = lambda **kw: sealed.append(kw)  # never reached
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        lines = [{"body": {**body, "provider": 1}}] + [{"body": body}] * 3
        with pytest.raises(ValidationError, match=(
            r"^2 of the 3 .* lines restricted to providers 3, 7 cannot be placed in the 24h window"
        )):
            await client.batches.submit(lines, providers=[7, 3])
        assert sealed == []
        assert not {"quotes", "upload", "create"} & set(state)
        await client.aclose()

    async def test_a_restricted_line_with_no_ceiling_its_providers_cannot_take_is_refused(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[]))
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        with pytest.raises(ValidationError,
                           match="^1 of the 1 .* lines restricted to provider 9 cannot be placed"):
            await client.batches.submit([{**REQUESTS[0], "body": {**body, "provider": 9}}])
        assert "quotes" not in state and "upload" not in state
        await client.aclose()

    async def test_a_plan_that_moves_a_restricted_line_outside_its_list_is_refused(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[
            {"provider_id": 4, "box_key": BOX_PUBLIC, "rate_in": "0.001", "rate_out": "0.002",
             "lines": 3}]))
        with pytest.raises(VorqError,
                           match="planned provider 4 for lines restricted to providers 3, 9"):
            await client.batches.submit(REQUESTS, providers=[9, 3])
        lines = [{**r, "body": {**r["body"], "provider": 9}} for r in REQUESTS]
        with pytest.raises(VorqError, match="planned provider 4 for lines restricted to provider 9"):
            await client.batches.submit(lines, providers=[4])
        assert "quotes" not in state and "upload" not in state
        await client.aclose()

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "7", True, 2**32])
    async def test_a_line_provider_that_is_not_an_id_is_refused_naming_the_line(self, bad):
        state = {}
        client = _sealing_client(batch_router(state))
        lines = [REQUESTS[0], {**REQUESTS[1], "body": {**REQUESTS[1]["body"], "provider": bad}}]
        with pytest.raises(ValidationError, match=r"line 2: provider must be a provider id"):
            await client.batches.submit(lines)
        assert not {"plans", "quotes", "upload"} & set(state)
        await client.aclose()

    @pytest.mark.parametrize("bad, match", [
        ([], r"^providers must be a non-empty list of provider ids"),
        (7, r"^providers must be a non-empty list of provider ids"),
        ("7", r"^providers must be a non-empty list of provider ids"),
        ({3, 7}, r"^providers must be a non-empty list of provider ids"),
        *[([3, bad], r"^providers\[1\] must be a provider id, an integer in \[1, 4294967295\]")
          for bad in (0, -1, 1.5, "7", True, None, 2**32)],
        ([3, 7, 3], r"^providers names provider 3 twice"),
    ])
    async def test_a_providers_list_that_is_not_unique_provider_ids_is_refused(self, bad, match):
        state = {}
        client = _sealing_client(batch_router(state))
        with pytest.raises(ValidationError, match=match):
            await client.batches.submit(REQUESTS, providers=bad)
        # Refused before anything is read, sealed or signed.
        assert not {"plans", "quotes", "upload"} & set(state)
        await client.aclose()

    async def test_an_open_resting_line_without_a_verifier_is_refused_before_sealing(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[]))
        with pytest.raises(EscrowKeyUnverified, match="no verifier to check that key with"):
            await client.batches.submit(REQUESTS)
        assert "quotes" not in state and "upload" not in state
        await client.aclose()

    async def test_an_open_resting_line_with_a_verifier_rests_open(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[]), verifier=escrow_verifier())
        await client.batches.submit(REQUESTS)
        assert [r["designated"] for r in _uploaded_lines(state["upload"])] == [0, 0, 0]
        await client.aclose()

    async def test_the_handle_lists_the_job_ids_in_input_order(self):
        """The correlation key for every line until its sealed result is opened.

        An error row has no sealed result at all, so this list is the only way a
        caller maps a failure back to the request that produced it.
        """
        state = {}
        client = _sealing_client(batch_router(state))

        handle = await client.batches.submit(REQUESTS, providers=[7])

        uploaded = [line["job_id"] for line in _uploaded_lines(state["upload"])]
        assert handle.job_ids == uploaded
        await client.aclose()

    async def test_the_create_names_the_file_the_endpoint_and_the_window(self):
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.submit(REQUESTS, providers=[7], metadata={"run": "nightly"})

        assert state["create"] == {
            "input_file_id": "file-in",
            "endpoint": "/v1/responses",
            "completion_window": "24h",
            "metadata": {"run": "nightly"},
        }
        await client.aclose()

    async def test_an_embeddings_batch_names_its_own_endpoint(self):
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.submit(
            [{"custom_id": "e-1", "url": "/v1/embeddings",
              "body": {"model": MODEL, "input": "vectorise me", **PRICE}}],
            providers=[7],
        )

        assert state["create"]["endpoint"] == "/v1/embeddings"
        assert _uploaded_lines(state["upload"])[0]["url"] == "/v1/embeddings"
        await client.aclose()

    async def test_unpriced_lines_are_planned_and_pinned_before_sealing(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[
            {"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": "0.000005", "rate_out": "0.000009", "lines": 2},
            {"provider_id": 8, "box_key": BOX_PUBLIC, "rate_in": "0.000006", "rate_out": "0.000009", "lines": 1},
        ]))
        unpriced = [{**r, "body": {k: v for k, v in r["body"].items() if k not in PRICE}}
                    for r in REQUESTS]
        await client.batches.submit(unpriced)

        [plan] = state["plans"]
        assert plan["completion_window"] == "24h"
        assert [(m["model_id"], m["lines"]) for m in plan["models"]] == [(1, 3)]
        # The totals are JSON integers, as every integer on the wire is.
        assert all(isinstance(m["units_in"], int) and isinstance(m["units_out"], int) for m in plan["models"])
        rows = _uploaded_lines(state["upload"])
        assert [(r["designated"], r["rate_in"], r["rate_out"]) for r in rows] == [
            (7, "0.000005", "0.000009"), (7, "0.000005", "0.000009"), (8, "0.000006", "0.000009"),
        ]
        await client.aclose()

    async def test_a_batch_the_network_cannot_take_is_refused_before_anything_is_sealed(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[
            {"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": "0.000005", "rate_out": "0.000009", "lines": 2},
        ]))
        unpriced = [{**r, "body": {k: v for k, v in r["body"].items() if k not in PRICE}}
                    for r in REQUESTS]
        with pytest.raises(ValidationError, match="can take 2 of the 3 .* lines without both ceilings"):
            await client.batches.submit(unpriced)
        assert "quotes" not in state and "upload" not in state
        await client.aclose()

    async def test_interleaved_planned_and_resting_lines_keep_their_own_terms(self):
        state = {}
        taken = [
            {"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": "0.000005", "rate_out": "0.000009", "lines": 1},
            {"provider_id": 8, "box_key": BOX_PUBLIC, "rate_in": "0.000006", "rate_out": "0.000009", "lines": 1},
        ]
        client = _sealing_client(batch_router(
            state, allocation=lambda entry: [] if "max_rate_in" in entry else taken,
        ), verifier=escrow_verifier())
        unpriced = {**REQUESTS[0], "body": {k: v for k, v in REQUESTS[0]["body"].items()
                                            if k not in PRICE}}
        lines = [{**unpriced, "custom_id": "u1"}, {**REQUESTS[1], "custom_id": "p"},
                 {**unpriced, "custom_id": "u2"}]
        await client.batches.submit(lines)

        # Nobody is within the priced line's ceilings, so it rests open at them;
        # the unpriced lines around it are planned normally.
        [plan] = state["plans"]
        assert [(m["lines"], m.get("providers")) for m in plan["models"]] == [(2, None), (1, None)]
        rows = _uploaded_lines(state["upload"])
        assert [(r["designated"], r["rate_in"]) for r in rows] == [
            (7, "0.000005"), (0, PRICE["max_rate_in"]), (8, "0.000006"),
        ]
        await client.aclose()

    async def test_lines_with_ceilings_are_planned_within_them_and_sign_the_ask(self):
        state = {}
        client = _sealing_client(batch_router(state))
        await client.batches.submit(REQUESTS)
        [plan] = state["plans"]
        assert [(m["lines"], m["max_rate_in"], m["max_rate_out"]) for m in plan["models"]] == [
            (3, "30", "90"),
        ]
        rows = _uploaded_lines(state["upload"])
        assert {(r["designated"], r["rate_in"], r["rate_out"]) for r in rows} == {(1, "0.001", "0.002")}
        await client.aclose()

    async def test_a_line_with_one_ceiling_rests_at_it_and_the_market_rate(self):
        """Nobody is within the input ceiling, so the line rests: a second plan,
        naming no ceiling, supplies the rate for the side that named none."""
        state = {}
        market = [{"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": "0.000005",
                   "rate_out": "0.000009", "lines": 1}]
        client = _sealing_client(batch_router(
            state, allocation=lambda entry: [] if "max_rate_in" in entry else market,
        ), verifier=escrow_verifier())
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        await client.batches.submit(
            [{**REQUESTS[0], "body": {**body, "max_rate_in": "0.000001"}}],
        )
        within, unbounded = state["plans"]
        assert within["models"][0]["max_rate_in"] == "0.000001"
        assert "max_rate_in" not in unbounded["models"][0]
        [row] = _uploaded_lines(state["upload"])
        assert (row["designated"], row["rate_in"], row["rate_out"]) == (0, "0.000001", "0.000009")
        await client.aclose()

    async def test_a_planned_ask_above_a_ceiling_is_refused(self):
        state = {}
        client = _sealing_client(batch_router(state))
        body = {k: v for k, v in REQUESTS[0]["body"].items() if k not in PRICE}
        with pytest.raises(VorqError, match="above the ceilings"):
            await client.batches.submit([{**REQUESTS[0], "body": {**body, "max_rate_out": "0.001"}}])
        assert "upload" not in state
        await client.aclose()

    @pytest.mark.parametrize("rate", [30, 0.5, "1e3"])
    async def test_a_line_rate_that_is_not_usd_is_refused_before_anything_is_sealed(self, rate):
        state = {}
        client = _sealing_client(batch_router(state))
        lines = [{**REQUESTS[0], "body": {**REQUESTS[0]["body"], "max_rate_in": rate}}]
        with pytest.raises(ValidationError, match=r'line 1: max_rate_in.*USD per 1M units'):
            await client.batches.submit(lines, providers=[7])
        assert not {"plans", "quotes", "upload"} & set(state)
        await client.aclose()

    async def test_a_plan_rate_that_is_not_canonical_usd_is_refused(self):
        state = {}
        client = _sealing_client(batch_router(state, allocation=[
            {"provider_id": 7, "box_key": BOX_PUBLIC, "rate_in": 5, "rate_out": "0.000009", "lines": 3},
        ]))
        unpriced = [{**r, "body": {k: v for k, v in r["body"].items() if k not in PRICE}}
                    for r in REQUESTS]
        with pytest.raises(VorqError, match="plan allocation is unreadable"):
            await client.batches.submit(unpriced)
        assert "quotes" not in state and "upload" not in state
        await client.aclose()

    @pytest.mark.parametrize(
        "bad, match",
        [
            ([], "no requests"),
            ([{"body": {"input": "x", **PRICE}}], "body.model is required"),
            ([{"custom_id": "a", "body": {"model": MODEL, "input": "x", **PRICE}},
              {"custom_id": "a", "body": {"model": MODEL, "input": "y", **PRICE}}], "duplicate custom_id"),
            ([{"custom_id": "", "body": {"model": MODEL, "input": "x", **PRICE}}], "1-64 character"),
            ([{"url": "/v1/chat/completions", "body": {"model": MODEL, "input": "x", **PRICE}}],
             "url must be one of"),
            ([{"url": "/v1/responses", "body": {"model": MODEL, "input": "x", **PRICE}},
              {"url": "/v1/embeddings", "body": {"model": MODEL, "input": "y", **PRICE}}],
             "one endpoint"),
        ],
    )
    async def test_a_bad_file_is_refused_before_anything_is_sealed(self, bad, match):
        """Every one of these is knowable locally, and sealing is the expensive half.

        The assertion that matters is the empty request log: a file with a typo on
        line 40 000 should cost the read and nothing else.
        """
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.path)
            return json_response(500, {"error": {"message": "should not be reached"}})

        client = _sealing_client(auth_router(handler))
        with pytest.raises(ValidationError, match=match):
            await client.batches.submit(bad, providers=[7])
        assert "/v1/files" not in seen
        await client.aclose()


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


@needs_wallet
class TestWaiting:
    async def test_a_batch_is_polled_on_the_stepped_schedule_with_one_read_a_tick(self, wait_clock):
        """Once a minute for 15 min, every 3 min to the hour, every 10 min after.

        And the read is the batch's own, once per tick: a batch of thousands of
        lines costs what a batch of one does, never a request per job.
        """
        seen: list[str] = []
        waiting = batch_object(status="in_progress", output_file_id=None,
                               request_counts={"completed": 0, "failed": 0, "total": 5000})

        def handler(request):
            seen.append(f"{request.method} {request.url.path}")
            return json_response(200, waiting)

        client = _sealing_client(auth_router(handler))
        handle = client.batches.get("batch_1")
        handle.job_ids = [f"0x{n:064x}" for n in range(5000)]
        with pytest.raises(WaitTimeout):
            await handle.results()

        assert wait_clock == [60.0] * 15 + [180.0] * 15 + [600.0] * 138
        reads = [call for call in seen if "/v1/" in call]
        assert reads == ["GET /v1/batches/batch_1"] * (len(wait_clock) + 1)
        await client.aclose()

    async def test_a_1h_batch_follows_the_same_schedule(self, wait_clock):
        state = {}
        client = _sealing_client(batch_router(
            state, batch=batch_object(status="in_progress", completion_window="1h")))
        with pytest.raises(WaitTimeout):
            await client.batches.get("batch_1").results()
        assert wait_clock == [60.0] * 15 + [180.0] * 15
        assert state["polls"] == 31
        await client.aclose()

    async def test_a_sleep_never_runs_past_the_timeout(self, wait_clock):
        state = {}
        client = _sealing_client(batch_router(state, batch=batch_object(status="in_progress")))
        with pytest.raises(WaitTimeout):
            await client.batches.get("batch_1").results(timeout=100)
        assert wait_clock == [60.0, 40.0]
        await client.aclose()


@needs_wallet
class TestResults:
    async def test_each_row_is_opened_from_the_bytes_it_names(self):
        """`result_cid` is authoritative and there is no inline copy to fall back to.

        The coordinator cannot read a result — it is sealed to this client's own
        key — so the row names the bytes and says nothing about them. Reading the
        named bytes and checking them is what stops sealed bodies being swapped
        between lines.
        """
        state = {}
        client = _sealing_client(batch_router(state))
        cid_a, raw_a = _sealed_result(client, "first")
        cid_b, raw_b = _sealed_result(client, "second")
        state.clear()
        client = _sealing_client(
            batch_router(
                state,
                files={"file-out": output_row(1, "0xaa", cid_a) + "\n"
                                   + output_row(2, "0xbb", cid_b) + "\n"},
                blobs={cid_a: raw_a, cid_b: raw_b},
            )
        )

        results = await client.batches.get("batch_1").results()

        assert [r.text for r in results] == ["first", "second"]
        assert all(isinstance(r, TextResult) for r in results)
        # Each row carries its own line's rates, so cost is per line and never a
        # batch-level average.
        assert results[0].rates == (Decimal("0.03"), Decimal("0.09"))
        await client.aclose()

    async def test_the_output_file_is_read_once_and_never_with_an_offset(self):
        """The file is frozen and pinned once, at the end.

        Content-addressed storage is immutable, so an append mints a different
        object under a different name: there is no partial file to resume into and
        no `?offset=` that could mean anything.
        """
        state = {}
        cid, raw = _sealed_result(None, "only")
        client = _sealing_client(
            batch_router(state, files={"file-out": output_row(1, "0xaa", cid) + "\n"},
                         blobs={cid: raw})
        )

        await client.batches.get("batch_1").results()

        assert len(state["content_reads"]) == 1
        assert "offset" not in state["content_reads"][0]
        await client.aclose()

    async def test_an_error_row_becomes_a_job_error_carrying_the_chain_s_own_cause(self):
        """One cause vocabulary, end to end.

        A client cancel and an order nobody claimed before its deadline are
        different facts; reporting both as "cancelled" lies about the second.
        """
        state = {}
        cid, raw = _sealed_result(None, "delivered")
        client = _sealing_client(
            batch_router(
                state,
                batch=batch_object(error_file_id="file-err"),
                files={
                    "file-out": output_row(1, "0xaa", cid) + "\n",
                    "file-err": error_row(2, "0xbb", "reclaim") + "\n"
                                + error_row(3, None, "invalid_order_signature") + "\n",
                },
                blobs={cid: raw},
            )
        )

        results = await client.batches.get("batch_1").results()

        errors = [r for r in results if isinstance(r, JobError)]
        assert [(e.type, e.job_id) for e in errors] == [
            ("reclaim", "0xbb"),
            ("invalid_order_signature", None),
        ]
        # A line that never became a job names none, which is why `job_ids` in
        # input order is the correlation key rather than anything on the row.
        assert errors[1].custom_id is None
        await client.aclose()

    async def test_a_failed_batch_raises_rather_than_reading_files_that_do_not_exist(self):
        """`failed` is the input file's own verdict, not a line's.

        There are no lines and therefore no output to read; a drain that tried
        would 404 and report the wrong thing.
        """
        state = {}
        client = _sealing_client(
            batch_router(state, batch=batch_object(status="failed", output_file_id=None))
        )

        with pytest.raises(BatchFailed, match="batch_1"):
            await client.batches.get("batch_1").results()
        assert "content_reads" not in state
        await client.aclose()

    async def test_cancel_relays_and_names_the_batch(self):
        state = {}
        client = _sealing_client(batch_router(state))

        await client.batches.get("batch_1").cancel()

        assert state["cancelled"] == "/v1/batches/batch_1/cancel"
        await client.aclose()

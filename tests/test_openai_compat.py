"""SealingTransport: the stock-openai path with end-to-end sealed payloads."""

from __future__ import annotations

import base64
import json
import os
from decimal import Decimal

import httpx
import pytest

from .conftest import req_body
from .test_client import CATALOG, CHAIN, provider_record, quote_body

# Only the tests that drive the sealed transport need key material. Marking them
# individually keeps the tests that are pure functions of their input — which
# seal nothing and need no wallet — running everywhere.
needs_wallet = pytest.mark.skipif(
    not os.environ.get("VORQ_WALLET_KEY"),
    reason="needs .env.test key material",
)


def _in_the_clear(submitted: dict) -> str:
    """Everything a submission puts on the wire outside the sealed container.

    The container rides inline as base64; what these tests check is that no
    prompt or param is on the wire *beside* it.
    """
    return json.dumps({k: v for k, v in submitted.items() if k != "container"})


def _emulator_handler(state: dict, *, opaque: bool = False, media: bool = False):
    """Route table for the native client underneath the transport: auth, 402
    challenge, job create, job fetch. Seals the result back to the client's key.

    A settled job names its result and the bytes are served from the blob
    surface — the path the compat surface reads a result on. ``opaque`` names
    bytes that are not a result object at all; ``media`` settles the job with a
    frame-bearing output instead of a chat completion.
    """
    from .conftest import fake_cid
    from vorq._container import commitment_of, derive_dek, open_dek, split_container
    from vorq._crypto import seal_to
    from nacl.public import PrivateKey, SealedBox
    from nacl.encoding import HexEncoder

    provider_key = PrivateKey.generate()
    # The client seals the payload to this box key with ``seal_to``, which decodes
    # the key as hex — so advertise the provider's public key hex-encoded.
    provider_box = provider_key.public_key.encode(HexEncoder).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "vorq_sess_t", "expires_at": 9999999999})
        if path == "/v1/models":
            return httpx.Response(200, json=CATALOG)
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        if path == "/key":
            # The escrow announcement an **open** order seals to. The same key
            # the designated path uses, so one emulator opens both containers:
            # what differs between them is who the client believed it was sealing
            # to, and `designated` is where that belief is recorded.
            return httpx.Response(200, json={"public_key": provider_box})
        if path.startswith("/evm/providers/"):
            return httpx.Response(200, json=provider_record(3, provider_box))
        if method == "POST" and path == "/v1/batches":   # forwarded (non-Responses) path
            return httpx.Response(200, json={"echoed": json.loads(request.content)})
        if method == "POST" and path == "/v1/jobs":
            body = req_body(request)
            if "auth_sig" not in body:      # phase 1: terms-only → 402 challenge
                # A market probe (0/0) names provider 3 with this emulator's key.
                market = "rate_in" not in body and "rate_out" not in body
                candidates = ([{"provider_id": 3, "box_key": provider_box,
                                "rate_in": "0.000007", "rate_out": "0.000011"}] if market else None)
                return httpx.Response(402, json=quote_body(body, candidates=candidates))
            state["submitted"] = body      # phase 2: the complete submission
            # The bytes ride the funded request inline as base64, and the terms
            # carry the commitment over them.
            container = base64.b64decode(body["container"])
            assert commitment_of(container) == bytes.fromhex(body["c"][2:])
            # Open it as the designated provider would: unseal the 80-byte wrap
            # to recover the DEK, then open the bulk under it. The envelope names
            # the owner and the key the result must be sealed back to.
            # The wrap holds a SEED (Q3): unseal it, then derive the DEK with the
            # job's owner before anything opens.
            wrap, ciphertext = split_container(container)
            state["envelope"] = json.loads(open_dek(
                ciphertext,
                derive_dek(SealedBox(provider_key).decrypt(wrap), body["owner"]),
            ))
            assert state["envelope"]["v"] == "vorq-env-v1"
            job_id = body["job_id"]
            state["job_id"] = job_id
            return httpx.Response(201, json={"job_id": job_id, "task_cid": "bafy-task",
                                             "tx_hash": "0x" + "11" * 32})
        if method == "GET" and path.startswith("/ipfs/"):
            return httpx.Response(200, content=state["result_bytes"])
        if method == "POST" and path.startswith("/v1/jobs/") and path.endswith("/cancel"):
            state["cancelled"] = path
            # `relay(chain, ..., reply, 200, { job_id: jobId })` — the receipt,
            # not a job row. There is no `id` and no `status` on it.
            return httpx.Response(200, json={"job_id": state["job_id"],
                                             "tx_hash": "0x" + "22" * 32})
        if method == "GET" and path.startswith("/v1/jobs/") and state.get("cancelled"):
            return httpx.Response(200, json={
                "id": state["job_id"], "object": "job", "status": "cancelled",
                "result_cid": None,
                "vorq": {"gas_fee": "0.03", "fee": "0", "sla_secs": 3600, "state": 3, "ended_because": 2,
                         "provider_id": 3}})
        if method == "GET" and path.startswith("/v1/jobs/"):
            if media:
                result = {"images": [{"b64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
                                      "content_type": "image/png", "width": 8, "height": 8}]}
            else:
                result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "sealed hello"}}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}}
            sealed = seal_to(state["envelope"]["result_key"], json.dumps(result).encode())
            output = {"enc": "vorq-sealed-v1", "ciphertext": base64.b64encode(sealed).decode()}
            job = {
                "id": state["job_id"], "object": "job", "status": "completed",
                "vorq": {"gas_fee": "0.03", "fee": "0", "sla_secs": 3600, "rate_in": "0.1",
                         "rate_out": "0.5", "provider_id": 3, "state": 2, "ended_because": 1},
            }
            named = b"\x00\x01\xff\xfe" if opaque else json.dumps(output).encode()
            state["result_bytes"] = named
            return httpx.Response(200, json={**job, "result_cid": fake_cid(named)})
        raise AssertionError(f"unexpected {method} {path}")

    return handler


@needs_wallet
def test_sync_create_seals_submits_and_unseals():
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    resp = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello", "max_output_tokens": 64,
        "temperature": 0.7, "vorq": {"sla": "1h", "provider": 3},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "sealed hello"
    assert body["usage"]["output_tokens"] == 7
    # E2E property: the whole submitted order is ciphertext — neither the prompt
    # nor any request param crosses the wire in the clear.
    raw = _in_the_clear(state["submitted"])
    assert "hello" not in raw and "max_output_tokens" not in raw and "temperature" not in raw
    from .conftest import json_keys

    assert "input" not in state["submitted"]
    keys = json_keys(state["submitted"])
    assert "dek" not in keys        # the DEK exists on the wire only inside the wrap
    assert "task_cid" not in keys   # and the client never names the content


@needs_wallet
def test_a_create_that_names_no_bid_takes_the_market_in_the_1h_window():
    """No `vorq` block: the transport's own 1h window, and the node's first pick."""
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    resp = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello", "max_output_tokens": 64,
    })
    assert resp.status_code == 200
    terms = state["submitted"]
    assert (terms["rate_in"], terms["rate_out"], int(terms["designated"])) == ("0.000007", "0.000011", 3)
    assert int(terms["sla_secs"]) == 3600


class _StubVerifier:
    """Takes the announcement at its word, and records that it was asked.

    A real :class:`vorq.Verifier` checks the evidence against chain state, which
    is a different test's subject. What is under test here is whether the compat
    transport can carry one at all — so this only has to be the thing the native
    client refuses to post an open order without.
    """

    def __init__(self) -> None:
        self.asked = 0

    async def verify_escrow_key(self, announcement: dict) -> str:
        self.asked += 1
        return announcement["public_key"]


@needs_wallet
def test_the_vorq_block_carries_the_bid_into_the_signed_order():
    """A stock caller has to be able to say what it will pay.

    The order signs `rate_in` and `rate_out`, and a bid below the provider's
    published ask is posted and never claimed — so a surface with no way to
    express one can only submit at zero, which every provider on a live network
    declines. The failure is invisible from a mocked emulator, which settles
    whatever it is given: it looks like a claim path that never fires.

    `vorq` is where a stock caller puts what OpenAI's body has no field for, and
    it already carries `sla` and `provider`. The bid belongs beside them.
    """
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    resp = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello",
        "vorq": {"sla": "1h", "provider": 3, "rate_in": "0.000007", "rate_out": "0.000011"},
    })

    assert resp.status_code == 200, resp.text
    terms = state["submitted"]
    # USD strings on the wire, as all money is.
    assert (terms["rate_in"], terms["rate_out"]) == ("0.000007", "0.000011")
    # And the bid is not in the payload: a rate is an order term, not a prompt
    # param, so it must not have been swept into the sealed body instead.
    assert "rate_in" not in json.dumps(state["envelope"])


@needs_wallet
def test_a_bid_that_is_not_a_usd_string_is_refused_before_anything_is_submitted():
    """A JSON number is ambiguous between USD and atomic units; the rate is USD per 1M units."""
    from vorq import sealing_http_client

    state: dict = {}
    resp = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    ).post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello",
        "vorq": {"sla": "1h", "provider": 3, "rate_in": 7, "rate_out": "0.000011"},
    })

    assert resp.status_code == 400
    assert "USD per 1M units" in resp.text
    assert "submitted" not in state


@needs_wallet
def test_an_open_order_is_refused_without_a_verifier_and_posts_with_one():
    """The default order shape, on the surface a stock caller uses.

    An open order seals to the coordinator's escrow key, and the native client
    fails closed without a verifier — `EscrowKeyUnverified`, nothing posted. That
    is Q17 working. What it means for this transport is that a stock caller
    against any coordinator that hosts an escrow — which is the default
    deployment — can submit **nothing** unless it names a provider, because there
    was no way to hand one in.
    """
    from vorq import sealing_http_client

    body = {"model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello",
            "vorq": {"sla": "1h", "rate_in": "0.000007", "rate_out": "0.000011"}}

    blind: dict = {}
    refused = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(blind)),
    ).post("/v1/responses", json=body)

    assert refused.status_code == 400
    assert "verifier" in refused.json()["error"]["message"]
    assert "submitted" not in blind, "an unverifiable open order reached the wire"

    state: dict = {}
    verifier = _StubVerifier()
    resp = sealing_http_client(
        base_url="http://coordinator.test", verifier=verifier,
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    ).post("/v1/responses", json=body)

    assert resp.status_code == 200, resp.text
    assert verifier.asked == 1, "the transport did not carry the verifier down"
    # `0` is the contract's own sentinel for "any provider" (Q22) — the order is
    # open, not quietly re-targeted at somebody.
    assert int(state["submitted"]["designated"]) == 0


@needs_wallet
def test_background_create_then_retrieve():
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    created = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello", "background": True, "vorq": {"sla": "1h", "provider": 3},
    }).json()
    assert created["status"] == "queued"
    fetched = client.get(f"/v1/responses/{created['id']}").json()
    assert fetched["status"] == "completed"
    assert fetched["output"][0]["content"][0]["text"] == "sealed hello"


@needs_wallet
def test_named_result_is_fetched_and_opened_on_the_compat_surface():
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    body = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello", "vorq": {"sla": "1h", "provider": 3},
    }).json()
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "sealed hello"
    # The background retrieve reads the same named result the same way.
    created = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello",
        "background": True, "vorq": {"sla": "1h", "provider": 3},
    }).json()
    fetched = client.get(f"/v1/responses/{created['id']}").json()
    assert fetched["status"] == "completed"
    assert fetched["output"][0]["content"][0]["text"] == "sealed hello"


def test_media_result_renders_as_image_generation_calls():
    """An OpenAI client consumes bytes: base64 in `result`, one item per frame.

    Verified against openai 2.47.0, where ImageGenerationCall is
    {id, result, status, type} and `result` is base64.
    """
    from vorq._openai_compat import _response_object
    from vorq._results import MediaResult

    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    result = MediaResult(frames=[{"b64": b64, "content_type": "image/png",
                                  "width": 1024, "height": 768}],
                         seed=42, raw={}, rates=(None, Decimal("0.09")), cost="0.07077888", gas_fee=Decimal("0.03"),
                         fee=Decimal("0.000707"), provider=1, job_id="job_1")

    obj = _response_object({"id": "job_1", "status": "completed"},
                           model="m", background=False, result=result)

    assert obj["status"] == "completed"
    assert len(obj["output"]) == 1
    item = obj["output"][0]
    assert item["type"] == "image_generation_call"
    assert item["status"] == "completed"
    assert item["result"] == b64          # passed through, never re-encoded


@needs_wallet
def test_background_retrieve_renders_a_settled_media_job():
    """The full path for media: seal, submit, fetch, open, render.

    Covers the retrieve guard, the result dispatch, and the rendered item over a
    real sealed round trip rather than a hand-built result.
    """
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state, media=True)),
    )
    created = client.post("/v1/responses", json={
        "model": "black-forest-labs/flux-2-pro", "input": "a cat", "background": True,
        "vorq": {"sla": "1h", "provider": 3},
    }).json()
    assert created["status"] == "queued"
    fetched = client.get(f"/v1/responses/{created['id']}").json()
    assert fetched["status"] == "completed"
    item = fetched["output"][0]
    assert item["type"] == "image_generation_call"
    assert base64.b64decode(item["result"]) == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("path", [
    "/v1/chat/completions", "/v1/completions", "/v1/embeddings",
    "/v1/moderations", "/v1/threads", "/v1/files", "/v1/vector_stores",
    "/v1/images/generations", "/v1/images/edits", "/v1/images/variations",
    # Prefix entries: the whole family is refused, not just its root.
    "/v1/audio/speech", "/v1/audio/transcriptions",
    "/v1/threads/thread_abc/messages",
    "/v1/vector_stores/vs_abc/files",
    "/v1/realtime/sessions",
    # Routes nobody enumerated: refusal is the default, so a surface the SDK
    # grows later is covered on the day it ships rather than on the day someone
    # notices it. This is the property the direction of the list buys.
    "/v1/conversations",
    "/v1/assistants/asst_abc",
    "/v1/some/surface/invented/after/this/test",
])
def test_prompt_paths_are_refused_without_transmitting(path):
    """A path this transport does not seal never leaves the process.

    The assertion that matters is the empty ``seen``: refusing after the body is
    on the wire would disclose it, and the caller would have no way to tell.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={"error": {"message": "unsupported"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.post(path, json={"model": "m", "input": "secret prompt"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert path in error["message"] and "was not sent" in error["message"]
    # Every refusal names the surface to use instead, and it is a surface that
    # exists. `POST /v1/files` is the one that points somewhere else: a stock
    # `files.create(purpose="batch")` uploads a plaintext JSONL, so it is refused
    # like any other prompt path and sent to the batch surface, which seals each
    # line before the file is built.
    if path == "/v1/files":
        assert "client.batches.submit" in error["message"]
        assert "in the clear" in error["message"]
    else:
        assert "/v1/responses" in error["message"]
    assert seen == []


@pytest.mark.parametrize("path", ["/api/v1/responses", "/api/v1/chat/completions"])
def test_mount_prefixed_paths_are_refused(path):
    """A mount prefix is a misconfiguration that fails closed.

    Both the intercept and the forward list match ``/v1/...`` exactly, so
    ``OpenAI(base_url="https://host/api/v1")`` matches neither. The prompt still
    does not leave the process: an unrecognised path is refused, so the
    misconfiguration surfaces as a 400 instead of as a silently unsealed body.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={"error": {"message": "unsupported"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.post(path, json={"model": "m", "input": "secret prompt"})
    assert resp.status_code == 400
    assert "was not sent" in resp.json()["error"]["message"]
    assert seen == []


@needs_wallet
def test_responses_cancel_maps_to_the_native_cancel():
    """``client.responses.cancel(id)`` is the native cancel under an OpenAI name.

    A response id is the job id, and the coordinator serves cancellation at
    ``/v1/jobs/{id}/cancel`` — there is no ``/v1/responses/{id}/cancel`` to
    forward to, so this is intercepted rather than passed through.
    """
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    created = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello",
        "background": True, "vorq": {"sla": "1h", "provider": 3},
    }).json()
    resp = client.post(f"/v1/responses/{created['id']}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "cancelled"
    assert body["id"] == created["id"]
    assert state["cancelled"] == f"/v1/jobs/{created['id']}/cancel"


@needs_wallet
def test_the_cancel_answer_is_a_relay_receipt_and_not_a_job_object():
    """`POST /v1/jobs/{id}/cancel` answers `{job_id, tx_hash}` — and only that.

    `src/api/routes/post.ts` ends the cancel handler in
    `relay(chain, ..., reply, 200, { job_id: jobId })`, and `relay` sends
    `{...body, tx_hash}`. There is no `id`, no `object`, no `status` and no
    terms on it: a cancel creates nothing, so it names the row it ended and the
    transaction that ended it.

    The mapping used to require an `id` and raise "returned no job object",
    turning a **successful** cancel into an error for every caller of the compat
    surface. This body is written out literally so that regressing to `id` fails
    here rather than in the stack.
    """
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    job_id = "0x" + "ab" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        assert request.method == "POST" and path == f"/v1/jobs/{job_id}/cancel"
        return httpx.Response(200, json={"job_id": job_id, "tx_hash": "0x" + "22" * 32})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    resp = client.responses.cancel(job_id)
    # The receipt named the job and the chain had already mined the cancel when
    # the node answered, so `cancelled` is read off the receipt, not guessed.
    assert resp.id == job_id
    assert resp.status == "cancelled"


@needs_wallet
def test_a_cancel_answer_naming_no_job_is_refused_by_name():
    """A `2xx` that names no `job_id` is a coordinator this client does not know.

    Named as such rather than left to escape as a `KeyError`, which the `openai`
    package wraps as `APIConnectionError` — blaming the network for a node that
    answered.
    """
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    job_id = "0x" + "ab" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        return httpx.Response(200, json={"tx_hash": "0x" + "22" * 32})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.APIStatusError) as caught:
        client.responses.cancel(job_id)
    assert "named no job_id" in str(caught.value)


@needs_wallet
def test_content_free_routes_still_forward():
    """``GET /v1/models`` forwards — the batch routes are covered by the openai tests."""
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    assert client.get("/v1/models").status_code == 200


@needs_wallet
def test_forward_preserves_request_body():
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state)),
    )
    # A forwarded path goes verbatim through the native client; the request body
    # must survive the hop (the fix for _forward dropping it).
    resp = client.post("/v1/batches", json={"marker": "round-trip", "n": 7})
    assert resp.status_code == 200
    assert resp.json() == {"echoed": {"marker": "round-trip", "n": 7}}


@needs_wallet
def test_compat_surface_shapes_opaque_bytes_into_an_error_response():
    """Bytes that are not a result object are refused, not crashed on."""
    from vorq import sealing_http_client

    state: dict = {}
    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(_emulator_handler(state, opaque=True)),
    )
    resp = client.post("/v1/responses", json={
        "model": "deepseek-ai/deepseek-v4-pro:fp8", "input": "hello", "vorq": {"sla": "1h", "provider": 3},
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "result_integrity"


# -- the stock openai package ----------------------------------------------
#
# Every test above drives the transport with hand-built httpx requests, which
# proves the transport's own behaviour but not the thing it exists for. The
# contract is with the `openai` package: its URL building decides which paths
# reach handle_request, and its response models decide whether the synthesized
# Response parses. These drive the real package.


@needs_wallet
def test_stock_openai_package_seals_and_reads_a_response():
    """The synthesized Response satisfies the package's own model."""
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    state: dict = {}
    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(
            base_url="http://coordinator.test",
            inner_transport=httpx.MockTransport(_emulator_handler(state))),
    )
    resp = client.responses.create(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello",
                                   extra_body={"vorq": {"sla": "1h", "provider": 3}})
    assert resp.output_text == "sealed hello"
    # The order the package's call produced is ciphertext, same as the raw path.
    assert "hello" not in _in_the_clear(state["submitted"])

    # models.list() is the forward list's reason to exist.
    assert [m.id for m in client.models.list().data] == [
        d["id"] for d in CATALOG["data"]
    ]

    # Background create, retrieve, cancel — the three-call flow, all intercepted.
    bg = client.responses.create(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi",
                                 background=True,
                                 extra_body={"vorq": {"sla": "1h", "provider": 3}})
    assert bg.status == "queued"
    assert client.responses.retrieve(bg.id).output_text == "sealed hello"
    assert client.responses.cancel(bg.id).status == "cancelled"


@needs_wallet
def test_stock_openai_batch_calls_all_land_on_the_forward_list():
    """Every path the package's batch surface builds is one the transport forwards."""
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    batch = {"id": "batch_1", "object": "batch", "endpoint": "/v1/chat/completions",
             "input_file_id": "file_in", "completion_window": "24h", "status": "completed",
             "created_at": 1, "output_file_id": "file_out"}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        seen.append(f"{method} {path}")
        if path == "/v1/batches" and method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [batch], "has_more": False})
        if path.startswith("/v1/batches"):
            return httpx.Response(200, json=batch)
        if path.endswith("/content"):
            return httpx.Response(200, content=b'{"line": 1}\n',
                                  headers={"content-type": "application/json"})
        raise AssertionError(f"unexpected {method} {path}")

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    client.batches.create(input_file_id="file_in", endpoint="/v1/chat/completions",
                          completion_window="24h")
    client.batches.retrieve("batch_1")
    list(client.batches.list())
    client.batches.cancel("batch_1")
    assert client.files.content("file_out").read() == b'{"line": 1}\n'
    # Reaching the handler at all means none of these was refused.
    assert seen == ["POST /v1/batches", "GET /v1/batches/batch_1", "GET /v1/batches",
                    "POST /v1/batches/batch_1/cancel", "GET /v1/files/file_out/content"]


@needs_wallet
def test_stock_openai_prompt_call_raises_before_the_prompt_is_sent():
    """A refused path surfaces as the package's own BadRequestError, naming the fix."""
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={"error": {"message": "unsupported"}})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.BadRequestError) as excinfo:
        client.chat.completions.create(model="m",
                                       messages=[{"role": "user", "content": "secret"}])
    assert "/v1/responses" in str(excinfo.value)
    assert seen == []


@pytest.mark.parametrize("body", [
    {"input": "hello"},                 # omitted entirely
    {"model": None, "input": "hello"},
    {"model": "", "input": "hello"},
    {"model": 7, "input": "hello"},     # present but not a name
])
def test_create_without_a_model_is_refused_locally(body):
    """An order with no model is refused before it is sealed.

    The model picks the provider and the rates the order clears at, so there is
    nothing to seal to. Needs no wallet: the refusal happens before any key
    material is touched.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "should not be reached"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.post("/v1/responses", json=body)
    assert resp.status_code == 400
    assert "'model' is required" in resp.json()["error"]["message"]
    assert seen == []


@pytest.mark.parametrize("body, expected", [
    ({"model": "m", "input": "hi", "stream": True}, "streaming is not available"),
    ({"model": "m", "input": "hi", "metadata": {"k": "v"}},
     "'metadata' is not carried by the sealed Responses surface"),
])
def test_unsupported_create_options_are_refused_not_ignored(body, expected):
    """Options the sealed surface cannot honour fail loudly.

    Both were previously accepted and quietly dropped, which is the worst shape
    for each: ``stream`` produced an *empty* event stream, because the openai
    package reads a non-SSE 200 as a stream with no events — so the caller got no
    answer and no error. ``metadata`` came back as ``{}``, reading as stored.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "should not be reached"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.post("/v1/responses", json=body)
    assert resp.status_code == 400
    assert expected in resp.json()["error"]["message"]
    assert seen == []


@needs_wallet
def test_stock_openai_streaming_raises_instead_of_yielding_nothing():
    """Through the real package: the refusal is what a caller sees, not an empty loop."""
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    state: dict = {}
    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(
            base_url="http://coordinator.test",
            inner_transport=httpx.MockTransport(_emulator_handler(state))),
    )
    with pytest.raises(openai.BadRequestError) as excinfo:
        for _ in client.responses.create(model="deepseek-ai/deepseek-v4-pro:fp8",
                                         input="hi", stream=True):
            pass
    assert "background=True" in str(excinfo.value)


@needs_wallet
def test_cancelling_a_claimed_job_surfaces_as_a_conflict():
    """A coordinator 409 reaches the caller as 409, not flattened to 400.

    A claimed job ends by settlement, provider failure or SLA reclaim, so the
    cancel is refused. Through the openai package that must be
    ``ConflictError`` — a caller distinguishing "too late to cancel" from "bad
    request" has only the status to go on.
    """
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    job_id = "0x" + "ab" * 32
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        # The compat cancel is the native one under another name, so it reads the
        # chain context first: the signature it posts is bound to a deployment.
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        assert request.method == "POST" and path.endswith("/cancel")
        sent["body"] = json.loads(request.content)
        return httpx.Response(409, json={"error": {
            "type": "state_conflict", "message": "Job x is claimed; a claimed job ends by "
            "settlement, provider failure, or SLA reclaim."}})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.ConflictError) as excinfo:
        client.responses.cancel(job_id)
    assert "claimed" in str(excinfo.value)
    # And it was the signed body that was refused, not an unsigned one: this
    # surface authors no cancel of its own.
    assert set(sent["body"]) == {"issued_at", "signature"}


@pytest.mark.parametrize("method, path", [
    # The submission route. The single most dangerous entry to add to _FORWARD by
    # accident: forwarding it would post an unsealed payload to the coordinator.
    ("POST", "/v1/jobs"),
    # Method confusion. Every _FORWARD entry is keyed on (method, pattern); these
    # share a path with a forwarded route but not its method, and a future edit
    # that widened an entry to a bare path would let them through.
    ("POST", "/v1/models"),
    ("DELETE", "/v1/batches/batch_1"),
    ("GET", "/v1/batches/batch_1/cancel"),
    ("POST", "/v1/files/file_1/content"),
    # Segment counts. '*' is one segment, never zero and never several.
    ("GET", "/v1/jobs/"),
    ("GET", "/v1/batches/batch_1/extra"),
    ("GET", "/v1/files//content"),
])
def test_forward_list_boundaries_are_refused(method, path):
    """Routes adjacent to the forward list stay off it.

    The forward list is the whole security boundary, so its edges are pinned
    rather than left to the reading of `_matches`.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "should not be reached"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.request(method, path, json={"secret": "payload"})
    assert resp.status_code == 400
    assert "was not sent" in resp.json()["error"]["message"]
    assert seen == []


@pytest.mark.parametrize("path", [
    "/v1/responses/",                       # no id
    "/v1/responses/resp_1/input_items",     # a real openai 2.x call, not a retrieve
    "/v1/responses/resp_1/extra/deep",
])
def test_responses_subroutes_are_not_swallowed_by_the_retrieve_intercept(path):
    """Only an exact ``/v1/responses/{id}`` is a retrieve.

    A prefix match would turn ``client.responses.input_items.list(id)`` into a
    lookup for a job named ``input_items`` — a confusing NotFoundError on an id
    the caller never used, rather than an honest refusal.
    """
    from vorq import sealing_http_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "should not be reached"}})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    assert client.get(path).status_code == 400
    assert seen == []


@needs_wallet
@pytest.mark.parametrize("wire_status, wire_type, expected", [
    (400, "invalid_request", "BadRequestError"),
    (401, "unauthenticated", "AuthenticationError"),
    (404, "not_found", "NotFoundError"),
    (409, "state_conflict", "ConflictError"),
    # Beyond the four the exception hierarchy names. These matter most: the
    # openai package retries 429 and 5xx and does NOT retry 400, so flattening
    # them to 400 would silently remove its retry policy.
    (429, "rate_limited", "RateLimitError"),
    (500, "internal_error", "InternalServerError"),
    (503, "unavailable", "InternalServerError"),
])
def test_coordinator_status_reaches_the_caller(wire_status, wire_type, expected):
    """A wire status survives the transport, so openai raises the class it implies."""
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        return httpx.Response(wire_status, json={"error": {
            "type": wire_type, "message": "upstream said so"}})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=0,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(getattr(openai, expected)) as excinfo:
        client.responses.cancel("0xabc")
    assert excinfo.value.status_code == wire_status
    assert "upstream said so" in str(excinfo.value)


@needs_wallet
def test_a_forwarded_response_relays_the_request_id_and_nothing_else():
    """Relaying is an allow-list, not a copy of what the coordinator sent.

    `x-request-id` is what the openai package attaches to its exceptions, so
    dropping it loses information the body does not repeat. The completeness
    headers that used to ride here are gone with the streamed output file: a batch
    output is frozen and pinned once, so there is no partial file to describe.
    """
    from vorq import sealing_http_client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        return httpx.Response(200, content=b'{"line": 1}\n', headers={
            "content-type": "application/json", "x-incomplete": "false",
            "x-last-line": "3", "x-request-id": "req_9",
            "x-coordinator-internal": "should not be relayed"})

    client = sealing_http_client(
        base_url="http://coordinator.test",
        inner_transport=httpx.MockTransport(handler),
    )
    resp = client.get("/v1/files/file_out/content")
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "req_9"
    assert "x-coordinator-internal" not in resp.headers
    # The streaming contract went with the streamed file. A transport still
    # relaying these would describe a state the surface can no longer be in.
    assert "x-incomplete" not in resp.headers
    assert "x-last-line" not in resp.headers


@needs_wallet
@pytest.mark.parametrize("wire_status", [429, 500, 503])
def test_a_failed_submission_is_never_retried(wire_status):
    """A create that fails is attempted once, whatever status it failed with.

    The openai package retries 429 and 5xx by default. Submission is not
    idempotent — the native client passes ``retry=False`` on ``POST /v1/jobs``
    for exactly this reason — and a retry re-seals, producing a *different* job
    id, so the coordinator's duplicate-id guard cannot collapse the copies
    either. A retried failure would mean paying twice for one call.
    """
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        if path == "/v1/models":
            return httpx.Response(200, json=CATALOG)
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        if path.startswith("/evm/providers/"):
            return httpx.Response(200, json=provider_record(3, "aa" * 32))
        if path == "/v1/jobs" and request.method == "POST":
            body = req_body(request)
            if "auth_sig" not in body:      # phase 1: terms-only -> 402 challenge
                return httpx.Response(402, json=quote_body(body))
            submitted.append(body["job_id"])
        return httpx.Response(wire_status, json={"error": {
            "type": "t", "message": "upstream failed"}})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=3,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.APIStatusError):
        client.responses.create(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi",
                                extra_body={"vorq": {"sla": "1h", "provider": 3}})
    assert len(submitted) == 1


@needs_wallet
def test_an_idempotent_read_is_still_retried():
    """Suppressing retries is scoped to the routes that create something.

    Without this the previous test would pass for the wrong reason — a blanket
    ``x-should-retry: false`` on every error would satisfy it while throwing away
    the retry behaviour the status fidelity was restored for.
    """
    openai = pytest.importorskip("openai")
    from vorq import sealing_http_client

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "n1", "chain_id": 84532})
        if path == "/auth/session":
            return httpx.Response(200, json={"token": "t", "expires_at": 9999999999})
        attempts.append(path)
        return httpx.Response(503, json={"error": {"type": "t", "message": "unavailable"}})

    client = openai.OpenAI(
        base_url="http://coordinator.test/v1", api_key="unused", max_retries=2,
        http_client=sealing_http_client(base_url="http://coordinator.test",
                                        inner_transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.InternalServerError):
        client.responses.retrieve("0xabc")
    assert len(attempts) == 3      # the initial call plus two retries

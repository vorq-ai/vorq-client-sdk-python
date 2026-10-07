import base64
import hashlib
import json as json_
import os
import time
from decimal import Decimal

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from nacl.encoding import HexEncoder
from nacl.public import PrivateKey

from vorq import Client
from vorq._container import (
    INLINE_MAX_BYTES,
    commitment_of,
    derive_dek,
    open_dek,
    split_container,
)
from vorq._crypto import (
    ORDER_TYPES,
    SealedBoxCipher,
    WalletSigner,
    derive_result_cipher,
    vorq_domain,
)
from vorq._handles import JobHandle
from vorq._results import MediaResult, TextResult
from vorq._sla import poll_interval, sla_seconds
from vorq import _client as _client_module
from vorq._client import UPLOAD_MAX_TIMEOUT_S
from vorq._client import MAX_SUBMIT_ATTEMPTS
from vorq._money import format_usd, parse_usd
from vorq._terms import ChainContext, order_domain
from vorq.errors import (
    EscrowKeyUnverified,
    JobFailed,
    NotFoundError,
    ResultIntegrityError,
    ValidationError,
    VorqError,
    WaitTimeout,
)

from .conftest import fake_cid, json_keys, json_response, make_client, multipart_parts, req_body

BOX_PUBLIC = (
    PrivateKey(os.environ["VORQ_RECIPIENT_KEY"].encode(), encoder=HexEncoder)
    .public_key.encode(HexEncoder)
    .decode()
)

#: `GET /evm/chain`, in the node's own shape (`src/api/routes/relay.ts`). All
#: four contracts, and the addresses are the devnet deployment `signing-v3.json`
#: was generated against, so a body here and a vector there name one chain.
CHAIN = {
    "chain_id": 84532,
    "contracts": {
        "job_registry": "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        "provider_registry": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "ask_registry": "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
    "decimals": 6,
    "token_domain": {"name": "USDC", "version": "2"},
    # On the body and deliberately unread by this SDK (Q19).
    "head_block": 128,
    "block_time_ms": 1000,
    "fee_bps": 100,
}
CTX = ChainContext.from_wire(CHAIN)

#: `GET /v1/models`, the node's OpenAI-shaped catalog with the numeric id the
#: order actually signs beside each name.
_MODEL_IDS = {
    "deepseek-ai/deepseek-v4-pro:fp8": 1,
    "black-forest-labs/flux-2-dev:fp8": 2,
    "m": 3,
    "deepseek-ai/e2ee-deepseek-v4-pro:fp8": 4,
    "black-forest-labs/flux-2-pro": 5,
}
CATALOG = {
    "object": "list",
    "data": [
        {"id": name, "object": "model", "owned_by": "vorq",
         "vorq": {"model_id": mid, "enabled": True}}
        for name, mid in _MODEL_IDS.items()
    ],
    "as_of_block": 128,
}


def market_candidate(provider_id: int) -> dict:
    """The candidate a market probe names: provider N at USD rates 0.001·N / 0.002·N."""
    return {"provider_id": provider_id, "box_key": BOX_PUBLIC,
            "rate_in": format_usd(1000 * provider_id, 6),
            "rate_out": format_usd(2000 * provider_id, 6)}


def is_probe(body: dict) -> bool:
    """A market probe: the one `POST /v1/jobs` body that names no rate."""
    return "rate_in" not in body and "rate_out" not in body


def within_ceilings(candidate: dict, probe: dict) -> bool:
    """Whether a candidate's ask is at or under the ceilings a probe names."""
    return all(
        Decimal(candidate[side]) <= Decimal(probe[f"max_{side}"])
        for side in ("rate_in", "rate_out") if f"max_{side}" in probe
    )


def provider_record(provider_id: int, box_key: str = BOX_PUBLIC, **over) -> dict:
    """`GET /evm/providers/{id}` — the registry record, as the index projects it."""
    record = {
        "provider_id": provider_id, "provider": provider_id,
        "operator": "0x1111111111111111111111111111111111111111",
        "box_key": box_key, "evidence": None, "listed": True, "reputation": 1000,
        "allow_all_models": True, "allowed_models": [], "capacity": 8, "active_jobs": 0,
        "as_of_block": 128,
    }
    record.update(over)
    return record


def quote_body(
    body: dict,
    *,
    amount: str = "0.00021",
    candidates: list[dict] | None = None,
    gas_fee: str = "0",
    fee_bps: int | None = 100,
) -> dict:
    """The `402` body, and the `409` body when the quote goes stale — one shape.

    Built from the *request*, because every member of the authorization is about
    the order that was signed: the nonce is the job id and the window closes one
    second past the order's own `expires_at`. A fixture that invented them would
    let a client that ignores the quote pass.

    A market probe (no rates, unsigned) is answered with ``candidates`` alone:
    the given list, or the pinned provider (provider 1 when unpinned), less any
    whose ask is above a ceiling the probe names. On a
    signed challenge ``candidates`` is added only when given.
    ``fee_bps=None`` leaves it off too, which is a node that cannot be priced.
    Money is USD strings; the authorization's ``value`` is the atomic integer signed.
    """
    v = body  # flat; a form body (the 409 path) carries strings
    if is_probe(v):
        if candidates is None:
            candidates = [market_candidate(int(v.get("designated") or 0) or 1)]
        return {"candidates": [c for c in candidates if within_ceilings(c, v)]}
    extra = {} if candidates is None else {"candidates": candidates}
    fee = {} if fee_bps is None else {"fee_bps": fee_bps}
    return {
        **extra,
        "quote": {
            "cap": "0.00021", "gas_fee": gas_fee, "amount": amount, **fee,
            "authorization": {
                "domain": {"name": "USDC", "version": "2", "chainId": CHAIN["chain_id"],
                           "verifyingContract": CHAIN["contracts"]["usdc"]},
                "to": CHAIN["contracts"]["job_registry"],
                "value": parse_usd(amount, 6),
                "valid_after": 0,
                "valid_before": int(v["expires_at"]) + 1,
                "nonce": v["job_id"],
            },
        },
        "accepts": [{"scheme": "eip3009", "network": f"eip155:{CHAIN['chain_id']}"}],
    }


#: An address this deployment names nowhere.
STRANGER = "0x" + "ee" * 20


def quote_with(body: dict, **authorization) -> dict:
    """The honest `402` for this order, with its authorization block patched."""
    honest = quote_body(body)
    return {**honest, "quote": {**honest["quote"], "authorization": {
        **honest["quote"]["authorization"], **authorization,
    }}}


def quote_with_domain(body: dict, **domain) -> dict:
    """The same, patching the authorization's *domain* rather than its members."""
    honest = quote_body(body)["quote"]["authorization"]["domain"]
    return quote_with(body, domain={**honest, **domain})


class _OpaqueSigner:
    """A ``Signer`` whose private key the SDK never sees — a contract wallet or a
    KMS-backed signer. Nothing here to derive a result cipher from."""

    address = "0x1111111111111111111111111111111111111111"

    def sign_order_v2(self, terms, ctx) -> str:
        raise AssertionError("nothing should be signed in these tests")

    def sign_cancel(self, job_id, issued_at, ctx) -> str:
        raise AssertionError("nothing should be signed in these tests")

    def sign_payment_authorization(self, **kwargs) -> str:
        raise AssertionError("nothing should be signed in these tests")

    def sign_nonce(self, nonce: str, chain_id: int) -> str:
        raise AssertionError("nothing should be signed in these tests")


# The coordinator's escrow key, served on GET /key with the evidence that binds
# it. An open order seals its payload to this one instead of to a provider's box
# key; nothing else about the two submissions differs — but this one is only
# usable once its evidence verifies, which is the whole of Q17.
ESCROW_SECRET = PrivateKey.generate()
ESCROW_PUBLIC = ESCROW_SECRET.public_key.encode(HexEncoder).decode()

#: The coordinator's mock image, distinct from the provider mock's. The two are
#: separate measurements because they are separate trust domains.
ESCROW_MEASUREMENT = hashlib.sha256(b"vorq-mock-coordinator-image-v1").hexdigest()
ESCROW_ALLOWLIST = {
    "entries": [{"kind": "image", "measurement": ESCROW_MEASUREMENT,
                 "status": "active", "mock": True}],
    "as_of_block": 128,
}


def escrow_announcement(*, key: str | None = None, now: float = 1_790_000_000.0,
                        **over) -> dict:
    """`GET /key` with evidence that binds the key it announces.

    ``report_data`` is `sha256(escrow_pk32 ‖ utf8("vorq-coordinator-escrow-v1"))`
    — the escrow's own binding, computed here from the same two inputs the node
    computes it from rather than copied off the implementation.
    """
    from vorq.verify import escrow_report_data

    pk = key or ESCROW_PUBLIC
    body = {
        "escrow_public_key": pk,
        "issued_at": int(now),
        "evidence": {
            "type": "mock-coordinator-v1",
            "measurement": ESCROW_MEASUREMENT,
            "report_data": escrow_report_data(pk),
            "debug": False,
            "tcb": {"svn": 1},
            "release": 1,
            "quote": "bW9jaw==",
        },
    }
    body.update(over)
    return body


def escrow_verifier(transport: httpx.MockTransport | None = None, *,
                    now: float = 1_790_000_000.0):
    """A mock-mode verifier over the escrow allowlist, with a pinned wall clock.

    The clock is pinned because `GET /key`'s `issued_at` is checked to ±600 s
    (Q23): a fixture stamped with a literal would age out of every run made more
    than ten minutes after it was written.
    """
    from vorq.verify import Verifier

    def allowlist(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json=ESCROW_ALLOWLIST)
        raise AssertionError(f"the verifier asked for {request.url.path}")

    return Verifier(
        "http://node", mode="mock",
        transport=transport or httpx.MockTransport(allowlist),
        wall_clock=lambda: now,
    )


def _open_container(container: bytes | str, secret_key=None, *, owner: str | None = None) -> dict:
    """Open a container as its recipient — yields the ``vorq-env-v1`` envelope.

    The recipient path in full, and the reason this is not a one-liner: split at
    the fixed offsets, unseal the 80-byte wrap to recover the **seed**, derive the
    DEK from the seed and the job's owner, then open the bulk under it (Q3). A
    helper that treated the unsealed bytes as the key would open nothing, which
    is exactly the failure a client sealing the DEK directly would produce on
    every provider in the network.
    """
    from nacl.public import SealedBox

    # Raw bytes off a single-job form, base64 off a batch line.
    raw = container if isinstance(container, bytes) else base64.b64decode(container)
    wrap, ciphertext = split_container(raw)
    sk = secret_key if secret_key is not None else PrivateKey(
        os.environ["VORQ_RECIPIENT_KEY"].encode(), encoder=HexEncoder
    )
    seed = SealedBox(sk).decrypt(wrap)
    return json_.loads(open_dek(ciphertext, derive_dek(seed, owner or WalletSigner().address)))


def _open_complete(complete: dict, secret_key=None) -> dict:
    """Open a complete submission's container, deriving under **its own** owner.

    The terms name the owner and the DEK derives under it, so the two have to
    agree or the payload is undecryptable by construction. Reading the owner off
    the body rather than restating it is what makes that agreement an assertion.
    """
    return _open_container(
        complete["container"], secret_key, owner=complete["owner"]
    )


def _signed_c(complete: dict) -> bytes:
    """The commitment the terms carry, as bytes."""
    return bytes.fromhex(complete["c"][2:])

def node_job(**over) -> dict:
    """`GET /v1/jobs/{id}` **exactly as the coordinator builds it**.

    Transcribed key-for-key from `clientJob` in
    `vorq-coordinator-node/src/api/routes/jobs.ts`, which is the authority. Every
    fixture in this file that stands in for a job row goes through here, so a
    field the node does not send cannot be read by accident — which is precisely
    how three mappings in this SDK came to read fields that never arrive.

    Note what is **not** on it: no `error` object (the end cause lives in
    `vorq.ended_because`), no `sla` string (`vorq.sla_secs` is an integer), no
    `provider` (it is `vorq.provider_id`), no `created_at`, no `output`.
    """
    vorq = {
        "job_id": "job_txt", "owner": "0x" + "11" * 20,
        "model_id": 1, "sla_secs": 3600,
        "rate_in": "0.05", "rate_out": "0.15", "gas_fee": "0.03", "fee": "0",
        "units_in": 12, "units_out": 4096,
        "designated": 0, "provider_id": 7,
        "expires_at": 1800000000, "task_cid": "bafy-task",
        "completion_tok": 0, "state": 2, "ended_because": 1,
    }
    vorq.update(over.pop("vorq", {}))
    job = {
        "id": "job_txt", "object": "job",
        "model": "deepseek-ai/deepseek-v4-pro:fp8",
        "status": "completed", "in_progress_at": None,
        "result_cid": None, "vorq": vorq, "as_of_block": 128,
    }
    job.update(over)
    return job


# A settled job names its result, so the fixture carries both the bytes and the
# name — and a handler that serves this job must serve those bytes at
# `{gateway}/ipfs/{cid}`, which is the only place the client reads them from.
# `output` stays on the row as the coordinator's convenience copy; nothing reads
# it, and `test_a_completed_job_that_names_no_result_is_refused` is what keeps it
# that way.
COMPLETED_TEXT_BYTES = json_.dumps({
    "object": "response",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi there"}]}],
    "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
}).encode()

COMPLETED_TEXT_JOB = {
    "id": "job_txt",
    "object": "job",
    "model": "deepseek-ai/deepseek-v4-pro:fp8",
    "status": "completed",
    "result_cid": fake_cid(COMPLETED_TEXT_BYTES),
    "output": json_.loads(COMPLETED_TEXT_BYTES),
    "vorq": {"gas_fee": "0.03", "fee": "0.000001", "sla_secs": 3600, "rate_in": "0.05", "rate_out": "0.15",
             "provider_id": 7, "state": 2, "ended_because": 1},
}


def completed_text_response(request):
    """The settled job, plus the bytes it names — one handler for both reads."""
    if request.url.path.startswith("/ipfs/"):
        return httpx.Response(200, content=COMPLETED_TEXT_BYTES)
    return json_response(200, COMPLETED_TEXT_JOB)


def queued_job(job_id="job_txt", sla="24h"):
    return {
        "id": job_id,
        "object": "job",
        "model": "black-forest-labs/flux-2-dev:fp8",
        "status": "queued",
        "queue_position": 1,
        "vorq": {"gas_fee": "0.03", "fee": "0", "sla_secs": sla_seconds(sla), "rate_out": "0.02", "provider_id": 0,
                 "state": 0, "ended_because": 0},
    }


class TestSubmit:
    """Submissions are always sealed, so ``submit`` here runs the 402-challenge
    loop (see ``auth_router`` / ``quote_body`` above); what a plain
    plaintext-submit test used to read off the request body directly is now
    checked off the challenge-phase terms and by decrypting the sealed payload.
    """

    async def test_wraps_string_input_and_normalizes_sla(self):
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["challenge_body"] = body
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        handle = await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello",
                                     sla="batch", provider=1)
        assert isinstance(handle, JobHandle)
        assert handle.id == "job_txt"
        assert "container" not in captured["challenge_body"]  # phase 1 carries terms only
        # "batch" -> "24h" -> 86400 seconds, which is what the order actually signs.
        assert captured["challenge_body"]["sla_secs"] == 86400
        assert _open_complete(captured["complete"])["input"] == {"input": "hello"}
        await client.aclose()

    async def test_custom_id_travels_sealed_and_never_on_the_terms(self):
        """A caller's own label for a line rides inside the container, not beside it.

        `custom_id` is client-chosen text. On the terms it would be readable by the
        coordinator and, once a batch manifest is pinned, by anyone who fetches the CID —
        so it goes where the payload goes. The provider reads it back out and stamps it on
        the result, which is how a caller correlates a line it did not submit in this process.
        """
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello",
                            sla="batch", provider=1, custom_id="req-42")

        assert _open_complete(captured["complete"])["custom_id"] == "req-42"
        # and nothing about it is on the cleartext terms
        assert "custom_id" not in json_keys(captured["complete"])
        await client.aclose()

    async def test_an_unlabelled_submission_carries_no_custom_id_key(self):
        """Absent, not null: an envelope is canonical JSON inside a commitment, so a key
        that means nothing must not be there at all."""
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello",
                            sla="batch", provider=1)

        assert "custom_id" not in _open_complete(captured["complete"])
        await client.aclose()

    async def test_dict_input_passthrough_and_rates(self):
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["challenge_body"] = body
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(
            model="black-forest-labs/flux-2-dev:fp8",
            input={"prompt": "a cat", "seed": 7},
            sla="1h",
            max_rate_out="0.03",
            provider=1,
        )
        terms = captured["challenge_body"]
        assert terms["sla_secs"] == 3600
        # Money goes out as a canonical USD string; every other integer as a JSON number.
        # The order signs provider 1's ask, which is under the 0.03 ceiling.
        assert terms["rate_out"] == "0.002"
        assert terms["model_id"] == 2          # resolved from the catalog, not the name
        assert _open_complete(captured["complete"])["input"] == {
            "prompt": "a cat", "seed": 7
        }

    async def test_no_rates_probes_the_market_and_bids_the_first_candidates_ask(self):
        challenges = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="hi")
        probe, terms = challenges
        assert probe == {"model_id": 3, "sla_secs": 86400, "units_in": probe["units_in"],
                         "units_out": probe["units_out"], "designated": 0}
        # market_candidate(1): the node's first pick, at its own ask.
        assert (terms["rate_in"], terms["rate_out"]) == ("0.001", "0.002")
        assert terms["designated"] == 1
        assert terms["sla_secs"] == 86400
        await client.aclose()

    async def test_no_rates_takes_the_nodes_first_candidate_as_ranked(self):
        """The node ranks; the client never re-sorts the list it is handed."""
        captured = {}
        ranked = [market_candidate(5), market_candidate(4)]

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["terms"] = body
                return json_response(402, quote_body(body, candidates=ranked))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="hi")
        terms = captured["terms"]
        assert (terms["rate_in"], terms["rate_out"], terms["designated"]) == ("0.005", "0.01", 5)
        await client.aclose()

    async def test_no_rates_with_a_provider_takes_that_providers_ask(self):
        challenges = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="1h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="hi", sla="1h", provider=3)
        probe, terms = challenges
        assert "signature" not in probe and "rate_in" not in probe
        assert probe["designated"] == 3
        assert (terms["rate_in"], terms["rate_out"], terms["designated"]) == ("0.003", "0.006", 3)
        await client.aclose()

    async def test_no_rates_and_no_live_ask_raises_before_signing_a_payment(self):
        challenges = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" in body:
                raise AssertionError("nothing should be posted")
            challenges.append(body)
            return json_response(402, quote_body(body, candidates=[]))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(ValidationError, match="no provider is serving m in the 24h window"):
            await client.submit(model="m", input="hi")
        assert len(challenges) == 1
        await client.aclose()

    @pytest.mark.parametrize("rate", [30000, 0.03, True])
    async def test_a_rate_that_is_not_a_usd_string_or_decimal_is_refused(self, rate):
        """A rate is USD per 1M units. An `int` is ambiguous between USD and atomic
        units and a `float` cannot carry money exactly, so both are refused — naming
        the unit — before anything reaches the network."""
        client = Client(transport=httpx.MockTransport(auth_router(
            lambda r: json_response(500, {})
        )))
        with pytest.raises(ValidationError, match=r'USD per 1M units, e\.g\. "0\.05"'):
            await client.submit(model="m", input="x", sla="1h", max_rate_out=rate, provider=1)
        await client.aclose()

    async def test_a_rate_finer_than_the_token_is_refused_rather_than_rounded(self):
        """Seven fraction digits at six decimals: rounding it would sign a bid the
        caller never made."""
        client = Client(transport=httpx.MockTransport(auth_router(
            lambda r: json_response(500, {})
        )))
        with pytest.raises(ValidationError, match="fraction digits"):
            await client.submit(model="m", input="x", sla="1h", max_rate_out="0.0000001", provider=1)
        await client.aclose()

    async def test_a_decimal_rate_is_signed_atomic_and_sent_as_usd(self):
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["probe" if is_probe(body) else "challenge"] = body
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="1h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="x", sla="1h", max_rate_in=Decimal("0.050"),
                            max_rate_out="340282366920938463463374607431768.211455", provider=1)
        assert captured["probe"]["max_rate_in"] == "0.05"
        assert captured["probe"]["max_rate_out"] == "340282366920938463463374607431768.211455"
        # What is signed is the ask the probe named, never the ceiling.
        assert captured["challenge"]["rate_in"] == "0.001"
        assert captured["challenge"]["rate_out"] == "0.002"
        await client.aclose()

    async def test_a_model_the_catalog_does_not_carry_is_refused(self):
        """The order signs a uint32 id. An unknown name is an error, not a zero."""
        client = Client(transport=httpx.MockTransport(auth_router(
            lambda r: json_response(500, {})
        )))
        with pytest.raises(ValidationError, match="no numeric model_id"):
            await client.submit(model="not-in-the-catalog", input="x", sla="1h",
                                validate_params=False, provider=1)
        await client.aclose()
        await client.aclose()

    async def test_submission_is_not_retried(self):
        calls = {"n": 0}

        def jobs(request):
            calls["n"] += 1
            return json_response(500, {"error": {"message": "boom", "type": "internal_error"}}, retryable=True)

        client = Client(transport=httpx.MockTransport(auth_router(jobs)), max_retries=3)
        with pytest.raises(Exception):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="x", sla="async",
                                provider=1)
        assert calls["n"] == 1  # never retry a submission
        await client.aclose()

    async def test_no_signer_or_cipher_raises_sealed_required(self, monkeypatch):
        monkeypatch.delenv("VORQ_CIPHER_KEY", raising=False)
        client = make_client(lambda request: (_ for _ in ()).throw(
            AssertionError(f"unexpected request: {request.url.path}")
        ))
        with pytest.raises(ValidationError, match="submissions must be sealed"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            provider=1)
        await client.aclose()

    async def test_env_wallet_derives_its_own_result_cipher(self, monkeypatch):
        # No VORQ_CIPHER_KEY and no cipher= is not a misconfiguration: a wallet
        # derives its result cipher, and re-derives the same one every time (D11).
        monkeypatch.delenv("VORQ_CIPHER_KEY", raising=False)
        client = Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
            AssertionError(f"unexpected request: {request.url.path}")
        )))
        assert client.signer is not None  # built from the env wallet
        assert client.cipher is not None
        assert client.cipher.public_key == derive_result_cipher(WalletSigner()).public_key
        await client.aclose()

    async def test_non_wallet_signer_without_cipher_raises_sealed_required(self, monkeypatch):
        # A signer whose key the SDK cannot reach (contract wallet, KMS) has
        # nothing to derive from — that client must be handed a cipher.
        monkeypatch.delenv("VORQ_CIPHER_KEY", raising=False)
        client = Client(
            signer=_OpaqueSigner(),
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                AssertionError(f"unexpected request: {request.url.path}")
            )),
        )
        assert client.cipher is None  # nothing to derive from, nothing in the env
        with pytest.raises(ValidationError, match="submissions must be sealed"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            provider=1)
        await client.aclose()


def auth_router(job_handler, *, token="vorq_sess_minted", expires_at=9999999999, seen=None,
                box_key=BOX_PUBLIC, key_body=None):
    """A handler that answers everything a submit reads, then delegates the rest.

    The reads a submission makes before it posts anything are the chain context
    (`GET /evm/chain`, once per client), the model catalog (`GET /v1/models`, for
    the numeric `model_id` the order signs), and then either the designated
    provider's registry record or the coordinator's escrow key.
    """

    def handler(request):
        path = request.url.path
        if path == "/evm/chain":
            if seen is not None:
                seen["chain_calls"] = seen.get("chain_calls", 0) + 1
            return json_response(200, CHAIN)
        if path == "/key":
            if seen is not None:
                seen["key_calls"] = seen.get("key_calls", 0) + 1
            return json_response(200, key_body or escrow_announcement())
        if path == "/evm/allowlist":
            return json_response(200, ESCROW_ALLOWLIST)
        if path.startswith("/evm/providers/"):
            if seen is not None:
                seen["provider_calls"] = seen.get("provider_calls", 0) + 1
            return json_response(200, provider_record(int(path.rsplit("/", 1)[1]), box_key))
        if path == "/auth/nonce":
            if seen is not None:
                seen["nonce_addr"] = request.url.params.get("address")
                seen["nonce_calls"] = seen.get("nonce_calls", 0) + 1
            return json_response(200, {"nonce": "n0nce", "expires_at": expires_at, "chain_id": 84532})
        if path == "/auth/session":
            if seen is not None:
                seen["session_body"] = req_body(request)
            return json_response(200, {"token": token, "expires_at": expires_at})
        if path == "/v1/models":
            if seen is not None:
                seen["model_calls"] = seen.get("model_calls", 0) + 1
            return json_response(200, CATALOG)
        return job_handler(request)

    return handler


class TestAuth:
    async def test_no_wallet_and_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("VORQ_WALLET_KEY", raising=False)
        with pytest.raises(ValueError):
            Client()

    async def test_env_wallet_mints_session_under_the_hood(self):
        # VORQ_WALLET_KEY is present (loaded from .env.test) — Client() just works.
        seen = {}

        def jobs(request):
            seen["job_auth"] = request.headers.get("authorization")
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        transport = httpx.MockTransport(auth_router(jobs, seen=seen))
        client = Client(transport=transport)
        assert client.signer is not None  # built from the env wallet
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            provider=1)

        assert seen["nonce_addr"] == client.signer.address
        assert seen["session_body"]["address"] == client.signer.address
        # the minted token, not the raw signature, becomes the bearer
        assert seen["job_auth"] == "Bearer vorq_sess_minted"
        await client.aclose()

    async def test_from_session_token_skips_handshake(self):
        seen = {"paths": []}

        def handler(request):
            seen["paths"].append(request.url.path)
            if request.url.path == "/v1/models":
                return json_response(200, CATALOG)
            if request.url.path == "/evm/chain":
                return json_response(200, CHAIN)
            if request.url.path.startswith("/evm/providers/"):
                return json_response(200, provider_record(1))
            seen["auth"] = request.headers.get("authorization")
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        transport = httpx.MockTransport(handler)
        # A signer is required to submit, but a pre-minted token with no expiry
        # still short-circuits the /auth handshake regardless.
        client = Client.from_session_token("vorq_test", transport=transport, signer=WalletSigner())
        await client.submit(model="m", input="a", sla="batch", provider=1)
        # No /auth round-trip, and the payload crosses the wire once: the reads a
        # submission needs, the market probe, the challenge, the funded submission.
        assert seen["paths"] == [
            "/v1/models",                              # the param schema
            "/evm/chain",                              # the deployment, once per client
            "/v1/models",                              # the model id
            "/v1/jobs", "/v1/jobs", "/v1/jobs",        # probe, challenge, funded post
        ]
        assert seen["auth"] == "Bearer vorq_test"
        await client.aclose()

    async def test_session_minted_once_then_cached(self, no_sleep):
        seen = {}

        def jobs(request):
            if request.method == "GET":
                return json_response(200, queued_job(sla="24h"))
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        transport = httpx.MockTransport(auth_router(jobs, seen=seen))
        client = Client(transport=transport)
        await client.submit(model="m", input="a", sla="batch", provider=1)
        await client.job("job_txt").status()
        assert seen["nonce_calls"] == 1  # cached after first mint
        await client.aclose()

    async def test_session_rotates_before_expiry(self, monkeypatch):
        clock = {"t": 100.0}
        monkeypatch.setattr("vorq._client.time.time", lambda: clock["t"])
        seen = {}

        def jobs(request):
            return json_response(200, queued_job(sla="24h"))

        # token minted at t=100 expiring at t=1000 (skew 60 => valid until 940)
        transport = httpx.MockTransport(auth_router(jobs, expires_at=1000, seen=seen))
        client = Client(transport=transport)
        await client.job("job_txt").status()
        assert seen["nonce_calls"] == 1
        clock["t"] = 950.0  # inside the refresh skew
        await client.job("job_txt").status()
        assert seen["nonce_calls"] == 2  # re-minted
        await client.aclose()

    async def test_401_triggers_remint_and_retry(self):
        state = {"jobs": 0}

        def jobs(request):
            state["jobs"] += 1
            if state["jobs"] == 1:
                return json_response(401, {"error": {"message": "expired", "type": "unauthorized"}})
            return json_response(200, queued_job(sla="24h"))

        seen = {}
        transport = httpx.MockTransport(auth_router(jobs, seen=seen))
        client = Client(transport=transport)
        status = await client.job("job_txt").status()
        assert status == "queued"
        assert state["jobs"] == 2  # retried after re-mint
        assert seen["nonce_calls"] == 2  # initial mint + re-mint on 401
        await client.aclose()

    async def test_token_only_client_without_signer_raises_sealed_required(self):
        # from_session_token, no signer — a cipher alone (auto-loaded from env)
        # is not enough; submissions need both.
        client = make_client(lambda request: (_ for _ in ()).throw(
            AssertionError(f"unexpected request: {request.url.path}")
        ))
        assert client.signer is None
        assert client.cipher is not None
        with pytest.raises(ValidationError, match="submissions must be sealed"):
            await client.submit(model="m", input="a", sla="batch")
        await client.aclose()

    async def test_cipher_autoloaded_from_env(self):
        from nacl.encoding import HexEncoder
        from nacl.public import PrivateKey

        client = Client.from_session_token("vorq_test")
        expected = (
            PrivateKey(os.environ["VORQ_CIPHER_KEY"].encode(), encoder=HexEncoder)
            .public_key.encode(HexEncoder)
            .decode()
        )
        assert client.cipher is not None
        assert client.cipher.public_key == expected
        await client.aclose()

    async def test_mint_session_token_helper(self):
        from vorq import mint_session_token

        def handler(request):
            if request.url.path == "/auth/nonce":
                return json_response(200, {"nonce": "n", "expires_at": 9999999999, "chain_id": 84532})
            return json_response(200, {"token": "vorq_sess_foropenai", "expires_at": 9999999999})

        token = mint_session_token(transport=httpx.MockTransport(handler))
        assert token == "vorq_sess_foropenai"


# --- encrypted 402-challenge path -------------------------------------------
#
# BOX_PUBLIC / CHAIN / CATALOG / quote_body are defined near the top of this
# module — shared with TestSubmit and TestAuth, which now also exercise the
# sealed flow (submissions are always sealed).

from vorq._crypto import RECEIVE_AUTHORIZATION_TYPES, content_job_id  # noqa: E402
from vorq._terms import payment_domain  # noqa: E402


class TestEncryptedSubmit:
    async def test_challenge_then_seal_then_complete(self):
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["challenge_body"] = body
                return json_response(402, quote_body(body))
            captured["complete_type"] = request.headers["content-type"]
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello", sla="batch",
                            max_rate_in="0.05", max_rate_out="0.15", provider=1)
        # Phase 1 carried terms only — no payload left the client.
        assert "container" not in captured["challenge_body"]
        # The complete submission is JSON too — the door never went back to a
        # multipart form, only the container's home on the body changed.
        assert captured["complete_type"].startswith("application/json")
        complete = captured["complete"]
        # The bytes ride the funded request as inline base64, and there is no
        # `input` object anywhere on the wire any more.
        assert "input" not in complete
        container = base64.b64decode(complete["container"])
        vorq = {k: v for k, v in complete.items() if k != "container"}
        assert vorq["designated"] == 1
        assert vorq["job_id"].startswith("0x")
        # The terms carry `c` and no CID: the client cannot know the name. Every
        # absence here is asserted on **parsed keys** and never on a substring of
        # the body — a container is base64, and base64 of random bytes contains
        # most short strings often enough to fail a run nobody can reproduce.
        assert commitment_of(container) == _signed_c(complete)
        assert vorq["job_id"] == content_job_id(client.signer.address, _signed_c(complete))
        assert not [k for k in json_keys(complete) if "cid" in k.lower()]
        # No bare key of any kind: the secret exists on the wire only inside the
        # 80-byte wrap, and what is in there is a seed rather than the DEK.
        assert not [k for k in json_keys(complete) if "dek" in k.lower()]
        # The result key rides inside the sealed envelope; the owner is on the
        # terms, because the node checks job_id against keccak(owner ‖ c).
        assert "result_key" not in vorq and "enc" not in vorq
        assert vorq["owner"] == client.signer.address
        envelope = _open_complete(complete)
        assert envelope["v"] == "vorq-env-v1"
        assert envelope["owner"] == client.signer.address
        assert envelope["result_key"] == client.cipher.public_key
        # The order signature recovers to the wallet over the contract's own
        # `Order` type, on the JobRegistry's domain.
        message = {
            "c": _signed_c(complete),
            "modelId": int(vorq["model_id"]), "slaSecs": int(vorq["sla_secs"]),
            "rateIn": parse_usd(vorq["rate_in"], 6), "rateOut": parse_usd(vorq["rate_out"], 6),
            "unitsIn": int(vorq["units_in"]), "unitsOut": int(vorq["units_out"]),
            "designated": int(vorq["designated"]), "expiresAt": int(vorq["expires_at"]),
        }
        signable = encode_typed_data(order_domain(CTX), ORDER_TYPES, message)
        assert Account.recover_message(signable, signature=vorq["signature"]) == client.signer.address
        # The payment is one EIP-3009 authorization and an echoed amount, flat
        # beside the terms — no authorization object the node would have to trust.
        assert set(complete) == set(captured["challenge_body"]) | {
            "auth_sig", "amount", "container",
        }
        assert complete["amount"] == "0.00021"
        authorization = {
            "from": client.signer.address,
            "to": CHAIN["contracts"]["job_registry"],
            "value": 210,
            "validAfter": 0,
            "validBefore": int(vorq["expires_at"]) + 1,
            "nonce": bytes.fromhex(vorq["job_id"][2:]),
        }
        signable = encode_typed_data(
            payment_domain(CTX), RECEIVE_AUTHORIZATION_TYPES, authorization
        )
        assert Account.recover_message(
            signable, signature=complete["auth_sig"]
        ) == client.signer.address
        await client.aclose()

    async def test_the_expiry_is_clamped_to_the_chains_own_ceiling(self):
        """`24h` + the settlement margin lands past `now + 86400`, which the node
        refuses at the door. Clamping here makes the ordinary long-window
        submission postable rather than a 400."""
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["challenge_body"] = body
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        import time as _time
        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="hi", sla="batch", provider=1)
        expires = captured["challenge_body"]["expires_at"]
        assert expires - int(_time.time()) <= 86400
        await client.aclose()

    async def test_the_container_crosses_the_wire_exactly_once(self):
        """The bytes are uploaded once and only once for one accepted submission."""
        posts = []

        def jobs(request):
            body = req_body(request)
            posts.append(body)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15", provider=1)
        probe, challenge, complete = posts
        assert "container" not in probe and "container" not in challenge
        assert "container" in complete
        await client.aclose()

    async def test_the_challenge_is_the_flat_order_and_nothing_else(self):
        """No `vorq` envelope, no `payment` block: the twelve order fields at the
        body's top level, as JSON, and the node reads a form's fields the same way."""
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                captured["challenge"] = body
                captured["challenge_type"] = request.headers["content-type"]
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15", provider=1)
        assert captured["challenge_type"].startswith("application/json")
        assert set(captured["challenge"]) == {
            "c", "owner", "job_id", "model_id", "sla_secs", "rate_in", "rate_out",
            "units_in", "units_out", "designated", "expires_at", "signature",
        }
        await client.aclose()

    async def test_a_container_over_the_inline_cap_is_uploaded_first_and_referenced_by_cid(self):
        """There is no cap on a container's size any more: past `INLINE_MAX_BYTES`
        it is filed through `POST /v1/files` (`purpose=input`, `purpose` ahead of
        the `file` part) instead of riding inline, and the job body that follows
        carries `container_cid` — the upload's `vorq.cid` — and no `container`."""
        captured = {}

        def jobs(request):
            if request.url.path == "/v1/files":
                captured["upload_type"] = request.headers.get("content-type", "")
                captured["upload_parts"] = multipart_parts(request)
                raw = captured["upload_parts"][-1][2]
                captured["cid"] = fake_cid(raw)
                return json_response(200, {
                    "id": "file_container", "object": "file", "purpose": "input",
                    "bytes": len(raw), "expires_at": 1_800_000_000,
                    "vorq": {"cid": captured["cid"]},
                })
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        prompt = "x" * (INLINE_MAX_BYTES + 1024)
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input=prompt, sla="batch",
                            max_rate_out="0.15", provider=1)

        assert captured["upload_type"].startswith("multipart/form-data")
        names = [name for name, _, _ in captured["upload_parts"]]
        assert names == ["purpose", "file"]
        purpose_name, purpose_filename, purpose_body = captured["upload_parts"][0]
        assert purpose_filename is None and purpose_body == b"input"
        _, filename, raw = captured["upload_parts"][1]
        assert filename is not None and len(raw) > INLINE_MAX_BYTES

        complete = captured["complete"]
        assert "container" not in complete
        assert complete["container_cid"] == captured["cid"]
        # Raw bytes, not base64: the commitment is over exactly what was filed.
        assert commitment_of(raw) == _signed_c(complete)
        assert _open_container(raw, owner=complete["owner"])["input"] == {"input": prompt}
        await client.aclose()

    async def test_the_inline_upload_first_split_is_pinned_exactly_at_inline_max_bytes(self):
        """`INLINE_MAX_BYTES` is an inclusive bound: a container of exactly that
        many bytes rides inline, and one byte more is filed first.

        The oversize tests around this one all use `INLINE_MAX_BYTES + 1024`, so
        none of them can tell `<=` from `<`. This drives the boundary itself. The
        container is `overhead + len(input)` — the version byte, the seed wrap and
        the secret box around a canonical JSON envelope, plus the "x"s, which need
        no JSON escaping and so add exactly one byte each — so the overhead is
        calibrated once against an empty input and the two lengths are solved for
        rather than guessed.
        """
        probe = {}

        def calibrate(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            probe["container"] = body["container"]
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(calibrate)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="", sla="batch",
                            max_rate_out="0.15", provider=1)
        await client.aclose()
        overhead = len(base64.b64decode(probe["container"]))

        def router(state):
            def jobs(request):
                if request.url.path == "/v1/files":
                    state["uploads"] += 1
                    raw = multipart_parts(request)[-1][2]
                    state["filed"] = len(raw)
                    return json_response(200, {
                        "id": "file_container", "object": "file", "purpose": "input",
                        "bytes": len(raw), "expires_at": 1_800_000_000,
                        "vorq": {"cid": fake_cid(raw)},
                    })
                body = req_body(request)
                if "auth_sig" not in body:
                    return json_response(402, quote_body(body))
                state["complete"] = body
                return json_response(200, queued_job(sla="24h"))
            return jobs

        # Exactly at the bound: inline, and the files door is never touched.
        at_bound = {"uploads": 0}
        client = Client(transport=httpx.MockTransport(auth_router(router(at_bound))))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8",
                            input="x" * (INLINE_MAX_BYTES - overhead), sla="batch",
                            max_rate_out="0.15", provider=1)
        await client.aclose()
        assert at_bound["uploads"] == 0
        assert len(base64.b64decode(at_bound["complete"]["container"])) == INLINE_MAX_BYTES
        assert "container_cid" not in at_bound["complete"]

        # One byte over: filed first, and the job names it by cid.
        over = {"uploads": 0}
        client = Client(transport=httpx.MockTransport(auth_router(router(over))))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8",
                            input="x" * (INLINE_MAX_BYTES - overhead + 1), sla="batch",
                            max_rate_out="0.15", provider=1)
        await client.aclose()
        assert over["uploads"] == 1
        assert over["filed"] == INLINE_MAX_BYTES + 1
        assert "container" not in over["complete"]
        assert over["complete"]["container_cid"]

    async def test_an_upload_answer_without_a_cid_is_refused_before_the_job_is_posted(self):
        """`vorq.cid` is the whole reference, and this client reads the field
        defensively everywhere else. Missing, it used to reach the job body as
        `container_cid: None`, which the node answers `container_required` to —
        an answer that reads as "this client forgot the container" and sends the
        caller looking in the wrong place."""
        posts = []

        def jobs(request):
            if request.url.path == "/v1/files":
                raw = multipart_parts(request)[-1][2]
                return json_response(200, {
                    "id": "file_container", "object": "file", "purpose": "input",
                    "bytes": len(raw), "expires_at": 1_800_000_000, "vorq": {},
                })
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        prompt = "x" * (INLINE_MAX_BYTES + 1024)
        with pytest.raises(VorqError, match="no vorq.cid for file file_container"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input=prompt,
                                sla="batch", max_rate_out="0.15", provider=1)
        assert posts == []  # nothing was posted against a name that does not exist
        await client.aclose()

    async def test_a_dropped_connection_after_an_upload_re_posts_without_uploading_again(self):
        """The upload happens once, right before the post. A connection dropped
        after it lands re-sends the same JSON body — `container_cid` included —
        rather than filing the container a second time."""
        state = {"uploads": 0, "posts": 0}

        def jobs(request):
            if request.url.path == "/v1/files":
                state["uploads"] += 1
                raw = multipart_parts(request)[-1][2]
                state["cid"] = fake_cid(raw)
                return json_response(200, {
                    "id": "file_container", "object": "file", "purpose": "input",
                    "bytes": len(raw), "expires_at": 1_800_000_000,
                    "vorq": {"cid": state["cid"]},
                })
            if request.method == "GET":
                return json_response(404, {"error": {"type": "not_found_error",
                                                     "message": "unknown job"}})
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            state["posts"] += 1
            if state["posts"] == 1:
                raise httpx.ReadTimeout("dropped before the bytes landed")
            state["second_body"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        prompt = "x" * (INLINE_MAX_BYTES + 1024)
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input=prompt, sla="batch",
                            max_rate_out="0.15", provider=1)
        assert state["uploads"] == 1  # filed once, not twice
        assert state["posts"] == 2  # posted, dropped, re-posted
        assert state["second_body"]["container_cid"] == state["cid"]
        assert "container" not in state["second_body"]
        await client.aclose()

    async def test_a_409_re_quote_on_an_oversized_container_uploads_only_once(self):
        """The upload is hoisted out of the re-quote loop: a `409` asking for a
        bigger payment changes nothing about the bytes, so every attempt reuses
        the `container_cid` the first (and only) upload minted."""
        state = {"uploads": 0}
        posts = []

        def jobs(request):
            if request.url.path == "/v1/files":
                state["uploads"] += 1
                raw = multipart_parts(request)[-1][2]
                state["cid"] = fake_cid(raw)
                return json_response(200, {
                    "id": "file_container", "object": "file", "purpose": "input",
                    "bytes": len(raw), "expires_at": 1_800_000_000,
                    "vorq": {"cid": state["cid"]},
                })
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            if len(posts) == 1:
                # The drift: same order, a bigger amount, in the 402's own shape.
                return json_response(409, quote_body(body, amount="0.000242"), retryable=False)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        prompt = "x" * (INLINE_MAX_BYTES + 1024)
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input=prompt, sla="batch",
                            max_rate_out="0.15", provider=1)
        assert state["uploads"] == 1  # filed once, not once per attempt
        assert len(posts) == 2
        assert "container" not in posts[0] and "container" not in posts[1]
        assert posts[0]["container_cid"] == state["cid"]
        assert posts[1]["container_cid"] == state["cid"]
        assert posts[0]["amount"] == "0.00021" and posts[1]["amount"] == "0.000242"
        await client.aclose()

    async def test_a_402_after_a_complete_submission_is_an_error_not_a_loop(self):
        """The defect this replaces: a second 402 used to rebuild and re-upload.

        A body carrying a container is a complete submission, and the node never
        answers one with a quote — its own `neverChallengeAfterBytes` hook
        rewrites such an answer to a `400 container_without_payment`. So a `402`
        here is a protocol violation, and looping on it would re-upload the whole
        payload to be quoted again, unbounded.
        """
        posts = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(402, quote_body(body))  # never legitimate

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match="402 to a body carrying a container"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                                max_rate_out="0.15", provider=1)
        assert len(posts) == 1  # uploaded once, then refused — never a second time
        await client.aclose()

    async def test_a_409_carrying_a_quote_is_re_signed_through_the_same_path(self):
        """`gasFee` drift: a refusal with the remedy attached, in the 402's shape.

        The two `409`s are told apart by body and never by status, so "sign this
        quote" is one code path whichever status delivered it. The container is
        unchanged — the terms and `c` did not move, only the payment.
        """
        posts = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            if len(posts) == 1:
                # The drift: same order, a bigger amount, in the 402's own shape.
                return json_response(409, quote_body(body, amount="0.000242"), retryable=False)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15", provider=1)
        assert len(posts) == 2
        # Same container, same commitment, same job id: nothing was re-sealed, so
        # the client did not burn a `c` to answer a moving gas fee.
        assert posts[0]["container"] == posts[1]["container"]
        assert posts[0]["c"] == posts[1]["c"]
        assert posts[0]["job_id"] == posts[1]["job_id"]
        assert posts[0]["signature"] == posts[1]["signature"]
        # Only the payment moved — and it moved to the amount the 409 quoted.
        assert posts[0]["amount"] == "0.00021"
        assert posts[1]["amount"] == "0.000242"
        assert posts[0]["auth_sig"] != posts[1]["auth_sig"]
        await client.aclose()

    async def test_a_409_carrying_an_error_is_raised_and_never_re_sent(self):
        posts = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(
                409,
                {"error": {"type": "invalid_request", "code": "DuplicateJob",
                           "message": "the chain refused this transaction: DuplicateJob"}},
                retryable=False,
            )

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError) as exc:
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                                max_rate_out="0.15", provider=1)
        # The node's own refusal reaches the caller verbatim — not the client's
        # complaint that a quote it expected was missing. Told apart by **body**:
        # a code path that branched on the status alone would send this one into
        # the re-quote loop and surface "no quote to sign" instead of DuplicateJob.
        assert "DuplicateJob" in str(exc.value)
        assert exc.value.status_code == 409
        assert len(posts) == 1
        await client.aclose()

    async def test_the_re_quote_loop_is_bounded(self):
        """A node that re-quotes forever stops getting the megabytes."""
        posts = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(409, quote_body(body, amount=format_usd(300 + len(posts), 6)))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match="re-quoted"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                                max_rate_out="0.15", provider=1)
        assert len(posts) == MAX_SUBMIT_ATTEMPTS
        await client.aclose()

    async def test_a_terms_only_200_is_refused_rather_than_short_circuited(self):
        """No server answers 200 to a terms-only body, and one that did would be wrong.

        The node returns `402` unconditionally when `payment` is absent. A success
        there would mean a job posted with no container — no bytes to pin, so no
        name, which the registry refuses on chain as `EmptyTaskCid`. The old
        short-circuit accepted exactly that answer and skipped phase 2 entirely.
        """
        def jobs(request):
            body = req_body(request)
            if is_probe(body):
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match="terms-only body"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                                provider=1)
        await client.aclose()
        # The market probe has the same single valid answer.
        client = Client(transport=httpx.MockTransport(auth_router(
            lambda r: json_response(200, queued_job(sla="24h"))
        )))
        with pytest.raises(VorqError, match="market probe"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch")
        await client.aclose()

    # -- the quoted authorization, member for member -------------------------
    #
    # **This client signs what it derives; the quote supplies the amount and
    # nothing else.** The block is still read member for member, because a node
    # describing a different authorization than the one this client is about to
    # sign is a node on another deployment — and signing anyway would produce an
    # unclaimable job rather than a refusal.
    @pytest.mark.parametrize(
        "challenge, match",
        [
            pytest.param(lambda b: {"quote": {"amount": "0.00021"}},
                         "names no amount and authorization", id="no-authorization"),
            pytest.param(lambda b: quote_with(b, to=STRANGER),
                         "not the one this client derives", id="another-payee"),
            pytest.param(lambda b: quote_with_domain(b, verifyingContract=STRANGER),
                         "not the one this client derives", id="another-token"),
            pytest.param(lambda b: quote_with_domain(b, name="USD Coin"),
                         "not the one this client derives", id="another-token-name"),
            pytest.param(lambda b: quote_with(b, nonce="0x" + "cc" * 32),
                         "not the one this client derives", id="another-jobs-nonce"),
            pytest.param(lambda b: quote_with(b, valid_before=int(b["expires_at"]) + 2),
                         "not the one this client derives", id="another-window"),
            pytest.param(lambda b: quote_with(b, value=211),
                         "not the one this client derives", id="another-value"),
            pytest.param(lambda b: {**quote_body(b), "quote": {**quote_body(b)["quote"],
                                                               "amount": 210}},
                         "not a canonical USD string", id="atomic-amount"),
            pytest.param(lambda b: quote_with(b, value="210"),
                         "not the one this client derives", id="string-value"),
        ],
    )
    async def test_a_quote_this_client_does_not_derive_is_refused(self, challenge, match):
        posts = []

        def jobs(request):
            body = req_body(request)
            posts.append(body)
            if is_probe(body):
                return json_response(402, quote_body(body))
            if "auth_sig" not in body:
                return json_response(402, challenge(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match=match) as exc:
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi",
                                sla="batch", max_rate_out="0.15", provider=1)
        assert exc.value.status_code == 402
        # The refusal is the whole point: no funded body ever went out.
        assert not [p for p in posts if "auth_sig" in p]
        await client.aclose()

    async def test_a_dropped_connection_reads_the_job_back_before_re_uploading(self):
        """The client-side pre-check, which is the mechanism and not an optimisation.

        Server-side idempotency was designed and proved impossible: the `jobs`
        table is a pure projection of `Posted`, which carries neither signature,
        and every field a comparison could use is public calldata a front-runner
        copies verbatim. Only the client knows `job_id = keccak(owner ‖ c)` before
        it sends, so only the client can ask whether its own job landed.
        """
        state = {"posts": 0, "gets": 0}

        def jobs(request):
            if request.method == "GET":
                state["gets"] += 1
                state["asked"] = request.url.path
                return json_response(200, {**queued_job(sla="24h"), "id": state["job_id"]})
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            state["posts"] += 1
            state["job_id"] = body["job_id"]
            raise httpx.ReadTimeout("connection dropped after the bytes landed")

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        handle = await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi",
                                     sla="batch", max_rate_out="0.15", provider=1)
        assert state["posts"] == 1                       # uploaded once, not twice
        assert state["gets"] == 1                        # and read back instead
        assert state["asked"] == f"/v1/jobs/{state['job_id']}"
        assert handle.id == state["job_id"]
        await client.aclose()

    async def test_a_dropped_connection_re_uploads_only_when_the_job_is_absent(self):
        state = {"posts": 0, "gets": 0}

        def jobs(request):
            if request.method == "GET":
                state["gets"] += 1
                return json_response(404, {"error": {"type": "not_found_error",
                                                     "message": "unknown job"}})
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            state["posts"] += 1
            if state["posts"] == 1:
                raise httpx.ReadTimeout("dropped before the bytes landed")
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15", provider=1)
        assert state["gets"] == 1 and state["posts"] == 2
        await client.aclose()

    async def test_a_probe_naming_candidates_designates_the_first_and_signs_its_ask(self):
        """Hosted matching. The probe's `402` names the asks within the
        ceilings, ranked by the node, and an unpinned order takes the first:
        sealed to its box key, `designated` set to its id, signed at its ask —
        no escrow key, no registry read, and no verifier needed. The probe is
        unsigned and names no commitment. The bytes still cross the wire once,
        on the complete submission.
        """
        first, second = SealedBoxCipher.generate(), SealedBoxCipher.generate()
        candidates = [
            {"provider_id": 5, "box_key": first.public_key, "rate_in": "0.000001", "rate_out": "0.000002"},
            {"provider_id": 6, "box_key": second.public_key, "rate_in": "0.000003", "rate_out": "0.000004"},
        ]
        challenges, completes, paths = [], [], []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body, candidates=candidates))
            completes.append(body)
            return json_response(200, queued_job(sla="24h"))

        inner = auth_router(jobs)

        def spy(request):
            paths.append(request.url.path)
            return inner(request)

        client = Client(transport=httpx.MockTransport(spy))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15")
        assert len(challenges) == 2 and len(completes) == 1
        probe, real = challenges
        assert "container" not in probe and "container" not in real
        assert probe["designated"] == 0 and probe["max_rate_out"] == "0.15"
        assert "signature" not in probe and "job_id" not in probe
        assert (real["rate_in"], real["rate_out"]) == ("0.000001", "0.000002")
        complete = completes[0]
        assert complete["job_id"] == real["job_id"]
        assert int(complete["designated"]) == 5
        assert _open_complete(complete, first._private)["input"] == {"input": "hi"}
        with pytest.raises(Exception):
            _open_complete(complete, second._private)
        assert "/key" not in paths
        assert not [p for p in paths if p.startswith("/evm/providers/")]
        await client.aclose()

    async def test_an_explicit_pin_probes_only_that_provider(self):
        """A pin is a refusal to be matched: the probe is pinned too, and the
        order signs that provider's ask or none."""
        challenges, completes = [], []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body))
            completes.append(body)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi", sla="batch",
                            max_rate_out="0.15", provider=2)
        probe, real = challenges
        assert probe["designated"] == 2
        assert (real["rate_in"], real["rate_out"]) == ("0.002", "0.004")
        assert int(completes[0]["designated"]) == 2
        assert _open_complete(completes[0])["input"] == {"input": "hi"}
        await client.aclose()

    async def test_an_input_ceiling_alone_signs_the_ask_on_both_sides(self):
        """Only `max_rate_in`, and a provider within it: the output side has no
        ceiling, so the order signs that provider's own output rate — not zero."""
        probes, challenges, completes = [], [], []

        def jobs(request):
            body = req_body(request)
            if is_probe(body):
                probes.append(body)
                return json_response(402, quote_body(body))
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body))
            completes.append(body)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.05")
        [probe] = probes
        assert probe["max_rate_in"] == "0.05" and "max_rate_out" not in probe
        # market_candidate(1): 0.001 in, under the ceiling, and 0.002 out.
        assert (challenges[0]["rate_in"], challenges[0]["rate_out"]) == ("0.001", "0.002")
        assert int(completes[0]["designated"]) == 1
        await client.aclose()

    async def test_a_candidate_above_a_ceiling_is_refused(self):
        """The node filters by the ceilings; a node that did not is not signed for."""
        def jobs(request):
            return json_response(402, {"candidates": [market_candidate(1)]})

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match="above the ceilings"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.0005")
        await client.aclose()

    async def test_one_ceiling_rests_at_it_and_the_market_rate_on_the_other_side(self):
        """No ask is within the input ceiling, so the order rests: at the ceiling
        on the side that names one, and at the cheapest live ask's rate on the
        side that does not — an order signs both rates."""
        probes, challenges, completes = [], [], []

        def jobs(request):
            body = req_body(request)
            if is_probe(body):
                probes.append(body)
                return json_response(402, quote_body(body))
            if "auth_sig" not in body:
                challenges.append(body)
                return json_response(402, quote_body(body))
            completes.append(body)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)),
                        verifier=escrow_verifier())
        await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.0005")
        ceiling, market = probes
        assert ceiling["max_rate_in"] == "0.0005" and "max_rate_out" not in ceiling
        assert "max_rate_in" not in market and "max_rate_out" not in market
        assert (challenges[0]["rate_in"], challenges[0]["rate_out"]) == ("0.0005", "0.002")
        assert int(completes[0]["designated"]) == 0
        assert _open_complete(completes[0], ESCROW_SECRET)["input"] == {"input": "hi"}
        await client.aclose()

    async def test_one_ceiling_and_no_live_ask_is_refused_before_anything_is_signed(self):
        """With no live ask there is no rate for the unnamed side to rest at."""
        posts = []

        def jobs(request):
            posts.append(req_body(request))
            return json_response(402, {"candidates": []})

        client = Client(transport=httpx.MockTransport(auth_router(jobs)),
                        verifier=escrow_verifier())
        with pytest.raises(ValidationError, match="no provider is serving"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.0005")
        assert all(is_probe(p) for p in posts) and len(posts) == 2
        await client.aclose()

    async def test_an_empty_candidate_list_rests_the_order_at_its_ceilings(self):
        """No ask is within the ceilings: the order rests at them — sealed to
        the verified escrow key, `designated` 0."""
        completes = []

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body, candidates=[]))
            completes.append(body)
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)),
                        verifier=escrow_verifier())
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="cheap",
                            sla="batch", max_rate_in="0.0003", max_rate_out="0.001")
        assert (completes[0]["rate_in"], completes[0]["rate_out"]) == ("0.0003", "0.001")
        assert int(completes[0]["designated"]) == 0
        assert _open_complete(completes[0], ESCROW_SECRET)["input"] == {"input": "cheap"}
        await client.aclose()

    async def test_a_malformed_candidates_field_is_an_api_error(self):
        def jobs(request):
            body = req_body(request)
            return json_response(402, {**quote_body(body), "candidates": "x"})

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(VorqError, match="candidates"):
            await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hi",
                                sla="batch", max_rate_out="0.15")
        await client.aclose()

    async def test_an_open_order_seals_to_the_verified_escrow_key(self):
        """The other order path. Same container, different recipient — that is all."""
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)),
                        verifier=escrow_verifier())
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="cheap",
                            sla="batch", max_rate_out="0.001")
        complete = captured["complete"]
        # Q22: an open order designates 0. That is the contract's sentinel for
        # "any provider", not an absence — a null has nowhere to live in a uint32.
        assert int(complete["designated"]) == 0
        # No bare key anywhere: the escrow reads the seed out of the wrap like
        # anyone else, and derives the DEK under the owner it reads from chain.
        assert not [k for k in json_keys(complete) if "dek" in k.lower()]
        assert "input" not in complete
        container = base64.b64decode(complete["container"])
        assert commitment_of(container) == _signed_c(complete)
        envelope = _open_complete(complete, ESCROW_SECRET)
        assert envelope["owner"] == client.signer.address
        assert envelope["input"] == {"input": "cheap"}
        await client.aclose()

    async def test_an_open_order_with_no_verifier_fails_closed_before_any_complete_post(self):
        """Q17: unverifiable includes "no way to verify", and nothing is posted.

        There is no fallback to a designated bid. Re-targeting is the caller's
        decision, because an SDK that picked a provider here would have turned a
        fail-closed into a fail-quiet — and the caller would never learn that the
        order it believed was escrow-protected went somewhere it did not choose.

        The probe challenge does go out — it is how the client learns nobody
        clears the bid — but it carries no bytes, and no complete submission ever
        follows it.
        """
        posts = []

        def jobs(request):
            body = req_body(request)
            if "container" not in body:
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(500, {})

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        with pytest.raises(EscrowKeyUnverified, match="no verifier"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        assert posts == []
        await client.aclose()

    async def test_an_open_order_fails_closed_when_the_escrow_measurement_is_revoked(self):
        """The tombstoned allowlist: a revoked image is a refusal, not a warning."""
        posts = []
        revoked = {"entries": [{"kind": "image", "measurement": ESCROW_MEASUREMENT,
                                "status": "revoked", "mock": True}],
                   "as_of_block": 128}

        def jobs(request):
            body = req_body(request)
            if "container" not in body:   # the probe: answered, never posted
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(500, {})

        def allowlist(request):
            return httpx.Response(200, json=revoked)

        client = Client(
            transport=httpx.MockTransport(auth_router(jobs)),
            verifier=escrow_verifier(httpx.MockTransport(allowlist)),
        )
        with pytest.raises(EscrowKeyUnverified, match="revoked"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        assert posts == []
        await client.aclose()

    async def test_an_open_order_fails_closed_on_evidence_that_binds_another_key(self):
        """The binding is `sha256(escrow_pk ‖ "vorq-coordinator-escrow-v1")`.

        Evidence about some other key is evidence about some other node, however
        well formed it is.
        """
        posts = []
        stranger = PrivateKey.generate().public_key.encode(HexEncoder).decode()
        lying = escrow_announcement()
        lying["evidence"] = {**lying["evidence"],
                             "report_data": __import__("vorq.verify", fromlist=["x"])
                             .escrow_report_data(stranger)}

        def jobs(request):
            body = req_body(request)
            if "container" not in body:   # the probe: answered, never posted
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(500, {})

        client = Client(
            transport=httpx.MockTransport(auth_router(jobs, key_body=lying)),
            verifier=escrow_verifier(),
        )
        with pytest.raises(EscrowKeyUnverified, match="does not bind"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        assert posts == []
        await client.aclose()

    async def test_an_open_order_fails_closed_on_a_stale_key_announcement(self):
        """Q23: `issued_at` is checked to ±600 s, so a replayed announcement dies."""
        posts = []

        def jobs(request):
            body = req_body(request)
            if "container" not in body:   # the probe: answered, never posted
                return json_response(402, quote_body(body))
            posts.append(body)
            return json_response(500, {})

        client = Client(
            transport=httpx.MockTransport(auth_router(jobs)),
            # The announcement is stamped 1_790_000_000; this clock is an hour on.
            verifier=escrow_verifier(now=1_790_003_600.0),
        )
        with pytest.raises(EscrowKeyUnverified, match="freshness bound"):
            await client.submit(model="m", input="hi", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        assert posts == []
        await client.aclose()

    async def test_the_escrow_key_is_cached_for_the_ttl(self):
        seen = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs, seen=seen)),
                        verifier=escrow_verifier())
        await client.submit(model="m", input="a", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        await client.submit(model="m", input="b", sla="batch", max_rate_in="0.000001", max_rate_out="0.000001")
        assert seen["key_calls"] == 1
        # And so is the chain context: a coordinator that changed the contract it
        # relays to mid-session has changed protocol, not configuration.
        assert seen["chain_calls"] == 1
        await client.aclose()

    async def test_result_decrypts_sealed_output(self, no_sleep):
        import base64 as _b64
        import json as _json

        from vorq._crypto import seal_to

        # The provider seals the result to the key the envelope carried — the one
        # the client derives from its wallet.
        cipher_public = derive_result_cipher(WalletSigner()).public_key
        real_output = {
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "sealed hi"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
        sealed = _b64.b64encode(seal_to(cipher_public, _json.dumps(real_output).encode())).decode()
        # The sealed envelope IS the settled bytes; the job names them.
        raw = _json.dumps({"enc": "vorq-sealed-v1", "ciphertext": sealed}).encode()
        completed = {
            "id": "0xabc", "object": "job", "model": "deepseek-ai/deepseek-v4-pro:fp8", "status": "completed",
            "result_cid": fake_cid(raw),
            "vorq": {"gas_fee": "0.03", "fee": "0",
                     "sla_secs": 3600, "rate_in": "0.05", "rate_out": "0.15", "provider_id": 7},
        }

        def handler(request):
            if request.url.path == "/auth/nonce":
                return json_response(200, {"nonce": "n", "expires_at": 9999999999, "chain_id": 84532})
            if request.url.path == "/auth/session":
                return json_response(200, {"token": "vorq_sess_x", "expires_at": 9999999999})
            if request.url.path.startswith("/ipfs/"):
                return httpx.Response(200, content=raw)
            return json_response(200, completed)

        client = Client(transport=httpx.MockTransport(handler))
        handle = client.job("0xabc")
        result = await handle.result()
        assert isinstance(result, TextResult)
        assert result.text == "sealed hi"
        await client.aclose()


class TestRetryPolicy:
    async def test_retries_get_on_retryable_then_succeeds(self, no_sleep):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return json_response(503, {"error": {"message": "busy", "type": "overloaded"}}, retryable=True)
            return json_response(200, COMPLETED_TEXT_JOB)

        client = make_client(handler, max_retries=3)
        status = await client.job("job_txt").status()
        assert status == "completed"
        assert calls["n"] == 3
        await client.aclose()

    async def test_does_not_retry_when_header_false(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return json_response(500, {"error": {"message": "boom", "type": "internal_error"}}, retryable=False)

        client = make_client(handler, max_retries=3)
        with pytest.raises(Exception):
            await client.job("job_txt").status()
        assert calls["n"] == 1
        await client.aclose()

    async def test_maps_404_to_not_found_with_request_id(self):
        def handler(request):
            return json_response(
                404, {"error": {"message": "Unknown job", "type": "not_found"}}, request_id="req_abc"
            )

        client = make_client(handler)
        with pytest.raises(NotFoundError) as exc:
            await client.job("job_none").status()
        assert exc.value.request_id == "req_abc"
        assert exc.value.type == "not_found"
        await client.aclose()


class TestJobHandle:
    async def test_result_returns_textresult_when_completed(self):
        client = make_client(completed_text_response)
        result = client.job("job_txt")  # re-attach, no network call — see JobHandle test below
        res = await result.result()
        assert isinstance(res, TextResult)
        assert res.text == "hi there"
        assert res.cost == "0.000125"
        await client.aclose()

    async def test_result_raises_jobfailed_on_failed(self):
        # No `error` object: the row has none. `ended_because = 4` is `reclaim`
        # — a provider claimed it and never settled inside the window.
        failed = node_job(
            id="job_f", status="failed", result_cid=None,
            vorq={"job_id": "job_f", "rate_out": "0.15",
                  "state": 3, "ended_because": 4},
        )

        def handler(request):
            return json_response(200, failed)

        client = make_client(handler)
        with pytest.raises(JobFailed) as exc:
            await client.job("job_f").result()
        assert exc.value.error_type == "reclaim"
        assert exc.value.job_id == "job_f"
        await client.aclose()

    async def test_result_times_out_when_still_queued(self):
        def handler(request):
            return json_response(200, queued_job())

        client = make_client(handler)
        with pytest.raises(WaitTimeout) as exc:
            await client.job("job_txt").result(timeout=0)
        assert exc.value.job_id == "job_txt"
        await client.aclose()

    async def test_a_24h_job_is_polled_on_the_stepped_schedule(self, wait_clock):
        """Once a minute for 15 min, every 3 min to the hour, every 10 min after."""
        def handler(request):
            return json_response(200, queued_job(sla="24h"))

        client = make_client(handler)
        with pytest.raises(WaitTimeout):
            await client.job("job_txt").result()
        assert wait_clock == [60.0] * 15 + [180.0] * 15 + [600.0] * 138
        await client.aclose()

    async def test_a_1h_job_is_polled_once_a_minute_throughout(self, wait_clock):
        def handler(request):
            return json_response(200, queued_job(sla="1h"))

        client = make_client(handler)
        with pytest.raises(WaitTimeout):
            await client.job("job_txt").result()
        assert wait_clock == [60.0] * 60
        await client.aclose()

    async def test_result_polls_until_terminal(self, no_sleep):
        calls = {"n": 0}

        def handler(request):
            if request.url.path.startswith("/ipfs/"):
                return completed_text_response(request)   # the result read, not a poll
            calls["n"] += 1
            if calls["n"] == 1:
                return json_response(200, queued_job(job_id="job_txt", sla="1h"))
            return json_response(200, COMPLETED_TEXT_JOB)

        client = make_client(handler)
        res = await client.job("job_txt").result()
        assert isinstance(res, TextResult)
        assert calls["n"] == 2
        await client.aclose()

    async def test_fetches_the_result_when_the_job_carries_a_result_cid(self):
        # The settled job names its result bytes; the client fetches them **from
        # the gateway** under that name and opens them with its wallet-derived
        # cipher. The full URL is asserted, host included: the coordinator serves
        # no blob endpoint, so a read that went to the node would be a read of
        # something the node wrote.
        from vorq._client import DEFAULT_GATEWAY
        from vorq._crypto import seal_to

        cipher_public = derive_result_cipher(WalletSigner()).public_key
        plaintext = {
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "fetched hi"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
        raw = json_.dumps({
            "enc": "vorq-sealed-v1",
            "ciphertext": base64.b64encode(
                seal_to(cipher_public, json_.dumps(plaintext).encode())
            ).decode(),
        }).encode()
        cid = fake_cid(raw)
        job = {
            "id": "0xabc", "object": "job", "model": "deepseek-ai/deepseek-v4-pro:fp8",
            "status": "completed", "result_cid": cid,
            "vorq": {"gas_fee": "0.03", "fee": "0",
                     "sla_secs": 3600, "rate_in": "0.05", "rate_out": "0.15", "provider_id": 7},
        }
        seen = {}

        def handler(request):
            if request.url.path == "/auth/nonce":
                return json_response(200, {"nonce": "n", "expires_at": 9999999999, "chain_id": 84532})
            if request.url.path == "/auth/session":
                return json_response(200, {"token": "vorq_sess_x", "expires_at": 9999999999})
            if request.url.path.startswith("/ipfs/"):
                seen["blob"] = str(request.url)
                return httpx.Response(200, content=raw)
            seen.setdefault("node", []).append(request.url.path)
            return json_response(200, job)

        client = Client(transport=httpx.MockTransport(handler))
        res = await client.job("0xabc").result()
        assert isinstance(res, TextResult)
        assert res.text == "fetched hi"
        # The shipped default gateway, by name — not merely "a path ending in the CID".
        assert seen["blob"] == f"{DEFAULT_GATEWAY}/ipfs/{cid}"
        # And the node was asked for the job row and nothing else.
        assert seen["node"] == ["/v1/jobs/0xabc"]
        await client.aclose()

    async def test_a_completed_job_that_names_no_result_is_refused(self):
        # There is no inline fallback: `output` is a body the coordinator wrote,
        # not the one the provider settled under a name.
        # A settle that named nothing is broken, and says so.
        calls = {"blob": 0}
        unnamed = {**COMPLETED_TEXT_JOB, "result_cid": None}

        def handler(request):
            if request.url.path.startswith("/ipfs/"):
                calls["blob"] += 1
            return json_response(200, unnamed)

        client = make_client(handler)
        with pytest.raises(ResultIntegrityError, match="names no result"):
            await client.job("job_txt").result()
        assert calls["blob"] == 0
        await client.aclose()

    async def test_a_gateway_miss_on_the_result_never_falls_back_to_the_row(self, monkeypatch):
        """The named bytes are unreachable, so there is no result — full stop.

        The row carries an `output` copy the coordinator wrote, and it is exactly
        the wrong thing to hand back: it is not what the provider settled, it was
        never opened with the caller's cipher, and returning it would make an
        unreachable result indistinguishable from a delivered one. So this
        raises, and it raises the gateway's own failure rather than a result
        error, because the result is not known to be bad — it is not known at
        all.
        """
        monkeypatch.setattr(_client_module, "GATEWAY_BACKOFF_S", 0.0)
        asked = []

        def handler(request):
            asked.append(str(request.url))
            if request.url.path.startswith("/ipfs/"):
                return httpx.Response(504, content=b"gateway timeout")
            return json_response(200, COMPLETED_TEXT_JOB)

        client = make_client(handler, gateway="http://gw.test")
        with pytest.raises(VorqError, match="gateway read of") as caught:
            await client.job("job_txt").result()
        assert caught.value.status_code == 504
        # The job row, then the gateway until it gives up — and nothing else ever.
        # A `504` is asked again (the gateway, not the name), so what matters here
        # is the set of addresses, not the count.
        assert set(asked) == {
            "https://api.vorq.co/v1/jobs/job_txt",
            f"http://gw.test/ipfs/{COMPLETED_TEXT_JOB['result_cid']}",
        }
        await client.aclose()

    async def test_bytes_at_the_name_that_are_not_a_result_reach_the_caller_shaped(self):
        """Whatever is at that name, it is not an answer — and it says so.

        The decoder's own exception would tell a caller nothing about which layer
        failed, so the read path raises `ResultIntegrityError`, which is the same
        error the unnamed case raises: from the caller's side both mean "this job
        has no result to hand back".
        """
        def handler(request):
            if request.url.path.startswith("/ipfs/"):
                return httpx.Response(200, content=b"\x00\x01\xff\xfe")
            return json_response(200, COMPLETED_TEXT_JOB)

        client = make_client(handler, gateway="http://gw.test")
        with pytest.raises(ResultIntegrityError, match="not JSON"):
            await client.job("job_txt").result()
        await client.aclose()

    async def test_a_sealed_result_needs_the_wallets_own_cipher(self):
        """Sealed to somebody else's key: opened by nobody, and never guessed at.

        The result is sealed to the `result_key` the submission's envelope
        carried, which a `WalletSigner` client derives from its own wallet. A
        client holding a different one cannot open these bytes, and the failure
        is the decrypt refusing rather than a partial or empty result.

        It reaches the caller as a `VorqError`, not as the crypto library's own
        exception: one `except VorqError` covers the whole read path or it covers
        none of it, and a caller cannot be expected to know which box this is.
        """
        from vorq._crypto import seal_to

        stranger = SealedBoxCipher.generate()
        raw = json_.dumps({
            "enc": "vorq-sealed-v1",
            "ciphertext": base64.b64encode(
                seal_to(stranger.public_key, json_.dumps({"output": []}).encode())
            ).decode(),
        }).encode()
        job = {**COMPLETED_TEXT_JOB, "result_cid": fake_cid(raw)}

        def handler(request):
            if request.url.path.startswith("/ipfs/"):
                return httpx.Response(200, content=raw)
            return json_response(200, job)

        client = make_client(handler, gateway="http://gw.test",
                             cipher=SealedBoxCipher.generate())
        with pytest.raises(ResultIntegrityError, match="did not open") as caught:
            await client.job("job_txt").result()
        # The cipher's own failure is chained, not swallowed.
        assert isinstance(caught.value, VorqError)
        assert caught.value.__cause__ is not None
        await client.aclose()

    async def test_fetch_blob_returns_the_raw_bytes_the_gateway_serves(self):
        """The hit: bytes in, bytes out, unparsed and untouched."""
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b"\x00\x01raw bytes")

        client = make_client(handler, gateway="http://gw.test")
        assert await client.fetch_blob("bafkreisomething") == b"\x00\x01raw bytes"
        assert seen == ["http://gw.test/ipfs/bafkreisomething"]
        await client.aclose()

    async def test_a_gateway_miss_raises_and_asks_nobody_else(self):
        """The gateway is the whole read path, so a miss on it is the answer.

        There is no coordinator blob door behind it, so this must raise rather
        than fall back — and nothing but the gateway may be asked on the way out.
        """
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(404, content=b"not found")

        client = make_client(handler, gateway="http://gw.test")
        with pytest.raises(VorqError, match="gateway read of bafkreiunknown failed") as caught:
            await client.fetch_blob("bafkreiunknown")
        # The status rides along, the way it does everywhere else a status is known.
        assert caught.value.status_code == 404
        # Nothing but the gateway, however many times it was asked (see below:
        # a fresh name legitimately 404s for a few seconds).
        assert set(paths) == {"/ipfs/bafkreiunknown"}
        await client.aclose()

    async def test_a_name_the_gateway_has_not_propagated_yet_is_waited_out(self, monkeypatch):
        """A 404 seconds after the pin is "not yet", not "never".

        The bytes are pinned by the coordinator and read back through a public
        gateway, and a freshly minted name takes a few seconds to become
        resolvable there. A single-shot read turns that window into a failed job
        whose result is sitting in the store — so this waits, exactly as the
        provider's own gateway source does on the task side.

        The backoff is flattened here; what is under test is the patience, not
        the sleeping.
        """
        monkeypatch.setattr(_client_module, "GATEWAY_BACKOFF_S", 0.0)
        attempts = []

        def handler(request):
            attempts.append(request.url.path)
            if len(attempts) < 3:
                return httpx.Response(404, content=b"not found")
            return httpx.Response(200, content=b"the bytes")

        client = make_client(handler, gateway="http://gw.test")
        assert await client.fetch_blob("bafkreifresh") == b"the bytes"
        assert len(attempts) == 3
        await client.aclose()

    async def test_a_gateway_that_refuses_the_read_is_not_waited_out(self, monkeypatch):
        """A `403` is an answer, and waiting does not change it.

        Private and bucket-scoped gateways refuse names outside their scope. That
        is a configuration fact rather than a propagation delay, so it is raised
        on the first answer instead of costing the caller the whole window.
        """
        monkeypatch.setattr(_client_module, "GATEWAY_BACKOFF_S", 0.0)
        attempts = []

        def handler(request):
            attempts.append(request.url.path)
            return httpx.Response(403, content=b"forbidden")

        client = make_client(handler, gateway="http://gw.test")
        with pytest.raises(VorqError, match="failed with HTTP 403"):
            await client.fetch_blob("bafkreiscoped")
        assert len(attempts) == 1
        await client.aclose()

    async def test_a_gateway_that_never_answers_raises_a_vorq_error(self, monkeypatch):
        """The transport branch: no response at all, so no status to carry.

        A caller holding one `except VorqError` should not also have to know
        which HTTP library reads the gateway, so the transport's own exception is
        wrapped rather than allowed to escape. And there is nothing behind the
        gateway to try, so this raises like the other two — after the same wait a
        404 gets, since a gateway that dropped one connection is the other half of
        the same "ask again" case.
        """
        monkeypatch.setattr(_client_module, "GATEWAY_BACKOFF_S", 0.0)
        paths = []

        def handler(request):
            paths.append(request.url.path)
            raise httpx.ConnectError("connection refused", request=request)

        client = make_client(handler, gateway="http://gw.test")
        with pytest.raises(VorqError, match="could not reach http://gw.test") as caught:
            await client.fetch_blob("bafkreisomething")
        assert caught.value.status_code is None
        assert isinstance(caught.value.__cause__, httpx.ConnectError)
        assert set(paths) == {"/ipfs/bafkreisomething"}
        await client.aclose()

    async def test_the_gateway_falls_back_to_the_environment(self, monkeypatch):
        """No ``gateway=``: the read gateway is the one ``$VORQ_PIN_GATEWAY`` names."""
        monkeypatch.setenv("VORQ_PIN_GATEWAY", "http://env-gw.test/")
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b"bytes")

        client = make_client(handler)
        # Resolved at construction, and the trailing slash is gone: one join, one URL.
        assert client._gateway == "http://env-gw.test"
        assert await client.fetch_blob("bafkreiabc") == b"bytes"
        assert seen == ["http://env-gw.test/ipfs/bafkreiabc"]
        await client.aclose()

    async def test_no_gateway_means_no_read_path_and_says_so(self):
        """`gateway=""` used to mean "read from the coordinator". It has no blob door."""
        def handler(request):
            raise AssertionError(f"nothing should be requested: {request.url}")

        client = make_client(handler, gateway="")
        assert client._gateway is None
        with pytest.raises(VorqError, match="no gateway configured"):
            await client.fetch_blob("bafkreisomething")
        await client.aclose()

    async def test_the_shipped_gateway_is_a_read_path_and_not_a_credential(self):
        """The default is a bare gateway base URL: no key, no token, no account.

        The URL literal is a configuration value rather than a name — it has to
        resolve to a host that actually serves the objects the coordinator pins,
        since a CID is content-addressed but retrievability is not. What must
        stay vendor-neutral is the prose and the identifiers around it.
        """
        from vorq._client import DEFAULT_GATEWAY

        assert DEFAULT_GATEWAY.startswith("https://")
        assert "@" not in DEFAULT_GATEWAY and "?" not in DEFAULT_GATEWAY
        for secret in ("VORQ_PIN_S3_KEY", "VORQ_PIN_API_TOKEN", "token=", "key="):
            assert secret not in DEFAULT_GATEWAY

    async def test_fetch_blob_defaults_to_the_shipped_gateway(self, monkeypatch):
        # Zero configuration: no gateway argument, no environment — the SDK's
        # built-in public gateway is the read path.
        from vorq._client import DEFAULT_GATEWAY

        monkeypatch.delenv("VORQ_PIN_GATEWAY", raising=False)
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, content=b"bytes")

        client = make_client(handler)
        assert client._gateway == DEFAULT_GATEWAY
        assert await client.fetch_blob("QmOpaque") == b"bytes"
        assert seen["url"] == f"{DEFAULT_GATEWAY}/ipfs/QmOpaque"
        await client.aclose()

    async def test_job_reattach_makes_no_network_call(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return json_response(200, COMPLETED_TEXT_JOB)

        client = make_client(handler)
        handle = client.job("job_txt")
        assert handle.id == "job_txt"
        assert calls["n"] == 0  # re-attach is a plain call, no request
        await handle.status()
        assert calls["n"] == 1
        await client.aclose()


# -- the job row, as the node actually serves it ---------------------------
#
# Every assertion below is built from `node_job()`, which is `clientJob` in
# `src/api/routes/jobs.ts` transcribed key for key. The three mappings this class
# covers each read a field the coordinator has never sent, and each failed
# *silently*: a `None` provider, a one-hour pacing on a one-day job, and a cause
# vocabulary that collapsed to the status. None of them raise, and every fixture
# in this repo agreed with the code rather than with the node.


class TestTheNodesOwnJobShape:
    async def test_the_settled_provider_is_read_from_provider_id(self):
        """`vorq.provider_id`, not `vorq.provider` — the node sends the former.

        `clientJob` projects the chain's `providerId`; there is no `provider`
        key on the row and never was. Reading the wrong one is invisible: the
        result is built, `.provider` is just `None`, and no error says why.
        """
        raw = COMPLETED_TEXT_BYTES
        job = node_job(result_cid=fake_cid(raw), vorq={"provider_id": 42})

        def handler(request):
            if request.url.path.startswith("/ipfs/"):
                return httpx.Response(200, content=raw)
            return json_response(200, job)

        client = make_client(handler, gateway="http://gw.test")
        res = await client.job("job_txt").result()
        assert res.provider == 42
        await client.aclose()

    async def test_the_poll_pacing_is_read_from_sla_secs(self):
        """`vorq.sla_secs` is an integer; there is no `sla` window string.

        The consequence is not cosmetic. `result()`'s default timeout **is** the
        job's SLA, so a re-attached 24 h job whose window failed to parse waits
        one hour and then raises `WaitTimeout` on a job that is fine — the
        client giving up on work the network is still doing.
        """
        job = node_job(status="queued", result_cid=None,
                       vorq={"sla_secs": 86400, "state": 0, "ended_because": 0})

        def handler(request):
            return json_response(200, job)

        client = make_client(handler)
        handle = client.job("job_txt")
        await handle.status()
        assert handle._sla == "24h"
        assert sla_seconds(handle._sla) == 86400
        # A re-attached 24 h job is paced by the long-wait schedule.
        assert poll_interval(handle._sla, 0) == 60.0
        assert poll_interval(handle._sla, 3600) == 600.0
        await client.aclose()

    @pytest.mark.parametrize("ended_because, status, cause", [
        (3, "failed", "provider_fail"),      # the claimant said it could not deliver
        (4, "failed", "reclaim"),            # claimed, never settled inside the window
        (2, "cancelled", "cancelled"),       # the owner's own signed cancel
        (5, "cancelled", "expired"),         # nobody ever claimed it
    ])
    async def test_the_end_cause_is_read_from_ended_because(
        self, ended_because, status, cause
    ):
        """The node sends **no `error` object**; the cause is `vorq.ended_because`.

        `Types.sol` fixes the vocabulary — 2 cancelled, 3 provider_fail, 4
        reclaim, 5 expired — and `clientJob` passes it through verbatim while
        folding 3/4 into the status `failed` and 2/5 into `cancelled`. Reading a
        `job["error"]` that is never on the row collapses four causes into the
        two statuses, and `provider_fail` and `reclaim` — the two the docs
        promise a caller can branch on — become unreachable.
        """
        job = node_job(status=status, result_cid=None,
                       vorq={"state": 3, "ended_because": ended_because})

        def handler(request):
            return json_response(200, job)

        client = make_client(handler)
        with pytest.raises(JobFailed) as caught:
            await client.job("job_txt").result()
        assert caught.value.error_type == cause
        assert caught.value.job_id == "job_txt"
        # The cause reaches `.type` too, which is what the compat surface restates.
        assert caught.value.type == cause
        await client.aclose()

    async def test_the_post_receipt_is_not_a_job_row_and_both_are_read(self):
        """`POST /v1/jobs` answers `201 {job_id, task_cid, tx_hash}` — a receipt.

        `GET /v1/jobs/{id}` answers a row keyed `id`. Both reach
        `_handle_from_job`, and they spell the job differently: the receipt is
        the only place `task_cid` can be learned (this node mints the name, so a
        caller that is not told cannot compute or read it), and it carries no
        terms at all, so the window the submission asked for has to stand in.
        """
        receipt = {"job_id": "0xfeed", "task_cid": "bafy-minted",
                   "tx_hash": "0x" + "11" * 32}
        client = make_client(lambda r: json_response(200, {}))
        handle = client._handle_from_job(receipt, "24h")
        assert handle.id == "0xfeed"
        assert handle.task_cid == "bafy-minted"
        assert handle._sla == "24h"          # nothing on the receipt says otherwise

        # And the row shape, whose terms do say so, and whose task_cid is nested.
        row = node_job(id="0xfeed", vorq={"sla_secs": 86400, "task_cid": "bafy-row"})
        handle = client._handle_from_job(row, "1h")
        assert handle.id == "0xfeed"
        assert handle.task_cid == "bafy-row"
        assert handle._sla == "24h"          # read from sla_secs, not from the argument
        await client.aclose()

    async def test_an_unreadable_end_cause_degrades_to_the_status(self):
        """A cause this SDK does not know is not invented into one it does.

        `ENDED_SETTLED` (1) and `ENDED_NONE` (0) never reach here, and a future
        constant would arrive as an integer with no name. Falling back to the
        status keeps the failure honest instead of guessing `reclaim`.
        """
        job = node_job(status="failed", result_cid=None,
                       vorq={"state": 3, "ended_because": 99})

        def handler(request):
            return json_response(200, job)

        client = make_client(handler)
        with pytest.raises(JobFailed) as caught:
            await client.job("job_txt").result()
        assert caught.value.error_type == "failed"
        await client.aclose()


# -- the signed, relayed cancel -------------------------------------------
#
# `cancel` is a chain op the node relays, not a state change the node decides.
# `JobRegistry.cancel(jobId, issuedAt, signature)` never reads `msg.sender` and
# there is no `cancelFor`, so this signature is the entire authority: a node that
# could author one could cancel any job it relayed. The client therefore signs
# `Cancel(bytes32 jobId,uint64 issuedAt)` on the JobRegistry's domain and posts
# `{issued_at, signature}` — the two fields `POST /v1/jobs/:id/cancel` parses
# (`src/api/routes/post.ts`) and nothing else.

#: A real 32-byte job id: the signed member is a `bytes32`, so the handle's id
#: has to be one. `keccak(owner ‖ c)` is what the chain derives, and every id the
#: client ever holds comes from there.
CANCEL_JOB_ID = "0x" + "ab" * 32


def cancel_router(cancel_handler, *, seen=None):
    """Everything a cancel reads — the chain context — then the cancel itself."""

    def handler(request):
        path = request.url.path
        if path == "/evm/chain":
            if seen is not None:
                seen["chain_calls"] = seen.get("chain_calls", 0) + 1
            return json_response(200, CHAIN)
        if path == "/auth/nonce":
            return json_response(200, {"nonce": "n0nce", "expires_at": 9999999999, "chain_id": 84532})
        if path == "/auth/session":
            return json_response(200, {"token": "vorq_sess_minted", "expires_at": 9999999999})
        return cancel_handler(request)

    return handler


def _recover_cancel(job_id: str, issued_at: int, signature: str, domain=None) -> str:
    """Recover the address a `Cancel` signature was made by, under `domain`."""
    from eth_utils import to_bytes

    from vorq._crypto import CANCEL_TYPES

    message = {"jobId": to_bytes(hexstr=job_id), "issuedAt": int(issued_at)}
    encoded = encode_typed_data(
        domain_data=domain or order_domain(CTX), message_types=CANCEL_TYPES,
        message_data=message,
    )
    return Account.recover_message(encoded, signature=signature)


class TestCancel:
    async def test_the_cancel_body_is_the_signed_cancel_typed_data(self):
        """The wire body, field for field, and the signature recovered from it.

        Not "a POST happened": the body is `{issued_at, signature}`, the
        signature recovers to this client's wallet over *this* job id and *that*
        `issued_at`, and there is nothing else on the body — the node reads two
        fields and a third would be a field this client invented.
        """
        captured = {}
        before = int(time.time())

        def cancel(request):
            captured["path"] = request.url.path
            captured["method"] = request.method
            captured["body"] = req_body(request)
            return json_response(200, {"job_id": CANCEL_JOB_ID, "tx_hash": "0x" + "11" * 32})

        client = Client(transport=httpx.MockTransport(cancel_router(cancel)))
        await client.job(CANCEL_JOB_ID).cancel()
        after = int(time.time())

        assert captured["method"] == "POST"
        assert captured["path"] == f"/v1/jobs/{CANCEL_JOB_ID}/cancel"
        assert set(captured["body"]) == {"issued_at", "signature"}

        issued_at = captured["body"]["issued_at"]
        # An integer, a JSON number, as every integer on the wire is.
        assert isinstance(issued_at, int) and not isinstance(issued_at, bool)
        # Unix seconds, stamped now — the chain bounds it to ±600 s of landing.
        assert before <= issued_at <= after

        signature = captured["body"]["signature"]
        assert signature.startswith("0x") and len(signature) == 132
        assert _recover_cancel(CANCEL_JOB_ID, issued_at, signature) == WalletSigner().address
        await client.aclose()

    async def test_the_signature_binds_the_job_it_cancels(self):
        """Change the job id and the same signature recovers to a stranger.

        This is what stops a relayed cancel being replayed onto another job: the
        job id is inside the digest, and a substitution is not detectable as a
        forgery — it just recovers to somebody who does not own that job.
        """
        captured = {}

        def cancel(request):
            captured["body"] = req_body(request)
            return json_response(200, {"job_id": CANCEL_JOB_ID})

        client = Client(transport=httpx.MockTransport(cancel_router(cancel)))
        await client.job(CANCEL_JOB_ID).cancel()
        body = captured["body"]

        mine = WalletSigner().address
        assert _recover_cancel(CANCEL_JOB_ID, body["issued_at"], body["signature"]) == mine
        other = "0x" + "cd" * 32
        assert _recover_cancel(other, body["issued_at"], body["signature"]) != mine
        # And so does moving `issued_at`, which is why it rides the body rather
        # than being re-derived by the node.
        assert _recover_cancel(
            CANCEL_JOB_ID, body["issued_at"] + 1, body["signature"]
        ) != mine
        await client.aclose()

    async def test_the_cancel_is_signed_on_the_job_registry_domain(self):
        """Under the ProviderRegistry's domain the same bytes recover to a stranger.

        The two VORQ domains share a name, a version and a chain id, so
        `verifyingContract` is the whole difference and the failure is silent.
        """
        from vorq._terms import registry_domain

        captured = {}

        def cancel(request):
            captured["body"] = req_body(request)
            return json_response(200, {"job_id": CANCEL_JOB_ID})

        client = Client(transport=httpx.MockTransport(cancel_router(cancel)))
        await client.job(CANCEL_JOB_ID).cancel()
        body = captured["body"]

        mine = WalletSigner().address
        assert _recover_cancel(CANCEL_JOB_ID, body["issued_at"], body["signature"]) == mine
        assert _recover_cancel(
            CANCEL_JOB_ID, body["issued_at"], body["signature"],
            domain=registry_domain(CTX),
        ) != mine
        await client.aclose()

    async def test_a_wallet_less_client_cannot_cancel_and_posts_nothing(self):
        """No wallet, no cancel — and the refusal costs no request.

        A token-only client can read a job all day; it cannot author the one
        signature that ends one. Raising before the post is what keeps the caller
        from reading a `400` off the node and concluding the job is uncancellable.
        """
        posts = []

        def handler(request):
            posts.append(request.url.path)
            return json_response(200, {"job_id": CANCEL_JOB_ID})

        client = make_client(handler)  # from_session_token, no signer
        with pytest.raises(ValidationError, match="wallet"):
            await client.job(CANCEL_JOB_ID).cancel()
        assert posts == []
        await client.aclose()

    async def test_the_cancel_reads_the_chain_context_the_client_already_has(self):
        """One `GET /evm/chain` per client, cancel included.

        The context is what the domain is built from, so a cancel needs it; it is
        also read once and cached, so two cancels do not re-read the deployment.
        """
        seen: dict = {}

        def cancel(request):
            return json_response(200, {"job_id": CANCEL_JOB_ID})

        client = Client(transport=httpx.MockTransport(cancel_router(cancel, seen=seen)))
        await client.job(CANCEL_JOB_ID).cancel()
        await client.job(CANCEL_JOB_ID).cancel()
        assert seen["chain_calls"] == 1
        await client.aclose()


# -- confidential submissions ---------------------------------------------
#
# ``submit(..., confidential=True)`` is the whole trigger: a client holding a
# Verifier then seals a designated bid only to a key whose attestation evidence
# checks out, and refuses to submit at all when it cannot. Without the flag the
# verifier never runs, whatever the model is called — names are inert here. The
# mock transport below serves both roles at once — the coordinator
# (``/v1/jobs``) and the chain-state reads the verifier makes.

MEASUREMENT = hashlib.sha256(b"vorq-mock-cvm-image-v1").hexdigest()

# The conventional confidential listing name and an ordinary one. Neither is
# inspected by the client; both are here so the tests prove that.
CONFIDENTIAL_NAME = "deepseek-ai/e2ee-deepseek-v4-pro:fp8"
PLAIN_NAME = "deepseek-ai/deepseek-v4-pro:fp8"


def _raising_transport(what: str) -> httpx.MockTransport:
    """A transport that fails the test if anything is ever sent through it."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request ({what}): {request.url}")

    return httpx.MockTransport(unreachable)


def _attested_env(good_box: str, bad_box: str, wallet: str, *, bind_wallet: str | None = None,
                  candidates: list[dict] | None = None):
    """MockTransport serving everything a designated submission reads.

    Provider 1 verifies; provider 2 has no evidence at all. ``bind_wallet`` is the
    payee the evidence *claims* to bind — leave it unset for a self-consistent
    record, or point it elsewhere to make provider 1's report-data binding fail.

    ``candidates`` is what the node's ``402`` names; left unset, a market probe
    names the pinned provider (or 1) and any other challenge is a bare quote.
    """
    from vorq.verify import report_data

    # `operator`, not `address`: the payee on this wire is `operator`, and the
    # evidence binds it. A fixture that set a phantom key left provider 1's
    # operator at the helper's default while binding the wallet — self-consistent
    # with the old reader, and impossible on the node.
    good_rec = provider_record(
        1, good_box, operator=wallet,
        evidence={"type": "mock-cvm-v1", "measurement": MEASUREMENT,
                  "report_data": report_data(good_box, bind_wallet or wallet),
                  "debug": False, "tcb": {"svn": 1}, "quote": "b3BhcXVl"},
    )
    bad_rec = provider_record(2, bad_box, operator=wallet, evidence=None)
    entries = [{"kind": "image", "measurement": MEASUREMENT, "release": "mock-dev",
                "status": "active", "mock": True}]
    sealed_to: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": entries, "as_of_block": 128})
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        if path == "/v1/models":
            return httpx.Response(200, json=CATALOG)
        if path == "/key":
            return httpx.Response(200, json=escrow_announcement())
        if path == "/evm/providers/1":
            return httpx.Response(200, json=good_rec)
        if path == "/evm/providers/2":
            return httpx.Response(200, json=bad_rec)
        if path == "/v1/jobs":
            body = req_body(request)
            if "container" not in body:   # phase 1: terms-only → the quote
                named = candidates
                if named is None and "rate_in" not in body:
                    # A market probe names the pin (or 1) with its registry box key.
                    pid = int(body["designated"]) or 1
                    rec = good_rec if pid == 1 else bad_rec
                    named = [{"provider_id": pid, "box_key": rec["box_key"],
                              "rate_in": "0.000001", "rate_out": "0.000002"}]
                return httpx.Response(402, json=quote_body(body, candidates=named))
            sealed_to.append(body)
            return httpx.Response(200, json={"id": body["job_id"], "object": "job",
                                             "status": "queued",
                                             "vorq": {"sla": "1h"}})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), sealed_to


async def test_a_confidential_submission_seals_only_to_a_verified_key():
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    good = SealedBoxCipher.generate()
    bad = SealedBoxCipher.generate()
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(good.public_key, bad.public_key, signer.address)
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=1,
                        validate_params=False, confidential=True)
    assert int(sealed_to[0]["designated"]) == 1
    # The wrap opens with the attested provider's key and with nobody else's.
    assert _open_complete(sealed_to[0], good._private)["input"] == {"input": "hi"}
    with pytest.raises(Exception):
        _open_complete(sealed_to[0], bad._private)
    await client.aclose()


async def test_a_confidential_submission_verifies_a_plain_model_name_too():
    """The name is inert: an ordinary listing name verifies exactly the same."""
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    good_box = SealedBoxCipher.generate().public_key
    bad_box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(good_box, bad_box, signer.address)
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    await client.submit(PLAIN_NAME, "hi", sla="1h", provider=1,
                        validate_params=False, confidential=True)
    assert int(sealed_to[0]["designated"]) == 1
    await client.aclose()


async def test_a_confidential_submission_to_an_unverifiable_provider_raises():
    """Provider 1's evidence binds somebody else's payee. Nothing is sealed."""
    from vorq.errors import VerificationError
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(box, box, signer.address,
                                         bind_wallet="0x" + "00" * 20)
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    with pytest.raises(VerificationError):
        await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=1,
                            validate_params=False, confidential=True)
    assert sealed_to == []   # nothing was sealed to anyone
    await client.aclose()


async def test_a_confidential_pin_on_a_provider_with_no_evidence_raises():
    """Never a silent substitute: provider 1 is right there and attested, and the
    pin on the unattested 2 still refuses."""
    from vorq.errors import VerificationError
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    good_box = SealedBoxCipher.generate().public_key
    bad_box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(good_box, bad_box, signer.address)
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    with pytest.raises(VerificationError):
        await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=2,
                            validate_params=False, confidential=True)
    assert sealed_to == []
    await client.aclose()


def _candidate(provider_id: int, box_key: str) -> dict:
    return {"provider_id": provider_id, "box_key": box_key, "rate_in": "0.000001", "rate_out": "0.000002"}


async def test_a_confidential_submission_seals_to_the_first_verified_candidate():
    """Unpinned and confidential: the node's ranking stands, filtered to what
    attests. Provider 2 is named first and has no evidence; 1 is the seal target."""
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    good = SealedBoxCipher.generate()
    bad = SealedBoxCipher.generate()
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(
        good.public_key, bad.public_key, signer.address,
        candidates=[_candidate(2, bad.public_key), _candidate(1, good.public_key)],
    )
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", validate_params=False,
                        confidential=True)
    assert int(sealed_to[0]["designated"]) == 1
    assert _open_complete(sealed_to[0], good._private)["input"] == {"input": "hi"}
    await client.aclose()


async def test_a_confidential_submission_with_no_verified_candidate_raises():
    from vorq.errors import VerificationError
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    good_box = SealedBoxCipher.generate().public_key
    bad = SealedBoxCipher.generate()
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(good_box, bad.public_key, signer.address,
                                         candidates=[_candidate(2, bad.public_key)])
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    with pytest.raises(VerificationError, match="none of the 1 candidate"):
        await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", validate_params=False,
                            confidential=True)
    assert sealed_to == []
    await client.aclose()


async def test_a_confidential_submission_with_no_candidates_raises():
    """An order no ask is within would rest as an escrowed open order, which
    cannot be an attested channel — so under `confidential=True` it is a refusal."""
    from vorq.errors import VerificationError
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(box, box, signer.address, candidates=[])
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock", transport=transport),
    )
    with pytest.raises(VerificationError, match="no provider asks within these ceilings"):
        await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", max_rate_in="0.000001", max_rate_out="0.000001",
                            validate_params=False, confidential=True)
    assert sealed_to == []
    await client.aclose()


async def test_confidential_without_a_verifier_raises_before_any_network_call():
    client = Client.from_session_token(
        "vorq_test", signer=WalletSigner.generate(), cipher=SealedBoxCipher.generate(),
        transport=_raising_transport("no verifier: submit must refuse offline"),
    )
    # validate_params is left on: the param-schema read would have hit the
    # network first, had the guard not fired ahead of it.
    with pytest.raises(ValidationError, match="requires a verifier"):
        await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=1, confidential=True)
    await client.aclose()


async def test_a_non_confidential_designated_submission_never_touches_the_verifier():
    """No flag, no provider verification — even on a conventionally-named
    confidential model with a verifier in hand."""
    from vorq.verify import Verifier

    cipher = SealedBoxCipher.generate()
    bad_box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(bad_box, bad_box, signer.address)
    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher, transport=transport,
        verifier=Verifier("http://node", mode="mock",
                          transport=_raising_transport("verifier read chain state")),
    )
    await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=2, validate_params=False)
    assert int(sealed_to[0]["designated"]) == 2   # unfiltered
    await client.aclose()


async def test_a_non_confidential_designated_submission_without_a_verifier_is_unchanged():
    cipher = SealedBoxCipher.generate()
    box = SealedBoxCipher.generate().public_key
    signer = WalletSigner.generate()
    transport, sealed_to = _attested_env(box, box, signer.address)
    client = Client.from_session_token("vorq_test", signer=signer, cipher=cipher,
                                       transport=transport)
    await client.submit(CONFIDENTIAL_NAME, "hi", sla="1h", provider=2, validate_params=False)
    assert int(sealed_to[0]["designated"]) == 2
    await client.aclose()


async def test_a_provider_that_publishes_no_box_key_is_refused():
    """An order resting on a pinned provider with nothing to seal to is a refusal
    rather than a null wrap. (The node does not name such a provider at all.)"""
    from vorq.errors import VerificationError

    signer = WalletSigner.generate()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/evm/chain":
            return httpx.Response(200, json=CHAIN)
        if path == "/v1/models":
            return httpx.Response(200, json=CATALOG)
        if path.startswith("/evm/providers/"):
            return httpx.Response(200, json=provider_record(1, box_key=None))
        if path == "/v1/jobs" and is_probe(json_.loads(request.content)):
            return httpx.Response(402, json={"candidates": []})  # the node names nobody keyless
        raise AssertionError(f"nothing should be posted: {path}")

    client = Client.from_session_token(
        "vorq_test", signer=signer, cipher=SealedBoxCipher.generate(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VerificationError, match="no box_key"):
        await client.submit(PLAIN_NAME, "hi", sla="1h", max_rate_in="0.000001", max_rate_out="0.000001", provider=1,
                            validate_params=False)
    await client.aclose()


class TestNoPinningSurface:
    """The SDK holds no pinning credential and no line of pinning code.

    The coordinator is the only party that pins, and the storage service mints
    the name. These are the assertions that keep a pinning store from creeping
    back in as a convenience: there is nothing to configure, and a caller that
    tries is told so rather than quietly ignored.
    """

    async def test_the_client_takes_no_pin_store(self):
        with pytest.raises(TypeError):
            Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                pin_store=object(),
            )

    async def test_a_pinning_credential_in_the_environment_changes_nothing(self, monkeypatch):
        """`$VORQ_PIN_S3_KEY` used to build a store at construction. It is inert now."""
        monkeypatch.setenv("VORQ_PIN_S3_KEY", "would-have-built-a-store")
        client = Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        assert not hasattr(client, "_pin_store")
        await client.aclose()

    async def test_the_standalone_pinning_package_is_gone(self):
        with pytest.raises(ImportError):
            import vorq.standalone  # noqa: F401

    async def test_the_client_exposes_no_pinning_helper(self):
        assert not hasattr(Client, "_pin_input")

    async def test_the_submission_names_no_cid_and_reads_one_back(self):
        """The client never sends `task_cid`; it reads the minted one off the answer."""
        captured = {}

        def jobs(request):
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            captured["complete"] = body
            return json_response(
                201, {"job_id": body["job_id"], "task_cid": "minted-by-the-service",
                      "tx_hash": "0xdeadbeef"},
            )

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        handle = await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="hello",
                                     sla="batch", max_rate_out="0.15", provider=1)
        assert "task_cid" not in json_keys(captured["complete"])
        assert handle.id == captured["complete"]["job_id"]
        assert handle.task_cid == "minted-by-the-service"
        await client.aclose()


class TestRetiredSurfaces:
    """What the Global Constraints require gone, asserted as gone.

    Each of these is a surface a later change could reintroduce as a
    convenience, and each one would be a real regression: the v1 order shape and
    the b402 payment build are refused by the node's post door, and every
    batch/file route named here is one this coordinator does not serve.
    """

    async def test_the_client_builds_no_v1_order_or_payment(self):
        # `_upload_file` is off this list: it is back with the batch surface, and
        # it is not a v1 shape — it files an already-sealed JSONL through the
        # coordinator's own pinning path. The v1 order and payment builders stay
        # gone; the post door refuses what they built.
        for gone in ("_seal_order", "_order_body", "_build_payment"):
            assert not hasattr(Client, gone), gone

    async def test_the_signer_has_no_v1_order_method(self):
        assert not hasattr(WalletSigner, "sign_order")

    async def test_the_order_type_is_the_contracts_and_not_the_v1_struct(self):
        assert set(ORDER_TYPES) == {"Order"}
        assert "VorqOrder" not in ORDER_TYPES

    async def test_the_crypto_module_exposes_no_v1_payment_surface(self):
        import vorq._crypto as crypto

        assert not hasattr(crypto, "order_input_hash")

    async def test_the_batch_surface_is_back_and_seals_every_line(self):
        """Batches return, and they return sealed.

        The gate was never the batch object — it was where the input JSONL lived.
        A line now carries its own container v1, so the file the coordinator
        receives is a manifest of sealed envelopes it pins like any other payload,
        and `submit` refuses outright without the keys that make that possible.
        """
        from vorq._batches import Batches

        client = Client.from_session_token(
            "vorq_test", transport=httpx.MockTransport(lambda r: httpx.Response(500))
        )
        assert isinstance(client.batches, Batches)
        with pytest.raises(ValidationError, match="must be sealed"):
            await client.batches.submit([{"body": {"model": "m", "input": "x"}}])
        await client.aclose()

    async def test_the_container_module_seals_no_bare_dek(self):
        import vorq._container as container

        for gone in ("new_dek", "seal_dek", "seal_dek_to"):
            assert not hasattr(container, gone), gone


class TestUploadFirstOperationalBounds:
    """When the bytes are filed, and what is refused before they are."""

    async def test_the_container_is_filed_only_after_the_payment_is_signed(self):
        """The node deletes an upload nobody attached after 300 s, so filing the
        bytes ahead of ``sign_payment_authorization`` spends that window on a
        signature.

        A local key signs in microseconds and would never notice. A signer that
        prompts — a hardware wallet, a remote signing service, the browser wallet
        the JS SDK shares this flow with — can take minutes, and an upload the
        sweep collects makes a post the node refuses ``unknown_container`` and
        marks not retryable. The payment does not depend on the container, so
        signing first costs nothing and a caller who declines never uploads.
        """
        order: list[str] = []

        def jobs(request):
            if request.url.path == "/v1/files":
                order.append("upload")
                return json_response(
                    200,
                    {"id": "file_1", "object": "file", "purpose": "input", "bytes": 1,
                     "vorq": {"cid": "bafy-input"}},
                )
            body = req_body(request)
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        signer = client.signer
        # `Client.signer` is optional — a read-only client has none. This test is
        # about what the signing path does, so a missing signer is a broken
        # fixture rather than a case to handle.
        assert signer is not None
        real = signer.sign_payment_authorization

        def watched(**kwargs):
            order.append("payment")
            return real(**kwargs)

        signer.sign_payment_authorization = watched  # type: ignore[method-assign]
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8",
                            input="x" * (INLINE_MAX_BYTES + 1024), sla="batch",
                            max_rate_out="0.15", provider=1)
        await client.aclose()

        assert order == ["payment", "upload"]

    async def test_the_container_is_filed_once_across_a_re_quote(self):
        """The memo must not move the upload *into* the attempt loop.

        Python's memo is the shape most likely to regress — a mutable local
        reassigned inside the loop body — so a future edit that moved
        ``content_field = None`` inside the ``for`` would re-upload on every 409
        with nothing to catch it. A gas-fee re-quote re-signs the payment and
        re-sends the same reference; it never re-sends the payload.
        """
        state = {"uploads": 0, "posts": 0}

        def jobs(request):
            if request.url.path == "/v1/files":
                state["uploads"] += 1
                return json_response(
                    200,
                    {"id": "file_1", "object": "file", "purpose": "input", "bytes": 1,
                     "vorq": {"cid": "bafy-input"}},
                )
            body = req_body(request)
            state["posts"] += 1
            if "auth_sig" not in body:
                return json_response(402, quote_body(body))
            if state["posts"] == 2:
                # The gas fee drifted: a 409 carrying a quote, which is the 402's
                # own body shape and the one status that legitimately sends a
                # complete submission back.
                return json_response(409, quote_body(body))
            return json_response(200, queued_job(sla="24h"))

        client = Client(transport=httpx.MockTransport(auth_router(jobs)))
        await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8",
                            input="x" * (INLINE_MAX_BYTES + 1024), sla="batch",
                            max_rate_out="0.15", provider=1)
        await client.aclose()

        assert state["uploads"] == 1
        # Three posts: the challenge, the re-quoted attempt, the accepted one.
        assert state["posts"] == 3

    async def test_the_client_budgets_its_body_legs_for_the_largest_upload(self):
        """One static budget, not one computed per call.

        `read` and `write` carry a body and are sized once for the largest thing
        this client sends; `connect` and `pool` keep the caller's own timeout, so
        a coordinator that is simply not there is still refused in seconds rather
        than after a quarter of an hour.
        """
        seen = {}

        def router(request):
            seen["timeout"] = request.extensions.get("timeout")
            return json_response(200, {"id": "file_1", "object": "file",
                                       "vorq": {"cid": "bafy"}})

        client = Client(transport=httpx.MockTransport(auth_router(router)))
        await client._upload_file("big", "input", b"x" * (4 * 1024 * 1024))
        await client.aclose()

        sent = seen["timeout"]
        assert sent["read"] == UPLOAD_MAX_TIMEOUT_S
        assert sent["write"] == UPLOAD_MAX_TIMEOUT_S
        assert sent["connect"] == client.timeout
        assert sent["pool"] == client.timeout
        assert client.timeout < UPLOAD_MAX_TIMEOUT_S

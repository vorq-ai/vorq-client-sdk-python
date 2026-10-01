"""The async ``Client`` — transport, session lifecycle, narrow retry, ``submit``."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from eth_utils import keccak

from . import _media, _media_units
from ._container import (
    INLINE_MAX_BYTES,
    build_container,
    commitment,
    derive_dek,
    encrypt_under_dek,
    new_seed,
    seal_seed_to,
)
from ._crypto import (
    ENVELOPE_VERSION,
    Cipher,
    SealedBoxCipher,
    Signer,
    WalletSigner,
    content_job_id,
    derive_result_cipher,
)
from ._handles import JobHandle
from ._models import Models
from ._params import check_input
from ._sla import normalize_sla, sla_seconds, window_from_seconds
from ._money import format_usd, usd_arg, usd_atomic, wire_atomic, wire_usd
from ._terms import ChainContext, OrderTerms, payment_domain
from .errors import (
    EscrowKeyUnverified,
    NotFoundError,
    ValidationError,
    VerificationError,
    VorqError,
    error_from_wire,
)

if TYPE_CHECKING:  # attestation verification is opt-in; don't import it to use the client
    from .verify import Verifier

DEFAULT_BASE_URL = "https://api.vorq.co"

#: The read gateway the SDK ships with — the storage network's public gateway, so
#: a stock client resolves any CID with zero configuration. It is the **only**
#: read path: the coordinator serves no blob endpoint, so there is nothing behind
#: this to fall back to. Overridden by ``gateway=`` or ``$VORQ_PIN_GATEWAY``; an
#: empty string opts out, which means the caller is supplying its own gateway or
#: running its own node and will pass one before reading anything.
#:
#: A CID is content-addressed but **retrievability is not**. The gateway that
#: fronts the service the coordinator pins through serves those objects reliably;
#: an arbitrary public gateway serves them only once the content has propagated,
#: which is neither guaranteed nor prompt. So this value is picked for the read
#: path that works, and the provider SDK ships the identical one — a client
#: writing against one default and a provider reading against another would be a
#: silent break no test in either repo would catch.
DEFAULT_GATEWAY = "https://ipfs.filebase.io"

#: How many times a gateway read is attempted, and how long it waits between.
#:
#: Sized for **pin propagation, not for a flaky link**. A result is read seconds
#: after the settle that pinned it, and a freshly minted name takes a little
#: while to become resolvable on a read gateway — which answers ``404`` in the
#: meantime, indistinguishable at the wire from a name that will never exist. A
#: single-shot read turns that window into a failed job whose bytes are sitting
#: in the store, so this waits it out; the provider SDK waits the same way on the
#: task side, and for the same reason.
#:
#: Only "ask again" answers are retried — a miss, a server-side failure, or a
#: connection that dropped. A ``403`` from a scoped gateway is a configuration
#: fact and is raised on the first answer.
GATEWAY_ATTEMPTS = 8
GATEWAY_BACKOFF_S = 1.5

#: How long a fetched coordinator escrow key stays usable client-side.
#:
#: Three hours, and the number is not free: the escrow decays a key generation
#: after 48 h and an order may not expire more than 24 h out, so
#: ``KEY_CACHE_TTL + MAX_EXPIRY < DECAY_WINDOW`` (3 h + 24 h < 48 h) is what
#: keeps a container sealed to a cached key openable for the whole life of the
#: job it belongs to. The three constants move together.
KEY_CACHE_TTL = 3 * 3600.0

#: How many times one submission may be sent complete — the original plus the
#: re-quotes a ``409 {quote}`` asks for.
#:
#: The loop is bounded because it is the one place a client re-sends a whole
#: request, container included, on purpose. ``gasFee`` drifting twice while a
#: client signs is already unusual; drifting forever is a server the client
#: should stop feeding megabytes to, not a condition to spin on.
MAX_SUBMIT_ATTEMPTS = 3

#: The furthest out an order may expire, in seconds — ``JobRegistry``'s own bound,
#: mirrored so a long window is clamped here rather than refused at the door. The
#: chain's rule is ``expiresAt ∈ (now, now + 86400]``, and the ``24h`` SLA window
#: plus any settlement margin lands outside it, so the clamp is the ordinary case
#: and not an edge one.
MAX_EXPIRY_SECONDS = 86400


def _resolve_gateway(gateway: str | None) -> str | None:
    """Explicit argument > ``$VORQ_PIN_GATEWAY`` > built-in default.

    An empty string at either level disables the gateway rather than falling
    through — "I will supply my own" must be sayable, not just the accident of
    nothing being set. It leaves the client with no read path at all, and
    :meth:`Client.fetch_blob` says so rather than reaching for the coordinator,
    which has no blob door to reach for.
    """
    if gateway is None:
        env = os.environ.get("VORQ_PIN_GATEWAY")
        gateway = env if env is not None else DEFAULT_GATEWAY
    return gateway.rstrip("/") or None


# The payment authorization must stay valid until the job settles — cover the full
# SLA window plus this margin so a long ``batch`` (24h) order does not expire before
# ``/settle``. Configurable via ``$VORQ_SETTLEMENT_MARGIN`` (seconds).
SETTLEMENT_MARGIN = int(os.environ.get("VORQ_SETTLEMENT_MARGIN", "3600"))


def _default_signer() -> Signer | None:
    """Build a wallet signer from ``$VORQ_WALLET_KEY`` if it is set."""
    return WalletSigner() if os.environ.get("VORQ_WALLET_KEY") else None


def _default_cipher() -> Cipher | None:
    """Build a sealed-box cipher from ``$VORQ_CIPHER_KEY`` if it is set."""
    return SealedBoxCipher() if os.environ.get("VORQ_CIPHER_KEY") else None


def _canonical_bytes(model_input: Any) -> bytes:
    """Canonical JSON bytes — the encoding of everything that gets sealed."""
    return json.dumps(
        model_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# Each output frame's dimensions default to this when the request omits width/height —
# matches the coordinator and provider so the declared units_out agrees with settlement.
# Re-exported from the shipped table rather than written here: the daemon prices
# the same order from the same number, and the party that signs the units and the
# party that charges for them cannot afford to disagree. See `_media_units.py`;
# `make media-check` is what keeps every repo's copy the same file.
_DEFAULT_DIM = _media_units.DEFAULT_DIM
_DEFAULT_UNITS_OUT = _media_units.DEFAULT_UNITS_OUT


def _frame_pixels(model_input: dict) -> int:
    return _media.frame_pixels(model_input)


def _declare_units(model_input: dict, units_out: int | None = None) -> tuple[int, int]:
    """Client-declared accounting scalars (the API can't tokenize ciphertext).

    **One shape decision drives both numbers.** :func:`vorq._media.shape` reads the
    request once and both scalars follow from it; deriving them separately is how a
    request ends up priced as text on the output leg and as pixels on the input leg,
    which is a bill no party agrees on.

    ``units_out`` is the out dimension and an exact integer: an output-token ceiling
    for text; output pixels (``num_images × width × height``) for image; pixel-seconds
    (``width × height × duration_secs``) for video. Metering media in raw pixels prices
    a per-image, per-megapixel, or per-second backend alike, so a bigger image or a
    higher resolution honestly costs more.

    ``units_in`` is the quantity of input bought, and what that means depends on the
    same shape. Text and embeddings are metered on the prompt, one unit per four bytes
    of canonical JSON — a proxy, and a coarse one. Image and video are metered on the
    **reference**: total pixel-seconds across the assets the request carries, and zero
    when it carries none. A prompt's length buys nothing on a media job, and counting
    it would price a job by how well its reference happened to compress.

    An explicit ``units_out`` wins over every heuristic, zero included: a job with no
    output side to buy (an embedding, priced on prompt tokens and settling at
    ``completionTok == 0``) states ``units_out=0`` itself, and zero has to survive as
    zero rather than fall through to the default, or the client escrows an output leg
    the job can never spend. It does **not** change the input side, which is the
    request's shape either way.
    """
    if units_out is not None and (
        isinstance(units_out, bool) or not isinstance(units_out, int) or units_out < 0
    ):
        raise ValidationError(
            "units_out must be a non-negative integer",
            type="invalid_request_error",
        )

    kind = _media.shape(model_input)
    if kind == "text":
        units_in = max(1, len(_canonical_bytes(model_input)) // 4)
    else:
        units_in = _media.reference_units(model_input)

    if units_out is not None:
        return int(units_in), int(units_out)
    if kind == "video":
        units_out = _frame_pixels(model_input) * _media.duration_secs(model_input)
    elif kind == "image":
        units_out = _frame_pixels(model_input) * int(model_input.get("num_images", 1) or 1)
    else:
        units_out = (
            model_input.get("max_output_tokens")
            or model_input.get("max_tokens")
            or model_input.get("max_completion_tokens")
            or _DEFAULT_UNITS_OUT
        )
    return int(units_in), int(units_out)


#: The contract's own scaling divisor — ``JobRegistry.RATE_SCALE``. Rates are
#: atomic token units per this many units of work, so a cap is a ceiling division
#: by it and never a float.
RATE_SCALE = 1_000_000


def _cap(terms: "OrderTerms") -> int:
    """The escrow this order commits, exactly as ``JobRegistry`` computes it.

    ``max(1, ceilDiv(rateIn*unitsIn + rateOut*unitsOut, RATE_SCALE))`` — integer
    arithmetic end to end, and the floor of 1 is the contract's own: an order that
    priced to zero would commit nothing and could be claimed for free.

    Written out here because a batch line authorizes its payment without being
    quoted. The single-job path asks the node for this number and signs what it
    was quoted; a batch of 50 000 lines cannot ask 50 000 times, so it computes
    the same value and reads only the fees that ride on top of it from the
    network. The node re-derives it and refuses a line that cannot cover the pull,
    so a disagreement here is a skipped line and never a silent underpayment.
    """
    scaled = terms.rate_in * terms.units_in + terms.rate_out * terms.units_out
    return max(1, -(-scaled // RATE_SCALE))


@dataclass(frozen=True)
class SealedLine:
    """One batch line, sealed and signed but not yet paid.

    Frozen because it is the client's own record of what it committed to: the
    container is already encrypted under a seed this process will not mint again,
    and the order signature is over exactly these terms.
    """

    url: str
    terms: "OrderTerms"
    job_id: str
    vorq: dict
    container_b64: str


def _candidates(body: Any) -> list[dict]:
    """The clearing candidates a ``402`` names, in the node's order.

    Absent (an older node) is an empty list. Present but not a list is a node
    this client does not understand. Entries that cannot be sealed to — no
    positive integer ``provider_id``, no ``box_key`` string — are dropped, the
    same tolerance :meth:`~vorq.Verifier.verify_candidates` shows. A candidate's
    ``rate_in`` / ``rate_out`` are its ask, USD per 1M units, read as ``Decimal``;
    one that is not a canonical USD string is the node's fault and refused.
    """
    raw = body.get("candidates") if isinstance(body, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VorqError(
            "the 402 challenge carried a `candidates` field that is not a list",
            type="api_error", status_code=402,
        )
    kept: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        pid = c.get("provider_id")
        if isinstance(pid, bool):
            continue
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        box_key = c.get("box_key")
        if pid <= 0 or not isinstance(box_key, str) or not box_key:
            continue
        rates = {k: wire_usd(c[k], f"candidates[].{k}", 402) for k in ("rate_in", "rate_out") if k in c}
        kept.append({**c, "provider_id": pid, "box_key": box_key, **rates})
    return kept


@dataclass(frozen=True)
class _SignedOrder:
    """One signed order: the container (``None`` for a probe), its id, its terms, its wire form."""

    container: bytes | None
    job_id: str
    terms: "OrderTerms"
    vorq: dict


def mint_session_token(
    *,
    signer: Signer | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Run the ``/auth`` handshake once and return a ``vorq_sess_…`` token.

    Synchronous helper for pointing a stock ``openai`` client at VORQ: mint a
    token here, then pass it as that client's ``api_key``. Uses the wallet from
    ``signer`` or ``$VORQ_WALLET_KEY``.
    """
    signer = signer or _default_signer()
    if signer is None:
        raise ValueError("no wallet: set VORQ_WALLET_KEY or pass signer=...")
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport) as http:
        minted = http.get("/auth/nonce", params={"address": signer.address}).raise_for_status().json()
        nonce, chain_id = minted["nonce"], int(minted["chain_id"])
        body = {"address": signer.address, "nonce": nonce, "signature": signer.sign_nonce(nonce, chain_id)}
        return http.post("/auth/session", json=body).raise_for_status().json()["token"]


#: What `read` and `write` are given on the client's HTTP session, in seconds.
#:
#: Sized for the largest thing this client ever sends: the files door stores up
#: to 200 MiB, and at a poor uplink (~256 KiB/s) that is ~800 s on the wire plus
#: the node's own answer, which it sends only once the object store has taken
#: the body. `connect` and `pool` are left at the caller's `timeout`, so a
#: coordinator that is not there is still refused in seconds — this bounds only
#: a transfer that has started and then stopped moving.
UPLOAD_MAX_TIMEOUT_S = 900.0


class Client:
    """Async-only client for the VORQ inference exchange.

    Dead simple: set ``VORQ_WALLET_KEY`` in the environment and call
    ``vorq.Client()`` — the client builds the wallet signer, mints the
    ``vorq_sess_…`` session token from a wallet signature, and rotates it before
    expiry, all under the hood. The wallet is the only key to configure: it also
    derives the result cipher your answers come back sealed to, so there is
    nothing else to set and nothing else to lose. Pass ``signer=`` / ``cipher=``
    to override (e.g. a KMS-backed signer, or ``SealedBoxCipher()`` to keep a
    result key held in ``VORQ_CIPHER_KEY``). For a pre-minted token (or the
    emulator), use :meth:`from_session_token`.

    The client speaks to a coordinator node and to nothing else: job state is the
    node's job object, read over the coordinator API. Result bytes are the one
    exception, and they are not an exception to the trust boundary — they are
    content-addressed, so they come off the storage gateway by CID (``gateway``,
    or ``$VORQ_PIN_GATEWAY``) and the name is the whole entitlement.
    """

    _REFRESH_SKEW = 60.0  # re-mint this many seconds before the token expires

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        signer: Signer | None = None,
        cipher: Cipher | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        verifier: "Verifier | None" = None,
        gateway: str | None = None,
    ) -> None:
        resolved = signer if signer is not None else _default_signer()
        if resolved is None:
            raise ValueError(
                "no wallet: set VORQ_WALLET_KEY, pass signer=..., "
                "or use Client.from_session_token(...)"
            )
        self._configure(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            signer=resolved,
            cipher=cipher,
            transport=transport,
            session_token=None,
            verifier=verifier,
            gateway=gateway,
        )

    @classmethod
    def from_session_token(
        cls,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        signer: Signer | None = None,
        cipher: Cipher | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        verifier: "Verifier | None" = None,
        gateway: str | None = None,
    ) -> "Client":
        """Build a client from a pre-minted ``vorq_sess_…`` token.

        The dev / emulator path (the emulator accepts any ``vorq_…`` string) and
        the way to reuse a token minted elsewhere. Without a ``signer`` the token
        is used as-is and never rotated.
        """
        self = cls.__new__(cls)
        self._configure(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            signer=signer,
            cipher=cipher,
            transport=transport,
            session_token=token,
            verifier=verifier,
            gateway=gateway,
        )
        return self

    def _configure(
        self,
        *,
        base_url: str,
        timeout: float,
        max_retries: int,
        signer: Signer | None,
        cipher: Cipher | None,
        transport: httpx.AsyncBaseTransport | None,
        session_token: str | None,
        verifier: "Verifier | None",
        gateway: str | None = None,
    ) -> None:
        # Job state reads from the coordinator, and result bytes come straight off
        # the storage network by name. That is not a default with an alternative
        # behind it — the coordinator serves no blob endpoint, so the gateway is
        # the whole read path. Resolved here, at construction, so a client that
        # opted out of one knows it before the first read rather than at it.
        self._gateway: str | None = _resolve_gateway(gateway)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.signer = signer
        # An explicit cipher always wins (contract wallet, KMS, a key persisted
        # elsewhere). Otherwise a wallet derives its own result cipher — nothing to
        # configure and nothing to lose — and only a signerless client falls back
        # to $VORQ_CIPHER_KEY.
        if cipher is not None:
            self.cipher: Cipher | None = cipher
        elif isinstance(signer, WalletSigner):
            self.cipher = derive_result_cipher(signer)
        else:
            self.cipher = _default_cipher()
        # One verifier for the client's lifetime — it owns the allowlist cache and
        # reads chain state through whatever endpoint the caller pointed it at.
        self.verifier = verifier
        self._session_token = session_token
        # An explicit token is honored until it 401s; a minted one tracks expiry.
        self._session_expires_at = float("inf") if session_token else 0.0
        # One static budget, not one per call. `connect` and `pool` keep the
        # caller's `timeout` so an unreachable coordinator still fails in
        # seconds; `read` and `write` are the legs that carry a body, and they
        # are set once to cover the largest upload the files door will take
        # (its 200 MiB ceiling, at an uplink slow enough that anything below it
        # was never going to finish). Sizing them per payload meant threading a
        # timeout parameter through every request for the sake of one call.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(UPLOAD_MAX_TIMEOUT_S, connect=timeout, pool=timeout),
            transport=transport,
        )
        # The coordinator's escrow key, fetched from GET /key when an open order
        # needs somebody to seal its payload to, and held for KEY_CACHE_TTL.
        self._escrow_key: str | None = None
        self._escrow_key_expires_at = 0.0
        # The deployment every signature belongs to. Read once from
        # GET /evm/chain and never re-read: a coordinator that changed the
        # contract it relays to mid-session has changed protocol, not
        # configuration, and a client that quietly followed it would re-sign
        # against a registry the caller never chose.
        self._chain: ChainContext | None = None
        self.models = Models(self)
        # Imported here rather than at module scope: `_batches` imports `Client`
        # for its type hints, and a top-level import each way is a cycle.
        from ._batches import Batches

        self.batches = Batches(self)

    # -- session -----------------------------------------------------------

    async def _ensure_session(self) -> None:
        """Mint or rotate the session token from the signer's wallet as needed.

        No-op for a token-only client (no signer) and while a live token has
        time left. Runs the ``/auth/nonce`` → sign → ``/auth/session`` exchange
        directly on the transport so it never recurses through ``_request``.
        """
        if self.signer is None:
            return
        if self._session_token and time.time() < self._session_expires_at - self._REFRESH_SKEW:
            return
        nonce_resp = await self._http.request(
            "GET", "/auth/nonce", params={"address": self.signer.address}
        )
        if not nonce_resp.is_success:
            raise self._error(nonce_resp)
        minted = nonce_resp.json()
        nonce, chain_id = minted["nonce"], int(minted["chain_id"])
        session_resp = await self._http.request(
            "POST",
            "/auth/session",
            json={
                "address": self.signer.address,
                "nonce": nonce,
                "signature": self.signer.sign_nonce(nonce, chain_id),
            },
        )
        if not session_resp.is_success:
            raise self._error(session_resp)
        payload = session_resp.json()
        self._session_token = payload["token"]
        expires = payload.get("expires_at")
        self._session_expires_at = float(expires) if expires else time.time() + 3600.0

    # -- transport ---------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return (0.5 * (2**attempt)) + random.uniform(0, 0.25)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict | None = None,
        files: Any = None,
        data: Any = None,
        retry: bool = True,
        allow_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """Issue a request, applying the narrow retry policy.

        Retries only when the response carries ``X-Vorq-Retryable: true`` and
        ``retry`` is set; submissions pass ``retry=False`` so a job that already
        exists is never duplicated. A ``401`` re-mints the session once (when a
        wallet is held) and retries — even for submissions, since no job was
        created. Statuses in ``allow_statuses`` (e.g. the ``402`` challenge) are
        returned to the caller as data instead of being raised.
        """
        await self._ensure_session()
        reauthed = False
        attempt = 0
        while True:
            headers = (
                {"Authorization": f"Bearer {self._session_token}"}
                if self._session_token
                else None
            )
            resp = await self._http.request(
                method, url, json=json, params=params, files=files, data=data, headers=headers
            )
            if resp.is_success or resp.status_code in allow_statuses:
                return resp
            if resp.status_code == 401 and self.signer is not None and not reauthed:
                reauthed = True
                self._session_token = None
                self._session_expires_at = 0.0
                await self._ensure_session()
                continue
            retryable = resp.headers.get("x-vorq-retryable") == "true"
            if retry and retryable and attempt < self.max_retries:
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue
            raise self._error(resp)

    def _error(self, resp: httpx.Response):
        try:
            body = resp.json()
        except Exception:
            body = {}
        return error_from_wire(
            resp.status_code, body, request_id=resp.headers.get("x-request-id")
        )

    # -- job surface -------------------------------------------------------

    async def submit(
        self,
        model: str,
        input: str | dict,
        sla: str = "batch",
        rate_in: str | Decimal | None = None,
        rate_out: str | Decimal | None = None,
        provider: int | None = None,
        validate_params: bool = True,
        *,
        confidential: bool = False,
        units_out: int | None = None,
        custom_id: str | None = None,
    ) -> JobHandle:
        """Submit a job (always ``POST /v1/jobs``, any modality).

        A ``str`` input is sugar for ``{"input": str}``; a ``dict`` is the
        model-owned input object, sent verbatim.

        Submissions are always sealed: a wallet (``signer``) and a ``cipher`` are
        required. A pinned order (``provider=``) takes two requests; an unpinned
        one three. The first carries the terms and nothing else and comes back a
        ``402`` with the clearing candidates (ids and box keys, ranked by the
        node) and the payment requirements — for an unpinned order this first
        challenge is a **probe** over a placeholder commitment, because the
        recipient must be known before the real order can be sealed and signed.
        The client then seals the payload into **one container v1** — the bulk
        under a DEK derived from a fresh 32-byte seed, and the **seed** sealed
        either to the chosen provider (the pin, or the first candidate) or, when
        the challenge names nobody, to the coordinator's escrow key (the key
        itself never travels) — signs the order over that container's
        commitment, challenges once more for that order's own quote, signs the
        payment authorization, and posts terms and bytes together. **The
        container crosses the wire once**, and the answer carries the
        ``task_cid`` the coordinator's storage service minted.

        ``rate_in`` / ``rate_out`` are the bid in USD per 1M units of work, as a
        ``str`` or ``Decimal`` (``"0.05"``); an ``int`` or ``float`` is refused.
        With neither, the order takes the market: an
        unsigned probe asks the node for every live ask ranked (cheapest for this
        job, then least recently picked), and the order bids the first
        candidate's own ask, pinned to it. No live ask raises
        :class:`~vorq.errors.ValidationError` before anything is signed.

        Pass ``confidential=True`` to demand attestation: the client seals only
        to a candidate whose evidence checks out against the on-chain allowlist,
        and raises :class:`~vorq.errors.VerificationError` rather than sealing to
        an unattested key, resting the order as an escrowed open order, or
        substituting a different provider for a pinned one. It requires a client
        built with ``verifier=``. Model names carry no confidentiality semantics —
        this flag is the only trigger.

        Params are checked locally against the model's published schema before
        anything is sealed; pass ``validate_params=False`` to skip.
        """
        payload_input = {"input": input} if isinstance(input, str) else input
        window = normalize_sla(sla)
        rate_in, rate_out = usd_arg(rate_in, "rate_in"), usd_arg(rate_out, "rate_out")
        if self.signer is None or self.cipher is None:
            raise ValidationError(
                "submissions must be sealed: configure VORQ_WALLET_KEY "
                "(or pass signer=/cipher=)",
                type="invalid_request_error",
            )
        if confidential and self.verifier is None:
            # Before any request: a confidential submission that cannot verify
            # anything must not reach the network at all.
            raise ValidationError(
                "confidential submission requires a verifier: build the client "
                "with verifier=vorq.Verifier(...)",
                type="invalid_request_error",
            )
        if validate_params:
            try:
                schema = await self.models.params_schema(model)
            except Exception:
                schema = None  # network hiccup on discovery never blocks a submit
            check_input(schema, payload_input)
        return await self._submit_encrypted(
            model, payload_input, window, rate_in, rate_out, provider, confidential,
            units_out, custom_id,
        )

    async def _submit_encrypted(
        self,
        model: str,
        payload_input: dict,
        window: str,
        rate_in: str | None,
        rate_out: str | None,
        provider: int | None,
        confidential: bool,
        declared_units_out: int | None = None,
        custom_id: str | None = None,
    ) -> JobHandle:
        assert self.signer is not None and self.cipher is not None
        owner = self.signer.address
        units_in, units_out = _declare_units(payload_input, declared_units_out)
        ctx = await self.chain_context()
        # The bid, in the atomic units the order signs; the market replaces it below.
        atomic_in = usd_atomic(rate_in, "rate_in", ctx.decimals)
        atomic_out = usd_atomic(rate_out, "rate_out", ctx.decimals)
        model_id = await self._model_id(model)

        # -- the recipient, and therefore the container ----------------------
        #
        # Both order paths converge on one container. The only difference between
        # them is who the seed is sealed to — a provider's box key, or the
        # coordinator's verified escrow key for an open order — and that choice
        # is invisible from outside the wrap.
        #
        # A pinned order knows its recipient. An unpinned one does not until the
        # network answers, and the network answers only a *signed* order — so
        # it asks first with a **probe**: the same terms over a placeholder
        # commitment, never posted, whose `402` names the clearing candidates.
        # The client takes the first, seals to it, signs the real order and
        # challenges again for that order's own quote. Three requests instead of
        # two, and the bytes still cross the wire exactly once. **The open path
        # fails closed** inside `_matched_recipient`, before a single byte is
        # posted.
        #
        # **No bid named is the market.** An unsigned probe asks the node for
        # every live ask ranked; the order then bids the first candidate's own
        # ask, pinned to it. With `provider=` the probe is pinned too, so the
        # answer is that provider's ask or nothing.
        if rate_in is None and rate_out is None:
            recipient, designated, atomic_in, atomic_out = await self._market(
                provider, confidential, model, model_id, window, units_in, units_out, ctx,
            )
        elif provider is not None:
            recipient, designated = await self._recipient(provider, confidential)
        else:
            probe = self._sign_order(
                None, 0, payload_input, owner, custom_id, model_id, window,
                atomic_in, atomic_out, units_in, units_out, ctx,
            )
            _, challenge = await self._challenge(
                probe.vorq, probe.job_id, ctx, probe.terms.expires_at
            )
            recipient, designated = await self._matched_recipient(
                _candidates(challenge), confidential
            )
        order = self._sign_order(
            recipient, designated, payload_input, owner, custom_id, model_id, window,
            atomic_in, atomic_out, units_in, units_out, ctx,
        )
        container, job_id, vorq = order.container, order.job_id, order.vorq
        expires_at = order.terms.expires_at
        assert container is not None

        # -- phase 1: the terms-only challenge -------------------------------
        #
        # No bytes leave the client here. The quote is arithmetic over the signed
        # terms plus one cached gas read, so the challenge needs no payload — and
        # a client that sent one anyway would be told to sign a quote and come
        # back, uploading the same megabytes a second time.
        amount, _ = await self._challenge(vorq, job_id, ctx, expires_at)

        # The container's one field on the wire, resolved **once** and on first
        # use: a 409 re-quote below changes only the payment, never the bytes, so
        # filing them again on every attempt would be a wasted upload per
        # re-quote. Deferred rather than computed here because *when* the bytes
        # are filed decides how much of the node's 300 s orphan window a
        # submission spends before it can attach them — see `_container_field`.
        content_field: dict[str, str] | None = None

        # -- phase 2: the complete submission ---------------------------------
        #
        # The container is on this request and never on a challenge, so a `402`
        # answered to it is a protocol violation rather than a loop to run: the
        # node's own `neverChallengeAfterBytes` hook rewrites such an answer to a
        # `400 container_without_payment`. The one status that legitimately sends
        # a complete submission back is a `409` carrying a `quote` — the gas fee
        # drifted — and it arrives in the `402`'s own body shape, which is why
        # "sign this quote" is one code path here whatever status delivered it.
        # The two `409`s are told apart by **body** and never by status.
        for _ in range(MAX_SUBMIT_ATTEMPTS):
            # The same flat fields the challenge sent, plus the container's
            # field and the payment. Only the payment moves between attempts, so
            # a drifting gas fee never costs a fresh seed, a fresh `c` or a
            # second upload of the payload.
            # The payment first, the container second, and as two statements
            # rather than one literal: the order of effects is the point, and
            # dict ordering would not enforce it.
            auth_sig = self.signer.sign_payment_authorization(
                amount=amount, job_id=job_id, expires_at=expires_at, ctx=ctx,
            )
            if content_field is None:
                content_field = await self._container_field(container)
            fields = {
                **vorq,
                **content_field,
                "auth_sig": auth_sig,
                "amount": format_usd(amount, ctx.decimals),
            }
            sent = await self._send_complete(fields, job_id)
            if isinstance(sent, dict):  # the pre-check found it already posted
                return self._handle_from_job(sent, window)
            if sent.status_code == 402:
                raise VorqError(
                    "the node answered 402 to a body carrying a container. A "
                    "container makes a body a complete submission, which is never "
                    "answered with a quote — looping on this would re-upload the "
                    "whole payload to be quoted again.",
                    type="api_error",
                    status_code=402,
                )
            if sent.status_code == 409:
                refusal = sent.json()
                if isinstance(refusal, dict) and "quote" in refusal:
                    # Not a challenge: a refusal with the remedy attached. The
                    # terms and `c` are unchanged, so the container is reused
                    # verbatim and only the payment is re-signed.
                    amount = self._quote(refusal, job_id, ctx, expires_at)
                    continue
                raise self._error(sent)  # the chain refused it
            return self._handle_from_job(sent.json(), window)
        raise VorqError(
            f"the node re-quoted this submission {MAX_SUBMIT_ATTEMPTS} times without "
            "accepting it; the gas fee is drifting faster than the order can be "
            "signed, so the container is not uploaded again",
            type="api_error",
            status_code=409,
        )

    async def _market(
        self, provider: int | None, confidential: bool, model: str, model_id: int,
        window: str, units_in: int, units_out: int, ctx: ChainContext,
    ) -> tuple[str, int, int, int]:
        """Seal target, pin and atomic rates for an order that names no bid: the first ask the node ranks.

        The market probe is `POST /v1/jobs` with no rates and no signature: it
        commits to nothing, so it costs no wallet prompt, and the node answers
        `402` with the live asks ranked and no quote.
        """
        resp = await self._request(
            "POST", "/v1/jobs",
            json={"model_id": model_id, "sla_secs": sla_seconds(window), "units_in": units_in,
                  "units_out": units_out, "designated": provider or 0},
            retry=False, allow_statuses=frozenset({402}),
        )
        if resp.status_code != 402:
            raise VorqError(
                f"POST /v1/jobs answered {resp.status_code} to a market probe; only a 402 "
                "naming the candidates is a valid answer to one",
                type="api_error", status_code=resp.status_code,
            )
        candidates = _candidates(resp.json())
        if not candidates:
            by = f"provider {provider} is not" if provider is not None else "no provider is"
            raise ValidationError(
                f"{by} serving {model} in the {window} window right now; nothing was "
                "signed. Try another window or model, or pass rate_in= and rate_out= to "
                "post a bid that rests until a provider takes it",
                type="invalid_request_error",
            )
        if confidential:
            assert self.verifier is not None  # submit() refuses confidential without one
            named = len(candidates)
            candidates = await self.verifier.verify_candidates(candidates)
            if not candidates:
                raise VerificationError(
                    f"none of the {named} candidate(s) the challenge named verified "
                    "against the allowlist; nothing was sealed"
                )
        chosen = candidates[0]
        pid = int(chosen["provider_id"])
        if provider is not None and pid != provider:
            raise VorqError(
                f"the node answered a probe pinned to provider {provider} with provider {pid}",
                type="api_error", status_code=402,
            )
        if "rate_in" not in chosen or "rate_out" not in chosen:
            raise VorqError(
                f"the node named provider {pid} without its ask, so there is no rate to bid",
                type="api_error", status_code=402,
            )
        try:
            rate_in = usd_atomic(chosen["rate_in"], "rate_in", ctx.decimals)
            rate_out = usd_atomic(chosen["rate_out"], "rate_out", ctx.decimals)
        except ValidationError as exc:
            raise VorqError(
                f"the node named provider {pid} with an unreadable ask: {exc}",
                type="api_error", status_code=402,
            ) from exc
        return chosen["box_key"], pid, rate_in, rate_out

    def _sign_order(
        self, recipient_box_key: str | None, designated: int, payload_input: dict,
        owner: str, custom_id: str | None, model_id: int, window: str,
        rate_in: int, rate_out: int, units_in: int, units_out: int,
        ctx: ChainContext,
    ) -> _SignedOrder:
        """Seal (or, for a probe, placeholder) the container and sign the order over it.

        With no recipient this is a **probe**: `c` is 32 random bytes and nothing
        is sealed. The node checks a challenge's signature and
        `job_id = keccak(owner ‖ c)` and never its bytes, so a probe is a complete
        order to challenge with and an impossible one to post — it names no
        container that exists.
        """
        assert self.signer is not None
        if recipient_box_key is None:
            container, c = None, secrets.token_bytes(32)
        else:
            container, c = self._seal_container(recipient_box_key, payload_input, owner, custom_id)
        job_id = content_job_id(owner, c)
        terms = OrderTerms(
            c=c,
            model_id=model_id,
            sla_secs=sla_seconds(window),
            rate_in=rate_in,
            rate_out=rate_out,
            units_in=units_in,
            units_out=units_out,
            designated=designated,
            expires_at=self._expires_at(window),
        )
        vorq = terms.to_wire(
            owner=owner, job_id=job_id, signature=self.signer.sign_order_v2(terms, ctx),
            decimals=ctx.decimals,
        )
        return _SignedOrder(container, job_id, terms, vorq)

    async def _challenge(
        self, vorq: dict, job_id: str, ctx: ChainContext, expires_at: int
    ) -> tuple[int, dict]:
        """The terms-only `POST /v1/jobs`: the amount it quotes, and the raw body."""
        resp = await self._request(
            "POST", "/v1/jobs", json=vorq,
            retry=False, allow_statuses=frozenset({402}),
        )
        # A terms-only body has exactly one answer, and it is the quote. The node
        # returns `402` unconditionally when `auth_sig` is absent, and a success
        # here would mean a job posted with **no container**, which names no task
        # and is refused on chain as `EmptyTaskCid`. So a non-402 is not a
        # shortcut, it is a server this client does not know.
        if resp.status_code != 402:
            raise VorqError(
                f"POST /v1/jobs answered {resp.status_code} to a terms-only body; "
                "only a 402 quote is a valid answer to one, because a job posted "
                "without a container names no task and is refused on chain",
                type="api_error",
                status_code=resp.status_code,
            )
        body = resp.json()
        return self._quote(body, job_id, ctx, expires_at), body

    async def _matched_recipient(
        self, candidates: list[dict], confidential: bool
    ) -> tuple[str, int]:
        """The seal target an unpinned order takes from the challenge's candidates.

        The first candidate, as the node ranked it — or, under ``confidential``,
        the first whose record attests. An empty list means nothing clears the
        bid: the order rests as an escrowed open bid, which is the one path
        ``confidential`` cannot take, so there it is a refusal.
        """
        if not candidates:
            if confidential:
                raise VerificationError(
                    "no candidate cleared this bid, so it would rest as an escrowed "
                    "open order — which cannot be an attested channel. Resubmit "
                    "without confidential=True, or raise the bid."
                )
            return await self._escrow_recipient(), 0
        if confidential:
            assert self.verifier is not None  # submit() refuses confidential without one
            verified = await self.verifier.verify_candidates(candidates)
            if not verified:
                raise VerificationError(
                    f"none of the {len(candidates)} candidate(s) the challenge named "
                    "verified against the allowlist; nothing was sealed"
                )
            candidates = verified
        chosen = candidates[0]
        return chosen["box_key"], int(chosen["provider_id"])

    def _expires_at(self, window: str) -> int:
        """When the order stops being postable, clamped to the chain's own ceiling.

        The payment authorization must outlive the work, so the SLA window plus
        :data:`SETTLEMENT_MARGIN` is the wanted value — but ``JobRegistry``
        refuses anything past ``now + 86400``, and the ``24h`` window already
        reaches it. Clamping here is what keeps the ordinary long-window
        submission from being a ``400`` at the door.
        """
        now = int(time.time())
        return now + min(sla_seconds(window) + SETTLEMENT_MARGIN, MAX_EXPIRY_SECONDS)

    def _quote(self, body: Any, job_id: str, ctx: ChainContext, expires_at: int) -> int:
        """Read a ``402``/``409`` quote, refusing one that is not about this job.

        **This client signs what it derives; the quote supplies the amount and
        nothing else.** ``quote.amount`` is a USD string; the atomic integer it
        names at the token's decimals is what is signed, and the quoted
        authorization's ``value`` must be that same integer. The block is read member
        for member and has to agree: a node that describes a different
        authorization than the one this client is about to sign is a node on
        another deployment, and signing anyway would produce an unclaimable job
        rather than a refusal.
        """
        quote = body.get("quote") if isinstance(body, dict) else None
        auth = quote.get("authorization") if isinstance(quote, dict) else None
        if not isinstance(quote, dict):
            raise VorqError(
                "the node answered a challenge with no quote to sign",
                type="api_error", status_code=402,
            )
        if not isinstance(auth, dict) or "amount" not in quote:
            raise VorqError(
                "the node's quote names no amount and authorization this client can check",
                type="api_error", status_code=402,
            )
        amount = wire_atomic(quote["amount"], "quote.amount", ctx.decimals, 402)
        domain = auth.get("domain") if isinstance(auth.get("domain"), dict) else {}
        want = payment_domain(ctx)

        def same(a: Any, b: str) -> bool:
            return isinstance(a, str) and a.lower() == b.lower()

        def num(v: Any) -> int | None:
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        agrees = (
            num(domain.get("chainId")) == ctx.chain_id
            and same(domain.get("verifyingContract"), ctx.usdc)
            and domain.get("name") == want["name"]
            and domain.get("version") == want["version"]
            and same(auth.get("to"), ctx.job_registry)
            and same(auth.get("nonce"), job_id)
            and num(auth.get("value")) == amount
            and num(auth.get("valid_after")) == 0
            and num(auth.get("valid_before")) == expires_at + 1
        )
        if not agrees:
            raise VorqError(
                "the quoted authorization is not the one this client derives for this job "
                "and deployment", type="api_error", status_code=402,
            )
        return amount

    async def _container_field(self, container: bytes) -> dict[str, str]:
        """The container's one field on the wire — decided **once** per submission.

        Inline as base64 (``container``) at or under :data:`INLINE_MAX_BYTES`;
        otherwise filed first (``POST /v1/files``, ``purpose=input``) and named
        by ``container_cid``, the upload's ``vorq.cid``. Called once per
        submission, from inside :meth:`_submit_encrypted`'s re-quote loop but
        memoised by its caller, so a `409` re-quote changes only the payment and
        never re-files the bytes.

        **Called after the payment is signed, not before.** An upload nobody
        attaches is deleted by the node after 300 s, and signing is where a
        submission can wait on a person — a hardware wallet, a remote signer, the
        browser dialog the JS SDK shares this flow with. Filing ahead of it
        spends that window on a prompt, and an upload the sweep collects makes a
        post the node refuses ``unknown_container`` and marks not retryable. The
        payment does not depend on the container, so signing first costs nothing
        and a caller who declines never uploads at all.
        """
        if len(container) <= INLINE_MAX_BYTES:
            return {"container": base64.b64encode(container).decode()}
        uploaded = await self._upload_file(
            "container", "input", container, "application/octet-stream"
        )
        cid = (uploaded.get("vorq") or {}).get("cid")
        if not isinstance(cid, str) or not cid:
            # Refused here, rather than posting ``container_cid: None``: the node
            # answers that body ``container_required``, which reads as "this
            # client forgot the container" and sends the caller looking in the
            # wrong place. The real fault is an upload answer with no name in it,
            # and no re-post can put it right.
            raise VorqError(
                f"POST /v1/files answered no vorq.cid for file {uploaded.get('id')}, so "
                "there is no name to post this container against",
                type="api_error",
            )
        return {"container_cid": cid}

    async def _send_complete(
        self, fields: dict[str, Any], job_id: str
    ) -> "httpx.Response | dict":
        """POST the complete submission; on a dropped connection, read before rewriting.

        Returns the response, or the job dict when the connection dropped and the
        job turned out to be posted after all.

        JSON, flat — ``fields`` already carries the order, the payment, and
        whichever of the container's two wire shapes :meth:`_container_field`
        decided. Nothing here uploads: that already happened, once, before this
        was ever called — so the dropped-connection retry below simply re-posts
        this same body, never a second upload.

        **The pre-check is the mechanism, not an optimisation, and it must not be
        replaced by server-side idempotency later.** The coordinator investigated
        exactly that and proved it impossible: its ``jobs`` table is a pure
        projection of the ``Posted`` event, which carries *neither* signature, and
        every field a comparison could be built from — owner, ``c``, the rates,
        the unit counts, ``expiresAt``, even ``task_cid``, whose mint is
        content-addressed — is public calldata that a front-runner copies verbatim
        out of the mempool. A server comparing those columns would hand an
        attacker's job row back to this client as its own, complete with a
        ``job_id``, a ``task_cid`` and a ``tx_hash``, for a job whose ``auth_sig``
        is garbage and which no provider can ever claim. That is strictly worse
        than the refusal it would replace.

        What makes the client side sound is the one thing the server does not
        have: this client knows ``job_id = keccak(owner ‖ c)`` *before* it sends,
        because it chose the seed and built the container. So it can name the job
        and ask. A hit is its own job by construction — nobody else can produce
        this ``c`` without the seed inside the wrap — and a miss is a genuine
        absence, which is the only case where re-uploading is right.
        """
        async def post() -> httpx.Response:
            return await self._request(
                "POST", "/v1/jobs", json=fields, retry=False,
                allow_statuses=frozenset({402, 409}),
            )

        try:
            return await post()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            posted = await self._posted_job(job_id)
            if posted is not None:
                return posted
            # Genuinely absent: the bytes never landed, so send them once more.
            # If this one drops too, it raises — an unbounded rewrite loop is the
            # same defect as an unbounded 402 loop wearing a different hat.
            try:
                return await post()
            except (httpx.TimeoutException, httpx.TransportError):
                raise exc from None

    async def _posted_job(self, job_id: str) -> dict | None:
        """``GET /v1/jobs/{id}`` — the job, or ``None`` if the node has no such row."""
        try:
            resp = await self._request("GET", f"/v1/jobs/{quote(job_id, safe='')}")
        except NotFoundError:
            return None
        except VorqError:
            # The read failed for some other reason; treat it as "unknown" and let
            # the caller re-send rather than swallow a submission.
            return None
        return resp.json()

    async def chain_context(self) -> ChainContext:
        """The deployment this client signs for (``GET /evm/chain``), read once.

        All five contract addresses are carried, not the four this SDK itself
        uses. ``provider_registry`` is the fifth, and dropping it would be the
        expensive kind of omission: the JobRegistry and ProviderRegistry EIP-712
        domains share a name, a version and a chain id, so ``verifyingContract``
        is the entire difference between them. A signature under the wrong one
        does not fail — it recovers to a stranger, and nothing on the wire can
        tell that from a forgery.
        """
        if self._chain is None:
            resp = await self._request("GET", "/evm/chain")
            self._chain = ChainContext.from_wire(resp.json())
        return self._chain

    async def _model_id(self, model: str) -> int:
        """Resolve a model name to the catalog's numeric id.

        The order signs a ``uint32``, because that is what the registry stores. A
        name the catalog does not carry is an error here rather than a `0` that
        would post an order against whatever model happens to be first.
        """
        for entry in await self.models.list():
            if entry.get("id") != model:
                continue
            model_id = (entry.get("vorq") or {}).get("model_id")
            if model_id is None:
                break
            return int(str(model_id))
        raise ValidationError(
            f"the coordinator's catalog carries no numeric model_id for {model!r}, "
            "and an order signs the id rather than the name",
            type="invalid_request_error",
        )

    async def _recipient(self, provider: int | None, confidential: bool) -> tuple[str, int]:
        """Who a **pinned** payload is sealed to, and what ``designated`` says about it.

        A named provider is sealed to its registry ``box_key``; with no pin the
        caller reaches this only as the fallback of :meth:`_matched_recipient`,
        which is the escrow key. ``designated`` is the provider id or ``0`` — the
        contract's own sentinel for "any provider", never a null (Q22).
        """
        if provider is not None:
            record = await self._provider_record(provider)
            if confidential:
                assert self.verifier is not None  # submit() refuses confidential without one
                # An unverified pin is a refusal and never a substitution: this
                # raises rather than quietly sealing to somebody else.
                await self.verifier.verify_record(record)
            box_key = record.get("box_key")
            if not isinstance(box_key, str) or not box_key:
                raise VerificationError(
                    f"provider {provider} publishes no box_key, so there is nobody "
                    "to seal this payload to"
                )
            return box_key, provider
        return await self._escrow_recipient(), 0

    async def _provider_record(self, provider: int) -> dict:
        """``GET /evm/providers/{id}`` — the registry record, straight from the index."""
        resp = await self._request("GET", f"/evm/providers/{int(provider)}")
        record = resp.json()
        if not isinstance(record, dict):
            raise VerificationError(f"the provider record for {provider} is not an object")
        return record

    async def _escrow_recipient(self) -> str:
        """The coordinator's **verified** escrow public key, for an open order.

        Reached only when the challenge named no candidate — nothing on the book
        clears the bid, so it rests. Cached for :data:`KEY_CACHE_TTL`. Fail closed, and this is the whole of
        Q17: an open order's payload is sealed to this key and to nothing else,
        so a key that cannot be shown to be what it claims raises
        :class:`~vorq.errors.EscrowKeyUnverified` and **nothing is posted**.

        There is no automatic fallback to a designated bid. Re-targeting is the
        caller's decision — ``submit(..., provider=N)`` — because an SDK that
        picked one here would have turned a fail-closed into a fail-quiet, and
        the caller would never learn that the order it thought was
        escrow-protected went to a provider it did not choose.

        A client built without ``verifier=`` cannot verify anything, so it cannot
        post an open order at all. That is not a gap in the check; it is the
        check: "unverifiable" includes "no way to verify".
        """
        now = time.time()
        if self._escrow_key is not None and now < self._escrow_key_expires_at:
            return self._escrow_key
        if self.verifier is None:
            raise EscrowKeyUnverified(
                "an open order seals its payload to the coordinator's escrow key, "
                "and this client has no verifier to check that key's evidence "
                "with. Build the client with verifier=vorq.Verifier(...), or name "
                "a provider with submit(..., provider=N). Nothing was posted."
            )
        resp = await self._request("GET", "/key")
        try:
            key = await self.verifier.verify_escrow_key(resp.json())
        except VerificationError as exc:
            raise EscrowKeyUnverified(
                f"the coordinator's escrow key did not verify ({exc}), so this open "
                "order was not posted. Name a provider with submit(..., provider=N) "
                "to re-target it deliberately."
            ) from exc
        self._escrow_key = key
        self._escrow_key_expires_at = now + KEY_CACHE_TTL
        return key

    def _envelope(self, payload_input: dict, custom_id: str | None = None) -> dict[str, Any]:
        """The ``vorq-env-v1`` plaintext: who ordered, where to send the result back,
        and the model input.

        Sealing the owner and the result key alongside the payload takes them off
        the wire entirely — whoever opens the box is the only party that learns
        which client the task belongs to and which key to seal the result to.
        """
        assert self.signer is not None and self.cipher is not None
        envelope: dict[str, Any] = {
            "v": ENVELOPE_VERSION,
            "owner": self.signer.address,
            "result_key": self.cipher.public_key,
            "input": payload_input,
        }
        if custom_id is not None:
            # Absent rather than null when unset: this dict is canonicalized into the
            # commitment preimage, so a key that carries no meaning must not be there.
            envelope["custom_id"] = custom_id
        return envelope

    def _seal_container(
        self, recipient_box_key: str, payload_input: dict, owner: str,
        custom_id: str | None = None,
    ) -> tuple[bytes, bytes]:
        """Seal one payload into a container v1. Returns ``(container, c)``.

        Envelope → canonical JSON → fresh **seed** → DEK derived from the seed and
        this order's owner → ``SecretBox`` over the bulk → ``SealedBox`` over the
        **seed** to ``recipient_box_key`` → ``version ‖ wrap ‖ ct``.

        No size check here: there is no cap on a container any more, in a single
        job or in a batch line — the caller decides how the bytes reach the wire
        (inline, or uploaded and referenced by cid).

        **The wrap seals the seed, not the DEK, and that is load-bearing.** The
        wrap is public — anyone who knows a container's name can fetch it — so an
        attacker can lift this wrap verbatim, wrap a fresh commitment around it,
        post and claim their own dust order, and ask the escrow to open it. Every
        field of that request is honest and no check over public data can refuse
        it. What refuses it is this line: the DEK is
        ``HKDF-SHA256(seed, info=b"vorq-dek" ‖ owner20)``, and the party
        opening the wrap derives with **the job's owner as the chain reports it**
        — so the attacker gets a key derived under their own address, which does
        not open these bytes.

        Sealing the DEK here instead would still produce a byte-perfect container
        and a job **no provider could ever decrypt**. Nothing structural catches
        that; `tests/test_container.py` pins the derivation's inputs instead.

        The seed never leaves this process except inside the wrap, and the DEK
        never leaves it at all: there is no key on the wire and no key field on
        the order.
        """
        assert self.signer is not None and self.cipher is not None
        plaintext = _canonical_bytes(self._envelope(payload_input, custom_id))
        seed = new_seed()
        ciphertext = encrypt_under_dek(plaintext, derive_dek(seed, owner))
        wrap = seal_seed_to(recipient_box_key, seed)
        container = build_container(wrap, ciphertext)
        return container, commitment(wrap, keccak(ciphertext))

    def _handle_from_job(self, job: dict, window: str) -> JobHandle:
        """A handle over a job row or a post's success body.

        The two shapes differ: ``GET /v1/jobs/{id}`` answers a row keyed ``id``,
        while ``POST /v1/jobs`` answers ``{job_id, task_cid, tx_hash}``. Both name
        the job, and only the row carries terms.
        """
        # `vorq.sla_secs` on a row; a post receipt carries no terms at all, so
        # the window this submission asked for stands in.
        job_sla = window_from_seconds((job.get("vorq") or {}).get("sla_secs")) or window
        job_id = job.get("id") or job["job_id"]
        return JobHandle(
            self, job_id, sla=job_sla, job=job, cipher=self.cipher,
            # The client cannot compute this name and has nothing to read it from
            # until the post is indexed, so it is kept from the answer that minted
            # it. `None` on a row-shaped job that carries it under `vorq`.
            task_cid=job.get("task_cid") or (job.get("vorq") or {}).get("task_cid"),
        )

    def job(self, job_id: str) -> JobHandle:
        """Re-attach to an existing job from a persisted id — no network call."""
        return JobHandle(self, job_id)

    async def fetch_blob(self, cid: str) -> bytes:
        """Fetch content-addressed bytes by CID from the storage network's gateway.

        The read needs no authorization: the CID *is* the authorization. Knowing
        the name is the whole entitlement to the bytes, and a name nobody handed
        you is not one you can guess.

        The gateway is the only read path. The coordinator serves no blob
        endpoint, so a gateway failure raises rather than falling back to one —
        and a client built with ``gateway=""`` has no read path at all until it
        supplies one.

        Three ways out, and every one of them is a ``VorqError``: the bytes, an
        HTTP status the gateway answered with, or a connection that never
        answered at all. The last one is wrapped rather than left as the
        transport's own exception, because a caller holding one ``except
        VorqError`` should not have to also know which HTTP library reads the
        gateway.

        **A miss is waited out before it is believed.** These bytes were pinned
        seconds ago and a fresh name is not instantly resolvable on a read
        gateway, so the first ``404`` is propagation far more often than absence
        — see :data:`GATEWAY_ATTEMPTS`. What is *not* waited out is a gateway
        that answered with a refusal it will keep giving.
        """
        if self._gateway is None:
            raise VorqError(
                f"no gateway configured, so there is nowhere to read {cid} from: "
                "the coordinator serves no blob endpoint. Pass gateway=... or set "
                "$VORQ_PIN_GATEWAY.",
                type="invalid_request_error",
            )
        url = f"{self._gateway}/ipfs/{quote(cid, safe='')}"
        for attempt in range(GATEWAY_ATTEMPTS):
            last = attempt == GATEWAY_ATTEMPTS - 1
            try:
                resp = await self._http.get(url, follow_redirects=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if not last:
                    await asyncio.sleep(GATEWAY_BACKOFF_S)
                    continue
                # No status_code: nothing answered, so there is no status to carry.
                raise VorqError(
                    f"gateway read of {cid} could not reach {self._gateway}: {exc}",
                    type="api_error",
                ) from exc
            if resp.status_code >= 400:
                # 404 is "not yet" until the window closes; 5xx is the gateway
                # itself, which is the same kind of "ask again". Every other
                # status is an answer about *this name* — a scoped gateway
                # refusing it, a malformed CID — and no amount of waiting turns
                # it into bytes.
                if not last and (resp.status_code == 404 or resp.status_code >= 500):
                    await asyncio.sleep(GATEWAY_BACKOFF_S)
                    continue
                raise VorqError(
                    f"gateway read of {cid} failed with HTTP {resp.status_code}",
                    type="api_error",
                    status_code=resp.status_code,
                )
            return resp.content
        raise AssertionError("unreachable: the loop above returns or raises")

    # -- batch plumbing ----------------------------------------------------

    async def _upload_file(
        self, filename: str, purpose: str, content: bytes,
        content_type: str = "application/jsonl",
    ) -> dict:
        """``POST /v1/files`` — file the bytes and return the parsed file object.

        A multipart upload — ``purpose`` first, then the ``file`` part, the one
        order the node reads a files body in. The returned object carries `id`,
        `bytes`, `expires_at` and `vorq.cid` — the name a job or batch line
        references it by. A batch input file is already sealed line by line, so
        the file this holds carries routing terms and ciphertext and nothing a
        reader could act on; a single job's container is a caller's own bytes,
        sealed before this call ever sees them.
        """
        resp = await self._request(
            "POST",
            "/v1/files",
            files={"file": (filename, content, content_type)},
            data={"purpose": purpose},
            retry=False,
        )
        return resp.json()

    async def _seal_line(
        self,
        *,
        model: str,
        payload_input: dict,
        window: str,
        url: str,
        rate_in: int,
        rate_out: int,
        provider: int | None,
        units_out: int | None,
        custom_id: str | None,
        ctx: ChainContext,
    ) -> "SealedLine":
        """One batch input line: a complete sealed submission, ready to be filed.

        **A line is the body of ``POST /v1/jobs``**, and deliberately so — the same
        order, the same container, the same payment authorization, read on the other side
        by the same parser. What it does not have is the ``402`` exchange: the
        quote is arithmetic over terms this client signed, so `cap` is computed
        here and the only things that need the network — the fees on top of it —
        are read once for the whole batch rather than once per line. The rates are
        atomic, already converted at the token's decimals.
        """
        assert self.signer is not None and self.cipher is not None
        owner = self.signer.address
        units_in, declared_out = _declare_units(payload_input, units_out)
        recipient, designated = await self._recipient(provider, False)
        container, c = self._seal_container(recipient, payload_input, owner, custom_id)
        job_id = content_job_id(owner, c)
        terms = OrderTerms(
            c=c,
            model_id=await self._model_id(model),
            sla_secs=sla_seconds(window),
            rate_in=rate_in,
            rate_out=rate_out,
            units_in=units_in,
            units_out=declared_out,
            designated=designated,
            expires_at=self._expires_at(window),
        )
        return SealedLine(
            url=url,
            terms=terms,
            job_id=job_id,
            vorq=terms.to_wire(
                owner=owner, job_id=job_id, signature=self.signer.sign_order_v2(terms, ctx),
                decimals=ctx.decimals,
            ),
            container_b64=base64.b64encode(container).decode(),
        )

    def _pay_line(
        self, line: "SealedLine", gas_fee: int, ctx: ChainContext, fee_bps: int
    ) -> dict[str, Any]:
        """Authorize one sealed line and render the JSONL row.

        Separate from the sealing because the two need different things: sealing
        needs a recipient key and produces the commitment the order signs, and this
        needs the fees, which are one read for the whole batch. Split that way the
        file is sealed **once** — a client that read the fees after sealing and
        then re-sealed would mint a fresh seed, a fresh ``c`` and a fresh job id
        for every line it had already paid to encrypt.
        """
        assert self.signer is not None
        cap = _cap(line.terms)
        # the protocol fee rides on top of the cap, floored as the chain floors it
        amount = cap + cap * fee_bps // 10000 + gas_fee
        # Flat, like a single job: the order's fields, the payment and the
        # container side by side, always inline — a line is one JSON row inside
        # the uploaded batch input file, so there is nowhere else for its bytes to go.
        return {
            "url": line.url,
            **line.vorq,
            "container": line.container_b64,
            "auth_sig": self.signer.sign_payment_authorization(
                amount=amount,
                job_id=line.job_id,
                expires_at=line.terms.expires_at,
                ctx=ctx,
            ),
            "amount": format_usd(amount, ctx.decimals),
        }

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

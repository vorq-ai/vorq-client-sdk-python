"""Client-edge attestation verification for confidential submissions.

Opt-in, and the flag decides: a client built with ``verifier=`` uses it on
``submit(..., confidential=True)`` and on nothing else — model names carry no
confidentiality semantics. At provider-selection time the verifier checks the
provider's registry evidence against the on-chain measurement allowlist —
measurement active, report-data binding for THIS record, no debug flags, TCB
floor — and only then is the bid sealed to the record's key. There is no
verification middleman: the client reads chain state itself and dispatches on
the evidence type tag.

Modes: ``structural`` (default floor) runs every check except vendor-PKI quote
validation; ``mock`` additionally accepts mock-tagged evidence and mock
allowlist entries (dev only). Strict vendor-PKI validators register per
evidence type when real hardware lands; until then every non-mock tag is
unknown and refused — the verifier fails closed by construction.

Every input here is untrusted: the record comes from a provider, the allowlist
from whatever chain infrastructure the client chose. Malformed shapes refuse
with :class:`~vorq.errors.VerificationError` rather than crashing, and a check
that cannot be evaluated counts as a failed check.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import re
import time
from typing import Any, Callable

import httpx

from .errors import VerificationError

#: The provider daemon's mock tag, and the coordinator escrow's. They are
#: **distinct on purpose** and each is accepted on exactly one path: a verifier
#: that took either for either would accept a provider's evidence as proof about
#: the coordinator's escrow key, which is a different trust domain holding
#: different keys. The union below is the set of tags this SDK will look at at
#: all, and it is still empty of every real vendor tag until a strict validator
#: for one lands.
MOCK_PROVIDER_EVIDENCE_TYPE = "mock-cvm-v1"
MOCK_COORDINATOR_EVIDENCE_TYPE = "mock-coordinator-v1"
MOCK_EVIDENCE_TYPES = frozenset({MOCK_PROVIDER_EVIDENCE_TYPE, MOCK_COORDINATOR_EVIDENCE_TYPE})

#: A coordinator whose escrow key is **derived from its operator credential**
#: rather than minted inside a measured guest.
#:
#: Accepted in every mode, including the default ``structural``, and deliberately
#: **not** a member of :data:`MOCK_EVIDENCE_TYPES`. The mock tag is refused
#: outside mock mode because mock evidence is computable by anyone and a mock
#: node hands its whole key set to any caller; this tag makes a smaller and true
#: claim — the binding — and accepting it must not soften that guard.
#:
#: **Forward constraint.** This tag is the whole switch: nothing lets a client
#: say "I require measured escrow evidence" and refuse this one instead. That
#: is not a live regression today — every real vendor tag still raises, so
#: nothing can yet be downgraded to this one — but once a strict tier adds
#: real measured-evidence validators, an unconditional accept here would become
#: a silent downgrade path around it. It must be refused there.
STATIC_COORDINATOR_EVIDENCE_TYPE = "static-coordinator-v1"

#: The escrow's service id, **UTF-8 and unpadded**, stated as bytes at the one
#: place the digest is taken. It is a cross-language contract: a verifier that
#: padded it to 32 bytes, or encoded it UTF-16, would compute a different digest
#: and refuse every honest node.
ESCROW_SERVICE_ID = b"vorq-coordinator-escrow-v1"

#: How far ``GET /key``'s ``issued_at`` may be from this client's clock, either
#: way, in seconds. The same ±600 s bound the escrow applies to a release
#: request's own ``issued_at`` — one number for freshness across the protocol,
#: rather than a second one that could drift from it.
KEY_FRESHNESS_S = 600.0

#: How long a fetched allowlist stays usable. Revocation is the control for a
#: compromised image, so a long-lived client must re-read chain state: an
#: unbounded cache would keep accepting a revoked measurement forever.
DEFAULT_ALLOWLIST_TTL_S = 60.0

_MODES = ("structural", "mock")
_BOX_KEY_RE = re.compile(r"\A(?:0[xX])?[0-9a-fA-F]{64}\Z")  # 32-byte Curve25519 key
_ADDRESS_RE = re.compile(r"\A(?:0[xX])?[0-9a-fA-F]{40}\Z")  # 20-byte payee address
_MEASUREMENT_RE = re.compile(r"\A(?:0[xX])?[0-9a-fA-F]+\Z")  # hex digest, any width
_PROVIDER_ID_RE = re.compile(r"\A[0-9]{1,20}\Z")


def _box_key_bytes(value: object) -> bytes:
    """Parse a Curve25519 public key, refusing anything that is not 64 hex chars.

    ``bytes.fromhex`` tolerates whitespace and raises bare ``ValueError``; a
    hostile record must never reach either behavior.

    The ``0x`` prefix is optional for the same reason it is on an address and on
    a measurement: the node serves every ``bytes`` column prefixed, and a
    verifier that refused that spelling would fail **closed** on records that are
    entirely honest — dropping every candidate rather than raising, which is the
    silent-empty failure Q11 names.
    """
    if not isinstance(value, str) or not _BOX_KEY_RE.match(value):
        raise VerificationError(
            "record box_key is not a 32-byte hex Curve25519 key"
        )
    return bytes.fromhex(value.lower().removeprefix("0x"))


def _hex_key(value: str) -> str:
    """One spelling of a hex key, for comparison. Case and ``0x`` are both noise."""
    return value.lower().removeprefix("0x")


def _address_bytes(value: object) -> bytes:
    """Parse a payee address, refusing anything that is not 40 hex chars (0x optional)."""
    if not isinstance(value, str) or not _ADDRESS_RE.match(value):
        raise VerificationError("record operator is not a 20-byte hex account address")
    return bytes.fromhex(value.lower().removeprefix("0x"))


def _measurement(value: object) -> str | None:
    """Normalize a measurement for comparison, or ``None`` if it cannot be one.

    Case and an optional ``0x`` prefix are both spellings of the same digest, so
    normalize away both — on the evidence side and the allowlist side alike, or
    a revocation written in one spelling would leave the other live.
    """
    if not isinstance(value, str):
        return None
    if not _MEASUREMENT_RE.match(value):
        return None
    return value.lower().removeprefix("0x") or None


def _binding(box_key: object, address: object) -> str:
    return hashlib.sha256(
        _box_key_bytes(box_key) + _address_bytes(address)
    ).hexdigest()


def report_data(box_key: str, address: str) -> str:
    """Recompute the evidence binding ``sha256(box_pub ‖ wallet)`` from record fields.

    Raw bytes on both sides — the 32-byte key and the 20-byte address — so the
    digest is byte-identical to the one the attesting daemon put in its quote.
    Malformed inputs raise :class:`VerificationError`, never ``ValueError``.
    """
    return _binding(box_key, address)


def escrow_report_data(escrow_public_key: str) -> str:
    """The escrow binding: ``sha256(escrow_pk32 ‖ utf8("vorq-coordinator-escrow-v1"))``.

    A **different** construction from :func:`report_data`, and that is the point
    of it having its own function. A provider record binds a key to a payee
    address; the escrow announcement has no payee and no address field at all, so
    running it through the record binding computes a digest over a missing field
    and refuses every honest announcement. The service id is what stands in
    place of the address: it says which flow this evidence is about, so evidence
    minted for the key announcement cannot be presented as evidence about
    anything else.
    """
    return hashlib.sha256(_box_key_bytes(escrow_public_key) + ESCROW_SERVICE_ID).hexdigest()


def _tcb_svn(evidence: dict[str, Any]) -> int | float:
    """Extract a comparable TCB security-version number, or refuse.

    Refuses instead of raising on a non-dict ``tcb``, a missing ``svn``, a
    non-numeric ``svn`` (including ``bool``, which is not a version), and any
    non-finite float — an uncomparable TCB is a failed TCB check.
    """
    tcb = evidence.get("tcb")
    if tcb is None:
        raise VerificationError("evidence carries no TCB version")
    if not isinstance(tcb, dict):
        raise VerificationError("evidence TCB block is not an object")
    svn = tcb.get("svn")
    if isinstance(svn, bool) or not isinstance(svn, (int, float)):
        raise VerificationError("evidence TCB version is not a number")
    if isinstance(svn, float) and not math.isfinite(svn):
        raise VerificationError("evidence TCB version is not a finite number")
    return svn


class Verifier:
    """Client-edge attestation verifier over the public chain-state endpoints.

    The allowlist signature is deliberately not a dependency: the client reads
    chain state through infrastructure it chose, so the read itself is the root
    of trust and re-checking a curation signature buys nothing here.
    """

    def __init__(
        self,
        base_url: str,
        *,
        mode: str = "structural",
        min_tcb_svn: int = 1,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allowlist_ttl_s: float = DEFAULT_ALLOWLIST_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(
                f"unknown verifier mode: {mode!r} (use 'structural' or 'mock')"
            )
        if isinstance(min_tcb_svn, bool) or not isinstance(min_tcb_svn, int) or min_tcb_svn < 0:
            raise ValueError(f"min_tcb_svn must be a non-negative int, got {min_tcb_svn!r}")
        if (
            isinstance(allowlist_ttl_s, bool)
            or not isinstance(allowlist_ttl_s, (int, float))
            or not math.isfinite(allowlist_ttl_s)
            or allowlist_ttl_s < 0
        ):
            raise ValueError(
                f"allowlist_ttl_s must be a finite non-negative number, got {allowlist_ttl_s!r}"
            )
        if not callable(clock):
            raise ValueError(f"clock must be callable, got {clock!r}")
        if not callable(wall_clock):
            raise ValueError(f"wall_clock must be callable, got {wall_clock!r}")
        self._mode = mode
        self._min_tcb_svn = min_tcb_svn
        self._ttl = float(allowlist_ttl_s)
        # Two clocks, because they answer two different questions. The cache TTL
        # is an elapsed-time question and must not move when the system clock is
        # stepped, so it is monotonic. `issued_at` is a wall-clock instant the
        # node stamped, so comparing it needs a wall clock — a monotonic reading
        # is a number of seconds since an arbitrary origin and would refuse
        # every announcement ever made.
        self._clock = clock
        self._wall_clock = wall_clock
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._entries: list[dict] | None = None
        self._fetched_at = 0.0
        # Bumped by invalidate(); a read that started before the bump must not
        # write its answer back as the new cache. See _cached_entries.
        self._generation = 0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def allowlist(self) -> list[dict]:
        """Return the measurement allowlist, re-reading chain state once the TTL lapses.

        A transport or HTTP failure propagates loudly (nothing was verified);
        a malformed envelope is a verification failure — chain state we cannot
        read is chain state we do not trust. The caller gets a copy: nothing it
        does to the result can rewrite what later checks compare against.
        """
        return copy.deepcopy(await self._cached_entries())

    async def refresh(self) -> list[dict]:
        """Drop the cached allowlist and re-read it now (e.g. on a revocation notice)."""
        self.invalidate()
        return await self.allowlist()

    def invalidate(self) -> None:
        """Forget the cached allowlist; the next check re-reads chain state."""
        self._entries = None
        self._fetched_at = 0.0
        self._generation += 1

    def _is_expired(self, now: float) -> bool:
        """Whether the cached allowlist has aged out at ``now``.

        Negative elapsed time counts as expired: a clock that steps backwards
        would otherwise make ``now - fetched_at`` shrink forever and pin a
        stale — possibly revoked — allowlist for good.
        """
        elapsed = now - self._fetched_at
        return elapsed < 0 or elapsed >= self._ttl

    async def _cached_entries(self) -> list[dict]:
        async with self._lock:
            now = self._clock()
            if self._entries is not None and not self._is_expired(now):
                return self._entries
            generation = self._generation
            resp = await self._http.get("/evm/allowlist")
            resp.raise_for_status()
            # Only a well-formed answer replaces the cache; a failed re-read
            # raises rather than silently extending the stale window.
            entries = _parse_allowlist(resp)
            if generation == self._generation:
                self._entries = entries
                self._fetched_at = now
            # Else invalidate() landed while this read was in flight: the answer
            # predates the revocation that prompted it, so it serves this caller
            # but is not cached — the next check reads chain state again.
            return entries

    async def verify_record(self, record: dict) -> None:
        """Verify one provider registry record. Raises :class:`VerificationError`."""
        if not isinstance(record, dict):
            raise VerificationError("provider record is not an object")

        ev = record.get("evidence")
        if not isinstance(ev, dict):
            raise VerificationError("provider record carries no attestation evidence")

        ev_type = ev.get("type")
        if ev_type == MOCK_PROVIDER_EVIDENCE_TYPE:
            if self._mode != "mock":
                raise VerificationError("mock evidence is refused outside mock mode")
        else:
            # No validator is registered for this tag — including every real
            # vendor tag, until the strict tier lands. Fail closed.
            raise VerificationError(
                f"unrecognized evidence type {ev_type!r}: no validator for it"
            )

        await self._match_image_entry(ev.get("measurement"))

        # `operator`, which is what the record actually carries. The registry's
        # payee is `operatorOf(id)` on chain and `operator` on
        # `GET /evm/providers/{id}`; there is no `address` key on that wire, and a
        # verifier that read one computed the binding over `None` and refused
        # every honest record — so no confidential designated submission could
        # succeed at all. Found end to end, because the unit fixtures spelled it
        # the way this code did rather than the way the node does.
        expected = _binding(record.get("box_key"), record.get("operator"))
        got = ev.get("report_data")
        if not isinstance(got, str) or got.lower() != expected:
            raise VerificationError(
                "evidence does not bind this record's box key and payee"
            )
        # A check that cannot be evaluated is a failed check: evidence that never
        # states its debug status has not shown the payload is production-locked,
        # exactly as a missing TCB block below is not a passing TCB.
        debug = ev.get("debug")
        if not isinstance(debug, bool):
            raise VerificationError("evidence does not state a boolean debug flag")
        if debug:
            raise VerificationError("evidence carries a debug flag")
        if _tcb_svn(ev) < self._min_tcb_svn:
            raise VerificationError("TCB is below the required floor")

    async def verify_escrow_key(self, announcement: dict) -> str:
        """Verify ``GET /key`` and return the escrow public key it announces.

        **Its own path, with its own binding.** Routing this through
        :meth:`verify_record` would compute ``sha256(box_key ‖ address)`` over an
        ``address`` field the announcement does not have — and never will, because
        the escrow has no payee — so it would raise on every honest node while
        looking like a verification failure. The two share the checks that are
        genuinely shared (allowlist, debug, TCB) and nothing else.

        Raises :class:`~vorq.errors.VerificationError`; the caller turns that into
        the fail-closed :class:`~vorq.errors.EscrowKeyUnverified`, which is the
        error a user sees, because "this key did not verify" and "so nothing was
        posted" are two different statements and the second is the one that
        matters to somebody holding a wallet.
        """
        if not isinstance(announcement, dict):
            raise VerificationError("GET /key did not answer an object")

        key = announcement.get("escrow_public_key")
        if not isinstance(key, str) or not _BOX_KEY_RE.match(key):
            raise VerificationError(
                "GET /key announced no 32-byte hex escrow_public_key, so an open "
                "order has nobody to seal its payload to"
            )

        ev = announcement.get("evidence")
        if not isinstance(ev, dict):
            raise VerificationError("GET /key carries no attestation evidence")

        ev_type = ev.get("type")
        if ev_type == STATIC_COORDINATOR_EVIDENCE_TYPE:
            # An operator-keyed escrow. There is no measured image, so there is
            # no allowlist entry to resolve and no TCB level to floor: both are
            # questions about hardware this node does not claim to have, and a
            # verifier that asked them of evidence that never answered them would
            # refuse every honest static node. What is left — the binding, the
            # freshness bound and `debug` — is checked below exactly as it is for
            # every other tag.
            measured = False
        elif ev_type == MOCK_COORDINATOR_EVIDENCE_TYPE:
            if self._mode != "mock":
                raise VerificationError("mock evidence is refused outside mock mode")
            measured = True
        else:
            # Every real vendor tag included, until the strict tier lands — and
            # the provider mock, which is evidence about a different trust
            # domain and must not be accepted here.
            raise VerificationError(
                f"unrecognized escrow evidence type {ev_type!r}: no validator for it"
            )

        # Q23: a key announcement is a claim about *now*. Checked before the
        # allowlist read so a replayed announcement costs no chain traffic.
        issued_at = announcement.get("issued_at")
        if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
            raise VerificationError("GET /key states no numeric issued_at")
        if not math.isfinite(float(issued_at)):
            raise VerificationError("GET /key states a non-finite issued_at")
        skew = float(issued_at) - self._wall_clock()
        if abs(skew) > KEY_FRESHNESS_S:
            raise VerificationError(
                f"GET /key issued_at is {skew:+.0f}s from this clock, outside the "
                f"±{int(KEY_FRESHNESS_S)}s freshness bound: this announcement is a "
                "replay, or one of the two clocks is wrong"
            )

        if measured:
            await self._match_image_entry(ev.get("measurement"))

        expected = escrow_report_data(key)
        got = ev.get("report_data")
        if not isinstance(got, str) or got.lower() != expected:
            raise VerificationError(
                "GET /key evidence does not bind the escrow key it announces"
            )
        debug = ev.get("debug")
        if not isinstance(debug, bool):
            raise VerificationError("escrow evidence does not state a boolean debug flag")
        if debug:
            raise VerificationError("escrow evidence carries a debug flag")
        if measured and _tcb_svn(ev) < self._min_tcb_svn:
            raise VerificationError("escrow TCB is below the required floor")
        return key

    async def _match_image_entry(self, measurement: object) -> dict:
        """Resolve an evidence measurement to its allowlist image entry.

        Every matching entry must clear the check, not just the first one: a
        list that re-lists a revoked measurement as active must not resurrect
        it, and a mock entry must not be shadowed by a non-mock duplicate.
        """
        wanted = _measurement(measurement)
        if wanted is None:
            raise VerificationError("evidence carries no usable measurement")

        matches = [
            n
            for n in (_normalize_entry(e) for e in await self._cached_entries())
            if n["kind"] in _IMAGE_KINDS and _measurement(n["measurement"]) == wanted
        ]
        if not matches:
            raise VerificationError("measurement is not on the allowlist")
        if any(e["status"] == "revoked" for e in matches):
            raise VerificationError("measurement is revoked")
        if any(e["status"] != "active" for e in matches):
            raise VerificationError("measurement is not active on the allowlist")
        if any(e["mock"] for e in matches) and self._mode != "mock":
            raise VerificationError("mock allowlist entry is refused outside mock mode")
        return matches[0]

    async def verify_candidates(self, candidates: list[dict]) -> list[dict]:
        """Filter 402-challenge candidates to those whose record verifies AND whose
        challenge box key IS the verified record key — the seal target is pinned.

        Each candidate is ``{provider_id, box_key, rate_in, rate_out}`` as the node
        names it (``provider`` is accepted as the older spelling). The list comes
        back **in challenge order**: the node ranks, this only filters, so the
        first survivor is the seal target.
        """
        if not candidates:
            return []
        # Resolve chain state up front: an unreadable allowlist must surface,
        # not masquerade as "every provider failed verification".
        await self._cached_entries()

        kept: list[dict] = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            pid = _provider_path_id(c.get("provider_id", c.get("provider")))
            if pid is None:
                continue
            resp = await self._http.get(f"/evm/providers/{pid}")
            if resp.status_code == 404:
                # No such record: this candidate has nothing to attest, but the
                # read itself worked — the other candidates still stand.
                continue
            if resp.status_code != 200:
                # Chain state we cannot read is chain state we do not trust —
                # and an unreadable record must never masquerade as "this
                # provider failed verification", which would silently narrow
                # the field (or empty it) on an auth or infrastructure fault.
                raise VerificationError(
                    f"provider record for {pid} is unreadable "
                    f"(HTTP {resp.status_code}): chain state could not be read"
                )
            try:
                rec = resp.json()
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            # Transport sanity: a record that answers for a different provider id
            # means the read was misrouted. Confidentiality does not rest on this
            # (the box-key pin below does), but a lying reader should not be used.
            echoed = rec.get("provider")
            if echoed is not None and _provider_path_id(echoed) != pid:
                continue
            try:
                await self.verify_record(rec)
            except VerificationError:
                continue
            challenge_key, record_key = c.get("box_key"), rec.get("box_key")
            if not isinstance(challenge_key, str) or not _BOX_KEY_RE.match(challenge_key):
                continue
            # Two spellings of one key, compared after normalizing both — never as
            # strings. A prefixed record against a bare challenge is the same key.
            if not isinstance(record_key, str) or _hex_key(challenge_key) != _hex_key(record_key):
                continue
            kept.append(c)
        return kept


#: What counts as an image entry. Two spellings, because the curation contract's
#: entry blob says ``cvm-image`` and the flat form this verifier has always read
#: says ``image``; they name the same thing and a verifier that knew only one of
#: them would silently match nothing at all.
_IMAGE_KINDS = frozenset({"image", "cvm-image"})

#: The curation status vocabulary as the chain writes it — ``setAllowlistEntry``
#: takes ``{1 active, 2 revoked}``. Anything else normalizes to a status that is
#: neither, which is refused: a status this client cannot read is not a status it
#: may treat as active.
_NUMERIC_STATUS = {1: "active", 2: "revoked"}


def _normalize_entry(entry: dict) -> dict:
    """One allowlist entry in the shape the checks read, whichever shape it arrived in.

    Two shapes exist on the wire and both are real. The chain-projected one is
    ``{key, status: <int>, entry: {kind, measurement, …}}`` — the row the node
    serves, where ``status`` is the contract's integer and everything descriptive
    is inside the opaque blob curation filed. The flat one carries the same
    fields at the top level.

    Reading only the flat one is the failure mode Q11 describes on the sibling
    surface: no exception, no diagnostic, just an allowlist that matches nothing
    and a verifier that refuses every honest provider. So the nested form is
    normalized rather than assumed away, and an unreadable status becomes the
    string ``"unknown"``, which is neither ``active`` nor ``revoked`` and is
    therefore refused by the checks that follow.
    """
    inner = entry.get("entry")
    blob = inner if isinstance(inner, dict) else {}
    status = entry.get("status")
    if isinstance(status, bool):
        normalized_status: object = "unknown"
    elif isinstance(status, int):
        normalized_status = _NUMERIC_STATUS.get(status, "unknown")
    else:
        normalized_status = status if status is not None else blob.get("status")
    return {
        "kind": entry.get("kind", blob.get("kind")),
        "measurement": entry.get("measurement", blob.get("measurement")),
        "status": normalized_status,
        "mock": entry.get("mock", blob.get("mock")),
    }


def _parse_allowlist(resp: httpx.Response) -> list[dict]:
    """Validate the allowlist envelope shape before anything trusts its contents."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise VerificationError("allowlist response is not JSON") from exc
    if not isinstance(body, dict):
        raise VerificationError("malformed allowlist response: expected an object")
    entries = body.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise VerificationError("malformed allowlist: entries is not a list")
    for entry in entries:
        # A bare string would turn membership tests into substring tests, and a
        # non-mapping entry has no fields to check at all.
        if not isinstance(entry, dict):
            raise VerificationError("malformed allowlist: entry is not an object")
    return entries


def _provider_path_id(value: object) -> str | None:
    """Return a URL-safe provider id, or ``None`` if it cannot be one.

    Interpolating an untrusted value into the request path would let a hostile
    challenge point the record fetch at another endpoint or host.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and _PROVIDER_ID_RE.match(value):
        return value
    return None

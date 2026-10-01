"""Container v1 — the one byte layout a job's payload has on the wire.

::

    container = version ‖ seed_wrap ‖ ciphertext   version   = 0x01, 1 byte
                                                  seed_wrap = seal(recipient, SEED), 80 bytes
    c         = keccak256(version ‖ seed_wrap ‖ keccak256(ciphertext))
    job_id    = keccak256(owner ‖ c)

One payload, two recipients. The bulk is always encrypted under a 32-byte DEK
(``SecretBox``, nonce-prefixed); a 32-byte secret is always sealed to somebody
(``SealedBox``, 80 bytes). Who that somebody is — the provider named on a
designated order, or the coordinator's escrow key on an open one — is the only
difference between the two order paths, and it is invisible from outside the
wrap. Nothing else about the two submissions differs.

**The sealed 32 bytes are a SEED, and the DEK is derived from it.** This is the
one place a reader is most likely to assume otherwise, so it is stated first:

::

    dek = HKDF-SHA256(ikm = seed, salt = b"", info = b"vorq-dek" ‖ owner20, L = 32)

The wrap is public — a container is fetched by CID by anyone who knows the name —
so an attacker can lift a victim's wrap verbatim, mint a fresh commitment around
it, post and claim a dust order of their own, and ask the escrow to open it. Every
field of that request is honest, so no check over public data can refuse it. What
refuses it is the derivation: whoever unseals a wrap derives with **the job's
owner as the chain reports it**, so the attacker's key is derived under the
attacker's address and does not open the victim's ciphertext. The rule is
universal and symmetric, which is what protects the designated path — where the
wrap is sealed to a provider's own key and never reaches the coordinator — by the
identical argument.

The cost of that fix was zero format change: the wrap is still 80 bytes, the
commitment preimage is still ``version ‖ wrap ‖ ct_hash``, and
`tests/vectors/container-v1.json` pins the wrap as an opaque blob and asserts
nothing about its plaintext. **A client that
seals the DEK directly still produces byte-perfect containers and jobs no
provider can decrypt** — nothing structural catches it, which is why
`tests/test_container.py` pins the KDF's inputs as arithmetic rather than as a
round trip through this module.

**The commitment is the whole integrity story, and it is signed.** The client
signs ``c`` and never a CID: the name does not exist when the order is signed,
because the coordinator mints it when it pins. So the bytes are checked against
``c`` or they are not checked at all, and every party re-derives the same three
lines — this module, ``vorq-coordinator-node/src/container.ts``, and the provider
daemon's own copy. `tests/vectors/container-v1.json` is what makes that agreement
checkable rather than coincidental: it is the coordinator's committed file,
copied here unmodified.

Two properties are worth naming because they are what the layout buys:

* The **ciphertext enters through its digest**, never directly, so the
  commitment preimage is always exactly ``1 + 80 + 32 = 113`` bytes however large
  the payload is.
* The **wrap is inside the commitment**. A wrap lifted from another order over
  identical ciphertext produces a different ``c`` and therefore a different
  ``job_id`` — which is why ``c`` is not simply ``keccak(ciphertext)``.

This module reads no configuration and opens no socket.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Literal

from eth_utils import keccak
from nacl.secret import SecretBox

from ._crypto import seal_to
from .errors import VorqError

#: Container v1. Byte 0 of every container and the first byte of ``c``'s
#: preimage, so flipping it changes the commitment, changes the job id, and is
#: caught at ``verify_container`` before a claim is spent. A v2 container can
#: never be read as a v1 one, and no unauthenticated version metadata exists to
#: downgrade.
#:
#: There is deliberately no length field in the bytes: the version implies the
#: wrap's kind and its width together. A length in the container is
#: attacker-supplied data needing validation on every parse; a width in the code
#: is a constant with nothing to lie about.
CONTAINER_VERSION = 1

#: The wrap's width. A sealed-box wrap of a 32-byte secret: 32 bytes of ephemeral
#: public key + 16 bytes of MAC + 32 bytes of plaintext. Fixed width is what makes
#: the split a split — and it is unchanged by the seed rule, which moved the
#: *meaning* of the plaintext and not one byte of the layout.
SEED_WRAP_BYTES = 80

#: The shortest thing that could be a container at all — the version byte and the
#: wrap. An empty ciphertext is well formed (``keccak256("")`` is a real hash and
#: the vectors pin that case); a buffer too short to split is not a container.
MIN_CONTAINER_BYTES = 1 + SEED_WRAP_BYTES

#: The largest body the node's two byte-carrying doors read. The node's
#: ``MAX_BODY_BYTES``: one ceiling for both doors, and the only number here with
#: a choice behind it — :data:`INLINE_MAX_BYTES` is derived from it.
MAX_BODY_BYTES = 20 * 1024 * 1024

#: What the inline threshold leaves free for everything that is not content. The
#: order, the payment and a 65-byte signature come to well under a KiB; the
#: reserve is far larger on purpose, because reserving too little means inlining
#: a payload the door then refuses with a ``413``.
ENVELOPE_RESERVE_BYTES = 64 * 1024


def base64_length(n: int) -> int:
    """How many bytes base64 costs to encode ``n``, as the encoder counts them."""
    return 4 * ((n + 2) // 3)


#: Above this many sealed bytes, upload first and send ``container_cid`` instead
#: of inline base64.
#:
#: **Derived, not chosen.** This was a second constant, hand-written as 7 MiB
#: here, in the JS SDK, in the daemon and in the spec, with nothing holding the
#: four copies together — editing one left every suite green while the parties
#: silently disagreed about where the line was. It was never a second decision:
#: base64 costs a third again, which is the whole reason a client cannot inline a
#: body's worth of bytes into a body. Every party derives it the same way from
#: the same ceiling, and the cross-language fixture holds them to it.
#:
#: A container at this threshold encodes to exactly the room the reserve leaves
#: it: ``base64_length(n) + ENVELOPE_RESERVE_BYTES == MAX_BODY_BYTES``.
INLINE_MAX_BYTES = ((MAX_BODY_BYTES - ENVELOPE_RESERVE_BYTES) // 4) * 3


#: Why a buffer is not a container. The same three strings the node answers with.
#: ``bad_version`` and not ``bad_tag``: the check is a comparison against the one
#: version byte this build reads, and
#: a token naming a check that no longer exists is a wire-visible error code that
#: misdescribes what happened.
ContainerFault = Literal["too_short", "bad_version", "commitment_mismatch"]


class ContainerError(VorqError):
    """These bytes are not the container they claim to be.

    ``fault`` is the node's own vocabulary, so a locally-caught refusal and one
    that came back as a ``400 invalid_request`` naming ``container`` read the
    same. The ``400`` names ``container`` and not ``vorq.c`` because ``c`` is
    signed and the container is not — the container is the half a caller can put
    right.
    """

    def __init__(self, fault: ContainerFault, message: str) -> None:
        super().__init__(message, type="invalid_request_error")
        self.fault: ContainerFault = fault


# -- layout ---------------------------------------------------------------------


def build_container(seed_wrap: bytes, ciphertext: bytes) -> bytes:
    """``version ‖ seed_wrap ‖ ciphertext``.

    The wrap width is asserted rather than trusted: it is the offset every reader
    splits at, so a wrap of the wrong length does not produce a bad container, it
    produces a container that means something else.
    """
    if len(seed_wrap) != SEED_WRAP_BYTES:
        raise ContainerError(
            "too_short",
            f"seed_wrap must be exactly {SEED_WRAP_BYTES} bytes, got {len(seed_wrap)}",
        )
    return bytes([CONTAINER_VERSION]) + bytes(seed_wrap) + bytes(ciphertext)


def split_container(container: bytes) -> tuple[bytes, bytes]:
    """Split at the offsets byte 0 names, or refuse. Returns ``(seed_wrap, ciphertext)``.

    Length and version are checked **before** the split. Python slicing clamps out
    of range instead of raising, so without them an 80-byte buffer would sail
    through and yield a plausible-looking commitment over a slice of garbage.
    """
    if len(container) < MIN_CONTAINER_BYTES:
        raise ContainerError(
            "too_short",
            f"a container is at least {MIN_CONTAINER_BYTES} bytes — a version byte and an "
            f"{SEED_WRAP_BYTES}-byte seed_wrap — and this one is {len(container)}",
        )
    if container[0] != CONTAINER_VERSION:
        raise ContainerError(
            "bad_version",
            f"container byte 0 is 0x{container[0]:02x}; this build reads "
            f"0x{CONTAINER_VERSION:02x}",
        )
    return container[1 : 1 + SEED_WRAP_BYTES], container[1 + SEED_WRAP_BYTES :]


def commitment(seed_wrap: bytes, ct_hash: bytes) -> bytes:
    """``keccak256(version ‖ seed_wrap ‖ ct_hash)`` — 32 bytes.

    Takes the ciphertext's *digest*, never the ciphertext: the preimage is 113
    bytes for a one-byte payload and for a four-megabyte one alike.

    The version is this build's own and is not a parameter. A caller holding a
    wrap and a digest and nothing else — which is every caller in this SDK, and
    the coordinator's ``/release``, which never sees a container — has exactly one
    layout those two pieces could belong to.
    """
    if len(seed_wrap) != SEED_WRAP_BYTES:
        raise ContainerError(
            "too_short",
            f"a seed_wrap is exactly {SEED_WRAP_BYTES} bytes, got {len(seed_wrap)}",
        )
    if len(ct_hash) != 32:
        raise ValueError(f"ct_hash must be a 32-byte keccak digest, got {len(ct_hash)}")
    return keccak(bytes([CONTAINER_VERSION]) + bytes(seed_wrap) + bytes(ct_hash))


def commitment_of(container: bytes) -> bytes:
    """The commitment these container bytes reproduce (split, hash, commit).

    ``split_container`` has already refused any byte 0 that is not this build's
    version, so the preimage is built over the version the bytes carry and the
    version this build writes — which are the same byte or there is no split.
    """
    seed_wrap, ciphertext = split_container(container)
    return commitment(seed_wrap, keccak(ciphertext))


def assert_commitment(container: bytes, c: bytes | str) -> None:
    """Raise unless ``container`` is the payload ``c`` commits to.

    The mirror of the node's ``assertCommitment``. The client builds containers
    rather than receiving them, so this is a self-check on the way out — but it
    is the same function the node runs on the way in, and having one of them is
    what makes the refusal vectors assertable from this side.
    """
    computed = commitment_of(container)
    if computed != _bytes32(c):
        raise ContainerError(
            "commitment_mismatch",
            "the container does not reproduce c: "
            "keccak256(version ‖ seed_wrap ‖ keccak256(ciphertext)) is 0x"
            f"{computed.hex()}. A job posted over bytes that miss their commitment "
            "is a job every provider refuses to claim, with the escrow already "
            "committed",
        )


def _bytes32(value: bytes | str) -> bytes:
    """A 32-byte word from raw bytes or ``0x``-prefixed hex."""
    raw = bytes.fromhex(value[2:] if value.startswith("0x") else value) if isinstance(value, str) else bytes(value)
    if len(raw) != 32:
        raise ValueError(f"expected a 32-byte word, got {len(raw)} bytes")
    return raw


# -- the seed, the derivation, and the bulk cipher --------------------------------

#: The width of the sealed secret. Unchanged at 32 bytes — what changed is what
#: those bytes *are*.
SEED_LEN = 32

#: The HKDF ``info`` prefix, as **bytes**, spelled out at the one place the
#: derivation happens. It is a cross-language contract: the coordinator writes it
#: ``Buffer.from("vorq-dek", "utf8")`` and a Python side that used a `str`
#: through some helper that padded or re-encoded it would derive a different key
#: and fail only at runtime, in a provider, on a job that is already paid for.
#:
#: It carries **no version**, and that is deliberate. The container's version byte
#: is the one version namespace: it travels with the bytes it describes and it is
#: committed by ``c``. Seeds are freshly random per job, so no seed ever appears
#: under two formats and cross-version key confusion is impossible by
#: construction — the label's only job is domain separation.
DEK_INFO_PREFIX = b"vorq-dek"

#: RFC 5869's ``salt`` for this derivation: **zero length, explicitly**. §2.2 then
#: substitutes HashLen zero bytes, which is not the same thing as passing 32 zero
#: bytes as `ikm` and is not the same thing as omitting the argument in a library
#: whose default is something else.
DEK_SALT = b""


def _hkdf_sha256(*, ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256, extract-then-expand, in the stdlib.

    Written out rather than imported so the two steps are readable next to the
    inputs they are pinned against: a KDF two implementations disagree about
    fails silently and only at runtime. ``length`` here is never more than one
    block, but the counter loop is the real thing anyway — a truncated
    implementation that happens to be correct for L ≤ 32 is a trap for the next
    caller.
    """
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        # T(n) = HMAC(PRK, T(n-1) ‖ info ‖ n). `info` is inside every block and
        # not only the first: an expand that drops it produces a perfectly
        # well-formed 32 bytes that no other implementation reproduces, and the
        # only thing that catches it is a digest checked against one.
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _owner_bytes(owner: str | bytes) -> bytes:
    """The owner as 20 **raw** bytes, never as a hex string.

    Checksum casing is display-only, so a derivation over the string form would
    produce two different keys for one address depending on how a caller spelled
    it — and the two sides of this protocol spell it differently: the client
    holds a checksummed address from `eth-account`, the coordinator reads a
    lowercase one off the chain.
    """
    raw = bytes(owner) if isinstance(owner, (bytes, bytearray)) else bytes.fromhex(
        owner[2:] if owner[:2].lower() == "0x" else owner
    )
    if len(raw) != 20:
        raise ValueError(f"owner must be a 20-byte address, got {len(raw)} bytes")
    return raw


def new_seed() -> bytes:
    """A fresh 32-byte seed — the plaintext of the wrap, and not a key itself."""
    return secrets.token_bytes(SEED_LEN)


def derive_dek(seed: bytes, owner: str | bytes) -> bytes:
    """``HKDF-SHA256(ikm=seed, salt=b"", info=b"vorq-dek" ‖ owner20, L=32)``.

    The one function in this SDK whose output nothing local can check. Both sides
    of a job run it — this client to encrypt, and either the coordinator (open
    path, deriving under the owner it reads from chain) or the provider
    (designated path, deriving under the owner it reads from the job) to decrypt
    — and they never exchange the result. So the inputs are the contract, and
    they are pinned byte for byte by a test that recomputes them from `hmac` and
    `hashlib` directly rather than by calling this function twice.
    """
    if len(seed) != SEED_LEN:
        raise ValueError(f"seed must be {SEED_LEN} bytes, got {len(seed)}")
    return _hkdf_sha256(
        ikm=bytes(seed),
        salt=DEK_SALT,
        info=DEK_INFO_PREFIX + _owner_bytes(owner),
        length=SecretBox.KEY_SIZE,
    )


def seal_seed_to(recipient_public_key: str, seed: bytes) -> bytes:
    """Seal the 32-byte seed to a Curve25519 public key — exactly ``SEED_WRAP_BYTES`` bytes.

    An anonymous sealed box: only the recipient's public key is needed to write
    one, and only the recipient's secret key can open it. That is what lets a
    designated order name a provider and an open order name the coordinator's
    escrow key through the identical code path — and it is also why the wrap
    alone cannot be an authorization, since anyone can write one to any key.
    """
    if len(seed) != SEED_LEN:
        raise ValueError(f"seed must be {SEED_LEN} bytes, got {len(seed)}")
    wrap = seal_to(recipient_public_key, seed)
    if len(wrap) != SEED_WRAP_BYTES:  # pragma: no cover — libsodium's overhead is fixed
        raise ContainerError(
            "too_short", f"sealed seed is {len(wrap)} bytes, expected {SEED_WRAP_BYTES}"
        )
    return wrap


def encrypt_under_dek(data: bytes, dek: bytes) -> bytes:
    """Encrypt ``data`` under a **given** DEK. Returns the ciphertext.

    The DEK is an argument rather than something minted here: it is derived from
    the seed and the owner, so a function that generated its own would be a
    function that could not produce a decryptable job.

    The nonce is prepended by ``SecretBox``, so the ciphertext is self-describing
    and the container needs no framing beyond its version byte.
    """
    if len(dek) != SecretBox.KEY_SIZE:
        raise ValueError(f"dek must be {SecretBox.KEY_SIZE} bytes, got {len(dek)}")
    return bytes(SecretBox(dek).encrypt(data))


def open_dek(data: bytes, dek: bytes) -> bytes:
    """Open a DEK-encrypted payload (the mirror of :func:`encrypt_under_dek`)."""
    return SecretBox(dek).decrypt(data)

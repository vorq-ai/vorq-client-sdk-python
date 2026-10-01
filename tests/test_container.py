"""Container v1 — cross-repo parity against the coordinator's committed vectors.

`tests/vectors/container-v1.json` is `vorq-coordinator-node/test/vectors/container-v1.json`
copied byte-for-byte. Three implementations split and commit these bytes — the
coordinator's TypeScript, this SDK, and the provider daemon — and this file is
what makes their agreement checkable rather than coincidental. Two implementations
agreeing by luck is exactly what it prevents, so nothing here re-derives the
format from prose: every expected value is read out of the file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from eth_utils import keccak
from nacl.public import PrivateKey
from nacl.secret import SecretBox

from vorq._container import (
    CONTAINER_VERSION,
    DEK_INFO_PREFIX,
    DEK_SALT,
    ENVELOPE_RESERVE_BYTES,
    INLINE_MAX_BYTES,
    MAX_BODY_BYTES,
    MIN_CONTAINER_BYTES,
    base64_length,
    SEED_LEN,
    SEED_WRAP_BYTES,
    ContainerError,
    assert_commitment,
    build_container,
    commitment,
    commitment_of,
    derive_dek,
    encrypt_under_dek,
    new_seed,
    open_dek,
    seal_seed_to,
    split_container,
)
from vorq._crypto import content_job_id

OWNER = "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"


def _sealed(plaintext: bytes, owner: str = OWNER):
    """One payload as the client seals it: seed, derived DEK, ciphertext."""
    seed = new_seed()
    return seed, encrypt_under_dek(plaintext, derive_dek(seed, owner))

VECTORS = json.loads((Path(__file__).parent / "vectors" / "container-v1.json").read_text())
CASES = VECTORS["cases"]
REFUSALS = VECTORS["refusals"]
KDF = VECTORS["kdf"]


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)


def _ids(cases) -> list[str]:
    return [c["name"] for c in cases]


class TestConstants:
    """The three numbers the layout is, read off the vectors rather than typed in."""

    def test_the_version_byte_matches_the_vectors(self):
        assert CONTAINER_VERSION == VECTORS["constants"]["version"]
        assert bytes([CONTAINER_VERSION]) == _b(VECTORS["constants"]["version_byte"])

    def test_wrap_and_minimum_match_the_vectors(self):
        assert SEED_WRAP_BYTES == VECTORS["constants"]["wrap_bytes"]
        assert MIN_CONTAINER_BYTES == VECTORS["constants"]["min_container_bytes"]

    def test_the_body_ceiling_and_what_it_derives(self):
        """The one number with a choice behind it, and the threshold derived from it.

        Spelled out rather than referenced symbolically: this suite used to name
        ``INLINE_MAX_BYTES`` only by import, so editing it to any other value
        left every Python test green while the JS SDK, the daemon and the node
        went on believing the old one.
        """
        assert MAX_BODY_BYTES == 20 * 1024 * 1024
        assert ENVELOPE_RESERVE_BYTES == 64 * 1024
        assert INLINE_MAX_BYTES == 15_679_488
        # The property behind the derivation: a container at the threshold
        # encodes to exactly the room the reserve leaves it, so the ceiling can
        # move without anyone re-checking the threshold by hand.
        assert base64_length(INLINE_MAX_BYTES) + ENVELOPE_RESERVE_BYTES == MAX_BODY_BYTES
        assert base64_length(INLINE_MAX_BYTES + 1) + ENVELOPE_RESERVE_BYTES > MAX_BODY_BYTES
        # Counted the way the encoder counts, not approximated.
        for n in (0, 1, 2, 3, 61, 1024):
            assert base64_length(n) == len(base64.b64encode(b"\0" * n))

    def test_the_vector_file_is_the_coordinators(self):
        assert VECTORS["format"] == "vorq-container-v1"
        assert CASES and REFUSALS  # positive and negative, both present


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
class TestPositiveVectors:
    def test_build_container_reproduces_the_bytes(self, case):
        built = build_container(_b(case["seed_wrap"]), _b(case["ciphertext"]))
        assert built == _b(case["container"])

    def test_split_container_recovers_wrap_and_ciphertext(self, case):
        wrap, ct = split_container(_b(case["container"]))
        assert wrap == _b(case["seed_wrap"])
        assert ct == _b(case["ciphertext"])

    def test_ciphertext_hash_matches(self, case):
        assert keccak(_b(case["ciphertext"])) == _b(case["ct_hash"])

    def test_commitment_matches(self, case):
        c = commitment(_b(case["seed_wrap"]), _b(case["ct_hash"]))
        assert c == _b(case["c"])

    def test_commitment_of_the_whole_container_matches(self, case):
        assert commitment_of(_b(case["container"])) == _b(case["c"])

    def test_the_preimage_is_always_113_bytes(self, case):
        """The ciphertext enters through its digest, never directly."""
        wrap, _ = split_container(_b(case["container"]))
        assert 1 + len(wrap) + 32 == 113

    def test_job_id_is_keccak_of_owner_and_c_with_no_inner_hash(self, case):
        """A reintroduced inner keccak fails right here."""
        assert content_job_id(case["owner"], _b(case["c"])) == case["job_id"].lower()

    def test_job_id_accepts_the_commitment_as_hex_too(self, case):
        assert content_job_id(case["owner"], case["c"]) == case["job_id"].lower()

    def test_assert_commitment_accepts_the_matching_bytes(self, case):
        assert_commitment(_b(case["container"]), case["c"])


class TestWrapSwap:
    """A wrap lifted from another order over identical ciphertext is a different job.

    This is the property that justifies hashing the wrap into ``c`` rather than
    only the ciphertext. Drop the wrap from the commitment preimage and this test
    is the one that goes red.
    """

    def test_the_swapped_container_reproduces_the_vectors_c(self):
        swap = VECTORS["wrap_swap"]
        assert commitment_of(_b(swap["container"])) == _b(swap["c"])

    def test_and_it_differs_from_the_original(self):
        swap = VECTORS["wrap_swap"]
        assert _b(swap["c"]) != _b(swap["differs_from"])

    def test_the_swap_is_assembled_from_two_real_cases(self):
        swap = VECTORS["wrap_swap"]
        by_name = {c["name"]: c for c in CASES}
        ct = by_name[swap["ciphertext_from"]]
        wrap = by_name[swap["seed_wrap_from"]]
        rebuilt = build_container(_b(wrap["seed_wrap"]), _b(ct["ciphertext"]))
        assert rebuilt == _b(swap["container"])
        # The ciphertext is byte-identical to the one it was lifted from, and the
        # commitment is not.
        assert commitment_of(rebuilt) != _b(ct["c"])


@pytest.mark.parametrize("refusal", REFUSALS, ids=_ids(REFUSALS))
class TestRefusalVectors:
    """Each negative vector produces the fault it is labelled with, and no other."""

    def test_produces_its_labelled_fault(self, refusal):
        container = _b(refusal["container"])
        with pytest.raises(ContainerError) as exc:
            if refusal["fault"] == "commitment_mismatch":
                # Well formed, and reproduces *some* commitment — just never the
                # one the order signed, which the vector carries as `c`. Only a
                # check against that value sees it.
                assert_commitment(container, refusal["c"])
            else:
                commitment_of(container)
        assert exc.value.fault == refusal["fault"]

    def test_a_mismatch_vector_is_otherwise_well_formed(self, refusal):
        """The two mismatch cases pass every structural check; only `c` catches them."""
        if refusal["fault"] != "commitment_mismatch":
            return
        wrap, _ = split_container(_b(refusal["container"]))
        assert len(wrap) == SEED_WRAP_BYTES
        assert commitment_of(_b(refusal["container"])) != _b(refusal["c"])

    def test_the_fault_is_one_of_the_three_the_node_answers(self, refusal):
        assert refusal["fault"] in {"too_short", "bad_version", "commitment_mismatch"}


class TestSplitRefusesLocally:
    def test_a_buffer_one_byte_short_is_too_short(self):
        with pytest.raises(ContainerError) as exc:
            split_container(bytes([CONTAINER_VERSION]) + b"\x00" * (SEED_WRAP_BYTES - 1))
        assert exc.value.fault == "too_short"

    def test_the_minimum_is_a_container_with_an_empty_ciphertext(self):
        wrap, ct = split_container(bytes([CONTAINER_VERSION]) + b"\x00" * SEED_WRAP_BYTES)
        assert len(wrap) == SEED_WRAP_BYTES and ct == b""

    def test_an_unknown_version_byte_is_a_bad_version(self):
        """Byte 0 must equal `CONTAINER_VERSION`, so every other byte is one fault.

        There is no magic left to be wrong about separately: 0x00 and 0x02 are
        the same refusal as 0xff.
        """
        for byte in (0x00, 0x02, 0xFF):
            with pytest.raises(ContainerError) as exc:
                split_container(bytes([byte]) + b"\x00" * SEED_WRAP_BYTES)
            assert exc.value.fault == "bad_version"

    def test_build_refuses_a_wrap_of_the_wrong_width(self):
        with pytest.raises(ContainerError):
            build_container(b"\x00" * (SEED_WRAP_BYTES - 1), b"payload")


class TestSealSeedTo:
    def test_a_sealed_seed_is_exactly_the_wrap_width(self):
        recipient = PrivateKey.generate()
        pk = recipient.public_key.encode().hex()
        wrap = seal_seed_to(pk, new_seed())
        assert len(wrap) == SEED_WRAP_BYTES

    def test_the_recipient_recovers_the_seed(self):
        from nacl.public import SealedBox

        recipient = PrivateKey.generate()
        seed = new_seed()
        wrap = seal_seed_to(recipient.public_key.encode().hex(), seed)
        assert SealedBox(recipient).decrypt(wrap) == seed

    def test_two_seals_of_one_seed_differ(self):
        """Anonymous sealed boxes are randomized: the wrap is never a key fingerprint."""
        recipient = PrivateKey.generate().public_key.encode().hex()
        seed = new_seed()
        assert seal_seed_to(recipient, seed) != seal_seed_to(recipient, seed)

    def test_a_seed_of_the_wrong_size_is_refused(self):
        recipient = PrivateKey.generate().public_key.encode().hex()
        with pytest.raises(ValueError):
            seal_seed_to(recipient, b"\x00" * 16)


class TestTheSealedBytesAreASeed:
    """The one change nothing structural can catch.

    Container v1 is byte-identical under this rule — the wrap is still 80 bytes
    and every vector above still passes — so a client that went on sealing the
    DEK directly would produce perfect containers and jobs no provider could
    decrypt. These tests are the only thing standing between that defect and a
    green suite, which is why they pin the derivation as **arithmetic** and not
    as a round trip through `derive_dek`: a round trip through one implementation
    passes under any KDF at all, including one that ignores the owner entirely.
    """

    #: The three inputs, transcribed from Plan 3's Task 4 report rather than from
    #: any plan document, and recomputed here from `hmac`/`hashlib` directly.
    def _hkdf(self, ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
        prk = hmac.new(salt, ikm, hashlib.sha256).digest()
        out, block, counter = b"", b"", 1
        while len(out) < length:
            block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
            out += block
            counter += 1
        return out[:length]

    def test_the_seed_is_thirty_two_bytes(self):
        assert SEED_LEN == 32 == len(new_seed())

    def test_the_info_prefix_is_utf8_vorq_dek_unpadded(self):
        """Stated as bytes, because the coordinator states it as bytes.

        `Buffer.from("vorq-dek", "utf8")` is 8 bytes. A Python side that padded it
        to a 32-byte block, or encoded it UTF-16, would derive a different key and
        fail only at runtime, in a provider, on a paid job.

        There is no `-v1` on it: the container's version byte is the one version
        namespace, and two namespaces declaring the same fact is what C3 removed.
        """
        assert DEK_INFO_PREFIX == b"vorq-dek"
        assert len(DEK_INFO_PREFIX) == 8
        # The vectors carry the label as text, not hex, so this is the encoding
        # step itself — the one the UTF-16 mistake above would fail.
        assert DEK_INFO_PREFIX == KDF["info_prefix"].encode("utf-8")

    def test_the_salt_is_zero_length_and_not_thirty_two_zero_bytes(self):
        """RFC 5869 2.2 substitutes HashLen zeros for an absent salt.

        That substitution happens inside HMAC's own key padding, and it is not
        the same operation as passing 32 zero bytes as the *ikm* — a mistake that
        produces a well-formed key that decrypts nothing.
        """
        assert DEK_SALT == b""
        assert len(DEK_SALT) == 0
        assert DEK_SALT == _b(KDF["salt"])

    def test_the_derivation_matches_hkdf_sha256_input_for_input(self):
        seed = bytes(range(32))
        owner_raw = bytes.fromhex(OWNER[2:])
        expected = self._hkdf(
            ikm=seed, salt=b"", info=b"vorq-dek" + owner_raw, length=32
        )
        assert derive_dek(seed, OWNER) == expected

    def test_the_derivation_is_pinned_to_the_vectors_kdf_block(self):
        """The value itself, produced by the OTHER implementation.

        Every test above rebuilds the expectation from the same inputs this
        module uses, so a co-ordinated edit to both would keep them green. This
        digest is `node:crypto.hkdfSync("sha256", seed, <empty>, info, 32)` — the
        exact call the coordinator's `/release` makes — and it is the only
        assertion here that a Python-only mistake cannot survive.

        It travels **inside `container-v1.json`**, in the same file as the layout
        it belongs to, rather than as a literal transcribed by hand into three
        test bodies. One value, one source, three readers: transcription is how
        the three implementations were going to end up pinning three
        independently-computed numbers and calling it agreement.

        It caught a real defect during Plan 3: an expand loop that omitted `info`
        from every block produced a well-formed 32 bytes and passed every
        round-trip test in this file.
        """
        assert derive_dek(_b(KDF["seed"]), KDF["owner"]) == _b(KDF["dek"])
        # And the block's inputs are this module's inputs, not a second set that
        # happens to sit next to them.
        assert KDF["info_prefix"].encode("utf-8") == DEK_INFO_PREFIX
        assert _b(KDF["salt"]) == DEK_SALT

    def test_the_owner_enters_as_twenty_raw_bytes_and_never_as_a_string(self):
        """Checksum casing is display-only; a KDF over the text form is not.

        The client holds a checksummed address and the coordinator reads a
        lowercase one off the chain. If either spelling reached the digest, the
        two sides would derive two different keys for one address.
        """
        assert derive_dek(bytes(range(32)), OWNER) == derive_dek(
            bytes(range(32)), OWNER.lower()
        )
        assert derive_dek(bytes(range(32)), OWNER) == derive_dek(
            bytes(range(32)), bytes.fromhex(OWNER[2:])
        )
        # And it is the address bytes, not the text: a derivation over utf8(owner)
        # would agree with neither.
        assert derive_dek(bytes(range(32)), OWNER) != self._hkdf(
            ikm=bytes(range(32)), salt=b"",
            info=b"vorq-dek" + OWNER.encode(), length=32,
        )

    def test_a_different_owner_derives_a_different_key(self):
        """The whole of the wrap-lifting defence, in one assertion.

        An attacker lifts this wrap verbatim, mints a fresh commitment around it
        and posts their own job. Every field of that job is honest, so nothing
        over public data refuses it — the derivation does, because the escrow
        derives under the owner it reads from chain.
        """
        seed = new_seed()
        attacker = "0x000000000000000000000000000000000000dEaD"
        assert derive_dek(seed, OWNER) != derive_dek(seed, attacker)

    def test_a_key_derived_under_the_wrong_owner_does_not_open_the_payload(self):
        """The objective, not the mechanism: the attacker's key fails to decrypt."""
        seed, ciphertext = _sealed(b'{"input":"the victim\'s prompt"}')
        attacker_key = derive_dek(seed, "0x000000000000000000000000000000000000dEaD")
        with pytest.raises(Exception):
            open_dek(ciphertext, attacker_key)
        assert open_dek(ciphertext, derive_dek(seed, OWNER)) == (
            b'{"input":"the victim\'s prompt"}'
        )

    def test_the_seed_is_not_the_key(self):
        """Sealing the DEK directly is the defect; here is the shape of it."""
        seed = new_seed()
        assert derive_dek(seed, OWNER) != seed

    def test_a_seed_of_the_wrong_width_is_refused(self):
        with pytest.raises(ValueError):
            derive_dek(b"\x00" * 31, OWNER)

    def test_an_owner_of_the_wrong_width_is_refused(self):
        with pytest.raises(ValueError):
            derive_dek(new_seed(), "0xdead")


class TestEnvelopeEncryption:
    def test_round_trip(self):
        seed, ct = _sealed(b"broad payload")
        assert ct != b"broad payload"
        assert open_dek(ct, derive_dek(seed, OWNER)) == b"broad payload"

    def test_each_call_mints_a_fresh_seed(self):
        assert new_seed() != new_seed()

    def test_the_wrong_dek_does_not_open_it(self):
        _, ct = _sealed(b"secret")
        other, _ = _sealed(b"secret")
        with pytest.raises(Exception):
            open_dek(ct, derive_dek(other, OWNER))

    def test_the_nonce_is_prefixed_so_the_ciphertext_is_self_describing(self):
        _, ct = _sealed(b"")
        assert len(ct) == SecretBox.NONCE_SIZE + SecretBox.MACBYTES

    def test_a_dek_of_the_wrong_width_is_refused(self):
        with pytest.raises(ValueError):
            encrypt_under_dek(b"x", b"\x00" * 16)


class TestEndToEndAssembly:
    """The whole client-side path, checked against itself the way the node checks it."""

    def test_a_freshly_built_container_passes_its_own_commitment(self):
        recipient = PrivateKey.generate()
        seed, ciphertext = _sealed(b'{"input":"hello"}')
        wrap = seal_seed_to(recipient.public_key.encode().hex(), seed)
        container = build_container(wrap, ciphertext)
        c = commitment(wrap, keccak(ciphertext))
        assert_commitment(container, c)
        assert commitment_of(container) == c

    def test_one_flipped_ciphertext_byte_breaks_the_commitment(self):
        recipient = PrivateKey.generate()
        seed, ciphertext = _sealed(b'{"input":"hello"}')
        wrap = seal_seed_to(recipient.public_key.encode().hex(), seed)
        c = commitment(wrap, keccak(ciphertext))
        tampered = bytearray(build_container(wrap, ciphertext))
        tampered[-1] ^= 0x01
        with pytest.raises(ContainerError) as exc:
            assert_commitment(bytes(tampered), c)
        assert exc.value.fault == "commitment_mismatch"

    def test_the_recipient_is_the_only_party_that_can_read_it(self):
        from nacl.public import SealedBox

        recipient = PrivateKey.generate()
        stranger = PrivateKey.generate()
        seed, ciphertext = _sealed(b"plaintext")
        wrap = seal_seed_to(recipient.public_key.encode().hex(), seed)
        container = build_container(wrap, ciphertext)
        got_wrap, got_ct = split_container(container)
        # The recipient unseals a SEED and derives the key from it with the
        # job's owner: unsealing alone is not enough to read the payload.
        recovered_seed = SealedBox(recipient).decrypt(got_wrap)
        assert open_dek(got_ct, derive_dek(recovered_seed, OWNER)) == b"plaintext"
        with pytest.raises(Exception):
            SealedBox(stranger).decrypt(got_wrap)

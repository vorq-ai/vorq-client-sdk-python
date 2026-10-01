"""Real cryptography: EIP-712 wallet signing and NaCl sealed-box encryption."""

from __future__ import annotations

import base64
import copy
import json
import os

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from nacl.encoding import HexEncoder
from nacl.public import PrivateKey, SealedBox

from eth_utils import keccak

from eth_utils import to_bytes

from vorq import Client
from vorq._container import commitment_of, open_dek, split_container
from vorq._container import derive_dek, encrypt_under_dek, new_seed, seal_seed_to
from vorq._crypto import (
    ORDER_TYPES,
    SESSION_TYPES,
    Cipher,
    SealedBoxCipher,
    Signer,
    WalletSigner,
    content_job_id,
    derive_result_cipher,
    seal_to,
    vorq_domain,
)
from vorq._money import format_usd, parse_usd
from vorq._terms import ChainContext, OrderTerms, TokenDomain, order_domain

#: A chain context for the tests that need one. The vector-driven digest checks
#: live in `tests/test_terms.py`, against the deployment `signing-v3.json` names;
#: this one only has to be a context, so its addresses are distinct sentinels —
#: distinct because two contracts sharing an address is the mix-up the type
#: exists to prevent.
CTX = ChainContext(
    chain_id=84532,
    job_registry="0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
    provider_registry="0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
    ask_registry="0x00000000000000000000000000000000000A5c00",
    usdc="0x5FbDB2315678afecb367f032d93F642f64180aa3",
    decimals=6,
    token_domain=TokenDomain("USDC", "2"),
)

# A 32-byte container commitment, of the shape `vorq._container.commitment`
# produces. The signer reads `c` off the terms and never recomputes it, so these
# tests hand it one directly.
C_ONE = keccak(b"container-one")
C_TWO = keccak(b"container-two")

#: The secp256k1 group order. A canonical signature carries `s <= N // 2`; the
#: twin at `N - s` recovers the same address and is what "malleable" means here.
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _terms(c: bytes = C_ONE, **over) -> OrderTerms:
    """A complete set of order terms, overridable member by member."""
    base = dict(
        c=c, model_id=1, sla_secs=3600, rate_in=30000, rate_out=90000,
        units_in=1000, units_out=2000, designated=1, expires_at=1800003600,
    )
    base.update(over)
    return OrderTerms(**base)  # type: ignore[arg-type]


def _public_hex(private_hex: str) -> str:
    return PrivateKey(private_hex.encode(), encoder=HexEncoder).public_key.encode(HexEncoder).decode()


# Only private keys live in .env.test; addresses and public keys derive from them.
WALLET_KEY = os.environ["VORQ_WALLET_KEY"]
WALLET_ADDRESS = Account.from_key(WALLET_KEY).address
CIPHER_KEY = os.environ["VORQ_CIPHER_KEY"]
CIPHER_PUBLIC = _public_hex(CIPHER_KEY)
RECIPIENT_KEY = os.environ["VORQ_RECIPIENT_KEY"]
RECIPIENT_PUBLIC = _public_hex(RECIPIENT_KEY)

def _offline_client(signer: WalletSigner, cipher: Cipher | None = None) -> Client:
    """A network-less client, used only for its body builders."""
    return Client.from_session_token(
        "vorq_test", signer=signer, cipher=cipher,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )


def _minimal_sealed_body(signer: WalletSigner, *, box_key: str = RECIPIENT_PUBLIC,
                         cipher: Cipher | None = None) -> dict:
    """One complete designated submission, built through the client's own builders.

    The 402 loop lives in `tests/test_client.py`; this assembles the same flat
    body the client assembles — a batch line's shape, container base64; the
    single-job form carries the same fields with the bytes raw — so the envelope
    and commitment assertions below stay over real client output rather than a
    hand-rolled imitation.
    """
    client = _offline_client(signer, cipher)
    container, c = client._seal_container(box_key, {"input": "hi"}, signer.address)
    terms = _terms(c=c, designated=7)
    return {
        **terms.to_wire(
            owner=signer.address,
            job_id=content_job_id(signer.address, c),
            signature=signer.sign_order_v2(terms, CTX),
            decimals=CTX.decimals,
        ),
        "container": base64.b64encode(container).decode(),
        "auth_sig": signer.sign_payment_authorization(
            amount=210, job_id=content_job_id(signer.address, c),
            expires_at=terms.expires_at, ctx=CTX,
        ),
        "amount": format_usd(210, CTX.decimals),
    }


def _order_message(body: dict) -> dict:
    """The typed `Order` message a verifier reconstructs from a submission body."""
    v = body
    return {
        "c": to_bytes(hexstr=v["c"]),
        "modelId": v["model_id"],
        "slaSecs": v["sla_secs"],
        "rateIn": parse_usd(v["rate_in"], CTX.decimals),
        "rateOut": parse_usd(v["rate_out"], CTX.decimals),
        "unitsIn": v["units_in"],
        "unitsOut": v["units_out"],
        "designated": v["designated"],
        "expiresAt": v["expires_at"],
    }


class TestWalletSigner:
    def test_derives_checksummed_address_from_key(self):
        signer = WalletSigner(WALLET_KEY)
        assert signer.address == WALLET_ADDRESS

    def test_reads_key_from_env(self):
        signer = WalletSigner()  # falls back to VORQ_WALLET_KEY
        assert signer.address == WALLET_ADDRESS

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("VORQ_WALLET_KEY", raising=False)
        with pytest.raises(ValueError):
            WalletSigner()

    def test_satisfies_signer_protocol(self):
        assert isinstance(WalletSigner(WALLET_KEY), Signer)

    def test_sign_order_v2_recovers_to_signer_address(self):
        signer = WalletSigner(WALLET_KEY)
        terms = _terms()
        sig = signer.sign_order_v2(terms, CTX)
        assert isinstance(sig, str) and sig.startswith("0x")
        signable = encode_typed_data(order_domain(CTX), ORDER_TYPES, terms.message())
        assert Account.recover_message(signable, signature=sig) == WALLET_ADDRESS

    def test_designated_is_a_uint32_id_and_zero_means_open(self):
        """Q22: an open order designates 0, and 0 is a value rather than an absence.

        A different pin is a different order, and the open case is signable —
        which is what stops "any provider" being spelled as a null the type has
        no room for.
        """
        signer = WalletSigner(WALLET_KEY)
        assert signer.sign_order_v2(_terms(designated=7), CTX) != (
            signer.sign_order_v2(_terms(designated=0), CTX)
        )
        assert signer.sign_order_v2(_terms(designated=0), CTX).startswith("0x")

    def test_sign_order_binds_the_commitment(self):
        # There is no `enc` term: container v1 carries its version in byte 0 and
        # that byte is inside `c`, so the scheme is bound by the commitment
        # itself. Two orders identical but for `c` are two orders.
        signer = WalletSigner(WALLET_KEY)
        assert signer.sign_order_v2(_terms(c=C_ONE), CTX) != (
            signer.sign_order_v2(_terms(c=C_TWO), CTX)
        )

    def test_sign_order_is_deterministic(self):
        signer = WalletSigner(WALLET_KEY)
        assert signer.sign_order_v2(_terms(), CTX) == signer.sign_order_v2(_terms(), CTX)

    def test_the_verifying_contract_binds_the_signature(self):
        """The Q6 failure, and it is silent: a wrong address is not an error."""
        signer = WalletSigner(WALLET_KEY)
        other = ChainContext(**{**CTX.__dict__, "job_registry": CTX.provider_registry})
        assert signer.sign_order_v2(_terms(), CTX) != signer.sign_order_v2(_terms(), other)

    def test_sign_nonce_recovers_to_signer_address(self):
        signer = WalletSigner(WALLET_KEY)
        nonce = "d4f81c2e9a774b0c8f21"
        sig = signer.sign_nonce(nonce, CTX.chain_id)
        message = {"address": WALLET_ADDRESS, "nonce": nonce}
        signable = encode_typed_data(vorq_domain(CTX.chain_id), SESSION_TYPES, message)
        assert Account.recover_message(signable, signature=sig) == WALLET_ADDRESS

    def test_the_session_domain_is_bound_to_the_chain_it_is_asked_for(self):
        """The coordinator announces its chain on ``GET /auth/nonce`` and verifies
        under it; a wallet on another chain signs a different digest and recovers
        a stranger, which is the whole of the separation."""
        assert vorq_domain(CTX.chain_id)["chainId"] == CTX.chain_id
        assert vorq_domain(1) != vorq_domain(CTX.chain_id)
        assert "verifyingContract" not in vorq_domain(CTX.chain_id)

    def test_the_order_domain_is_not_the_session_domain(self):
        """A captured login must not be an order, and an order must not be a login.

        Different name/version pairs *and* a verifyingContract on one side only,
        so neither digest can ever be the other's.
        """
        assert "verifyingContract" in order_domain(CTX)
        assert "verifyingContract" not in vorq_domain(CTX.chain_id)
        assert order_domain(CTX)["version"] != vorq_domain(CTX.chain_id)["version"]

    def test_every_signature_is_canonical(self):
        """Low-`s`, and `v` in {27, 28}, on all four artifacts this signer makes.

        **The chain does not enforce this and never will.** `JobRegistry._recover`,
        `ProviderRegistry._recover` and `AskRegistry._recover` call bare
        `ecrecover` with no canonical-`s` bound, so the high-`s` twin
        `(r, N - s, 55 - v)` of any signature below recovers the same address and
        is accepted just as readily. That is signature malleability, it is
        deliberate, and it is harmless *here* because nothing on this network
        keys on signature bytes: replay is stopped by the job state machine and
        by the monotonic `issuedAt` floors.

        What it means for a signer is one rule, and it is why this test exists
        rather than a comment: **a signature is never an identifier.** Not a
        cache key, not a dedupe key, not an idempotency key — two byte strings
        that are not equal can carry exactly the same authority. `eth-account`
        emits low-`s` already; this keeps that a property of the SDK rather than
        of whichever version of a dependency happens to be installed.
        """
        signer = WalletSigner(WALLET_KEY)
        terms = _terms()
        job_id = "0x" + "ab" * 32
        signatures = {
            "Order": signer.sign_order_v2(terms, CTX),
            "Cancel": signer.sign_cancel(job_id, 1_700_000_000, CTX),
            "payment authorization": signer.sign_payment_authorization(
                amount=210, job_id=job_id, expires_at=terms.expires_at, ctx=CTX
            ),
            "session nonce": signer.sign_nonce("n1", CTX.chain_id),
        }

        for artifact, sig in signatures.items():
            raw = to_bytes(hexstr=sig)
            assert len(raw) == 65, artifact
            s = int.from_bytes(raw[32:64], "big")
            v = raw[64]
            assert 0 < s <= SECP256K1_N // 2, f"{artifact} carries a high-s signature"
            assert v in (27, 28), f"{artifact} carries v={v}, which recovers address(0)"

    def test_the_high_s_twin_recovers_the_same_address(self):
        """The property the rule above exists for, demonstrated once.

        Flipping `s` to `N - s` and `v` to the other parity produces different
        bytes over the same digest that recover the same signer. Any contract
        using bare `ecrecover` accepts both, so any code that treated the bytes
        as the identity of the authorization would be treating one authorization
        as two.
        """
        signer = WalletSigner(WALLET_KEY)
        terms = _terms()
        sig = to_bytes(hexstr=signer.sign_order_v2(terms, CTX))
        twin = (
            sig[0:32]
            + (SECP256K1_N - int.from_bytes(sig[32:64], "big")).to_bytes(32, "big")
            # 27 <-> 28. Not `v ^ 1`: that is the {0, 1} recovery-id convention,
            # and it takes 27 to 26, which `eth-account` refuses outright.
            + bytes([55 - sig[64]])
        )

        assert twin != sig
        signable = encode_typed_data(order_domain(CTX), ORDER_TYPES, terms.message())
        assert Account.recover_message(signable, signature=twin) == WALLET_ADDRESS

    def test_generate_makes_a_usable_signer(self):
        signer = WalletSigner.generate()
        assert signer.address.startswith("0x")
        sig = signer.sign_nonce("abc", CTX.chain_id)
        assert sig.startswith("0x")


class TestSealedBoxCipher:
    def test_self_roundtrip_with_own_keypair(self):
        cipher = SealedBoxCipher(CIPHER_KEY, recipient_public_key=CIPHER_PUBLIC)
        blob = cipher.encrypt(b"secret payload")
        assert blob != b"secret payload"
        assert cipher.decrypt(blob) == b"secret payload"

    def test_encrypt_to_counterparty_only_they_can_open(self):
        sender = SealedBoxCipher(CIPHER_KEY, recipient_public_key=RECIPIENT_PUBLIC)
        blob = sender.encrypt(b"for the recipient")
        recipient = SealedBoxCipher(RECIPIENT_KEY)
        assert recipient.decrypt(blob) == b"for the recipient"

    def test_public_key_is_exposed_as_hex(self):
        cipher = SealedBoxCipher(CIPHER_KEY)
        assert cipher.public_key == CIPHER_PUBLIC

    def test_encrypt_without_recipient_raises(self):
        cipher = SealedBoxCipher(CIPHER_KEY)  # no recipient set
        with pytest.raises(ValueError):
            cipher.encrypt(b"nowhere to send")

    def test_decrypt_wrong_recipient_fails(self):
        sender = SealedBoxCipher(CIPHER_KEY, recipient_public_key=RECIPIENT_PUBLIC)
        blob = sender.encrypt(b"not for you")
        wrong = SealedBoxCipher(CIPHER_KEY)  # different private key than recipient
        with pytest.raises(Exception):
            wrong.decrypt(blob)

    def test_reads_key_from_env(self):
        cipher = SealedBoxCipher()  # falls back to VORQ_CIPHER_KEY
        assert cipher.public_key == CIPHER_PUBLIC

    def test_satisfies_cipher_protocol(self):
        assert isinstance(SealedBoxCipher(CIPHER_KEY), Cipher)

    def test_generate_makes_a_usable_cipher(self):
        a = SealedBoxCipher.generate()
        b = SealedBoxCipher.generate()
        blob = SealedBoxCipher(a._private_hex, recipient_public_key=b.public_key).encrypt(b"hi")
        assert b.decrypt(blob) == b"hi"


class TestDerivedResultCipher:
    """The result cipher derives from the wallet (whitepaper:27, spec D11) — a
    deterministic (RFC 6979) signature over a fixed message, so the same wallet
    re-derives the same X25519 key after a restart with nothing persisted."""

    def test_derive_result_cipher_is_deterministic_per_wallet(self):
        s = WalletSigner.generate()
        a, b = derive_result_cipher(s), derive_result_cipher(s)
        assert a.public_key == b.public_key            # re-derivable after restart (D11)
        assert derive_result_cipher(WalletSigner.generate()).public_key != a.public_key

    def test_derivation_survives_a_fresh_signer_over_the_same_key(self):
        key = Account.create().key.hex()
        assert (
            derive_result_cipher(WalletSigner(key)).public_key
            == derive_result_cipher(WalletSigner(key)).public_key
        )

    def test_from_seed_is_a_usable_keypair(self):
        cipher = SealedBoxCipher.from_seed(keccak(b"seed"))
        blob = seal_to(cipher.public_key, b"hello")
        assert cipher.decrypt(blob) == b"hello"


class TestRecipientKeySpelling:
    """A recipient key arrives in both spellings, and both must seal.

    The coordinator serves `GET /evm/providers/{id}`'s `box_key` as `0x` hex —
    every `bytes` column on that wire is — while `GET /key` builds its escrow key
    by hand and serves it bare. nacl's `HexEncoder` strips no prefix, so a client
    that passed the string through died with `binascii.Error` **before posting a
    byte**, and no designated order could be submitted at all. Found end to end;
    pinned here, where the SDK owns it.
    """

    def test_a_0x_prefixed_recipient_seals_to_the_same_key(self):
        cipher = SealedBoxCipher.from_seed(keccak(b"designated recipient"))
        blob = seal_to("0x" + cipher.public_key, b"designated payload")
        assert cipher.decrypt(blob) == b"designated payload"

    def test_case_is_not_part_of_a_key(self):
        cipher = SealedBoxCipher.from_seed(keccak(b"upper"))
        assert cipher.decrypt(seal_to("0X" + cipher.public_key.upper(), b"x")) == b"x"

    @pytest.mark.parametrize("bad", [
        "zz" * 32,                       # not hex
        "ab" * 16,                       # 16 bytes, not 32
        "0x" + "ab" * 33,                # 33 bytes
        " " + "ab" * 32,                 # bytes.fromhex tolerates space; a key must not
        "0x",
        "",
    ])
    def test_a_key_that_is_not_one_raises_rather_than_binascii(self, bad):
        with pytest.raises(ValueError, match="32-byte Curve25519"):
            seal_to(bad, b"payload")

    def test_the_cipher_constructor_takes_the_same_two_spellings(self):
        """The sibling door. Both seal to a recipient; both must agree on hex."""
        recipient = SealedBoxCipher.from_seed(keccak(b"cipher recipient"))
        sender = SealedBoxCipher(
            PrivateKey.generate().encode(HexEncoder).decode(),
            recipient_public_key="0x" + recipient.public_key,
        )
        assert recipient.decrypt(sender.encrypt(b"via the cipher")) == b"via the cipher"


class TestSealedOrderBody:
    def test_order_signature_covers_the_commitment(self):
        s = WalletSigner.generate()
        body = _minimal_sealed_body(s)                 # build via the same helpers the client uses
        sig = body["signature"]
        moved = copy.deepcopy(_order_message(body))
        moved["c"] = keccak(b"some other container")
        assert s.sign_order_v2(_terms(c=moved["c"], designated=7), CTX) != sig

    def test_order_signature_recovers_over_the_struct(self):
        s = WalletSigner.generate()
        body = _minimal_sealed_body(s)
        signable = encode_typed_data(order_domain(CTX), ORDER_TYPES, _order_message(body))
        assert Account.recover_message(signable, signature=body["signature"]) == s.address

    def test_the_body_carries_a_container_and_no_input_object(self):
        """Asserted on **parsed keys**, never on a substring of the serialized body.

        A container is base64, and base64 of random bytes contains most short
        strings often enough to fail a run for a reason nobody can reproduce.
        """
        body = _minimal_sealed_body(WalletSigner.generate())
        assert "input" not in body
        assert isinstance(body["container"], str)
        # Flat: the order's fields, the container and the payment side by side.
        assert set(body) == {
            "c", "owner", "job_id", "model_id", "sla_secs", "rate_in", "rate_out",
            "units_in", "units_out", "designated", "expires_at", "signature",
            "container", "auth_sig", "amount",
        }
        keys = set(body)
        assert not [k for k in keys if "dek" in k.lower() or "cid" in k.lower()]
        assert not [k for k in keys if k in ("input", "enc", "ciphertext")]

    def test_the_container_reproduces_the_signed_commitment(self):
        body = _minimal_sealed_body(WalletSigner.generate())
        container = base64.b64decode(body["container"])
        assert commitment_of(container) == to_bytes(hexstr=body["c"])

    def test_sealed_envelope_carries_owner_and_result_key(self):
        box_sk = PrivateKey.generate()
        box_pk = box_sk.public_key.encode(HexEncoder).decode()
        signer = WalletSigner.generate()
        body = _minimal_sealed_body(signer, box_key=box_pk)
        wrap, ciphertext = split_container(base64.b64decode(body["container"]))

        # The wrap holds a SEED, not the DEK (Q3): unseal it, derive with the
        # order's owner, then open the bulk. Treating the unsealed bytes as the
        # key is the defect that produces jobs nobody can decrypt.
        seed = SealedBox(box_sk).decrypt(wrap)
        assert seed != derive_dek(seed, signer.address)
        env = json.loads(open_dek(ciphertext, derive_dek(seed, signer.address)))
        assert env["v"] == "vorq-env-v1"
        assert env["owner"].lower() == signer.address.lower()
        assert env["result_key"] == derive_result_cipher(signer).public_key
        assert env["input"] == {"input": "hi"}
        assert "result_key" not in body

    def test_wire_vorq_object_carries_owner_and_no_enc(self):
        # The node's `parseOrder` reads `vorq.owner` and checks `job_id` against
        # `keccak(owner ‖ c)`; there is no `enc` any more, because container v1
        # carries its version in byte 0 and that byte is inside `c`.
        signer = WalletSigner.generate()
        body = _minimal_sealed_body(signer)
        assert body["owner"] == signer.address
        assert "enc" not in body

    def test_the_owner_in_the_envelope_is_the_owner_the_dek_derives_under(self):
        """The two must agree or the payload is undecryptable by design."""
        box_sk = PrivateKey.generate()
        signer = WalletSigner.generate()
        body = _minimal_sealed_body(
            signer, box_key=box_sk.public_key.encode(HexEncoder).decode()
        )
        wrap, ciphertext = split_container(base64.b64decode(body["container"]))
        seed = SealedBox(box_sk).decrypt(wrap)
        env = json.loads(open_dek(ciphertext, derive_dek(seed, body["owner"])))
        assert env["owner"] == body["owner"]

    def test_job_id_is_keccak_of_owner_and_the_commitment(self):
        signer = WalletSigner.generate()
        body = _minimal_sealed_body(signer)
        assert body["job_id"] == content_job_id(signer.address, body["c"])


class TestContentJobId:
    def test_is_0x_bytes32(self):
        jid = content_job_id(WALLET_ADDRESS, C_ONE)
        assert jid.startswith("0x") and len(jid) == 66  # 0x + 32 bytes hex

    def test_binds_owner_and_commitment(self):
        base = content_job_id(WALLET_ADDRESS, C_ONE)
        assert content_job_id(WALLET_ADDRESS, C_TWO) != base  # commitment-sensitive
        other = Account.create().address
        assert content_job_id(other, C_ONE) != base  # owner-sensitive

    def test_is_deterministic(self):
        assert content_job_id(WALLET_ADDRESS, C_ONE) == content_job_id(WALLET_ADDRESS, C_ONE)

    def test_takes_the_commitment_and_never_hashes_it_again(self):
        """`keccak(owner ‖ c)`, with no inner hash — the vectors say so too."""
        expected = keccak(to_bytes(hexstr=WALLET_ADDRESS) + C_ONE)
        assert content_job_id(WALLET_ADDRESS, C_ONE) == "0x" + expected.hex()

    def test_a_commitment_of_the_wrong_width_is_refused(self):
        with pytest.raises(ValueError):
            content_job_id(WALLET_ADDRESS, b"too short")

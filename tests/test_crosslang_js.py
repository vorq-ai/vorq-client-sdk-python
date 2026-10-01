"""Open a container the JavaScript SDK built — the other half of the round trip.

`tests/vectors/container-v1.json` pins the wrap as an **opaque blob** and asserts
nothing about its plaintext, so it does not prove the seed rule and does not
prove either side's sealed box is libsodium's rather than merely self-consistent.
A round trip through this repo's own code proves agreement with itself and
nothing else. This file proves the rest: the fixture
(`tests/fixtures/crosslang-js.json`, emitted by
`vorq-client-sdk-js/scripts/gen-crosslang.mjs`) carries the recipient's
**private** key, so the box has to actually open — and it is opened with raw
PyNaCl rather than through this SDK, so what it says is "that is libsodium",
not "that is what we do".

**This file guards this repo's reader, and the JS repo's writer only as of the
last regeneration.** The reading path — ``split_container``, ``derive_dek``,
``open_dek``, ``commitment`` — runs here on every suite. The JS side's *writer* —
its sealed box, ``sealSeedTo``, ``encryptUnderDek`` — ran once, when the fixture
was generated, and does not run again. So if a future edit over there made
``sealSeedTo`` seal the DEK instead of the seed, this file would keep passing
against the committed bytes indefinitely. **The standing guard against the seed
trap on the writing side is the JS repo's own seed-rule test —
`vorq-client-sdk-js/test/container.test.ts`, ``describe("the seed rule")`` — and
not this one.** The mirror of this paragraph is in
`vorq-client-sdk-js/test/crosslang.test.ts`.

**Regenerating the fixture is not reviewable by diff.** The wrap's ephemeral key
and the bulk cipher's nonce are fresh per run, so ``container``, ``c``, ``job_id``
and the wrap all change wholesale: "the seed rule broke over there" and "somebody
re-ran the script" produce visually identical diffs. Running this test against the
new bytes is the only review there is, so it is a required step and not a courtesy::

    cd ../vorq-client-sdk-js && npm run build
    node scripts/gen-crosslang.mjs > ../vorq-client-sdk-python/tests/fixtures/crosslang-js.json
    cd ../vorq-client-sdk-python && .venv/bin/python -m pytest tests/test_crosslang_js.py -v

The seed rule is checked **first** below, and in its own test function, for the
same reason: a cross-language keccak disagreement must not consume the run before
the one property with almost no structural backstop has been evaluated.
"""

from __future__ import annotations

import json
from pathlib import Path

from eth_utils import keccak
from nacl.public import PrivateKey, SealedBox

from vorq._container import (
    CONTAINER_VERSION,
    SEED_WRAP_BYTES,
    commitment,
    derive_dek,
    open_dek,
    split_container,
)
from vorq._crypto import content_job_id

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "crosslang-js.json").read_text())


def _bytes(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x"))


def _split() -> tuple[bytes, bytes]:
    return split_container(_bytes(FIXTURE["container"]))


def _unseal() -> bytes:
    seed_wrap, _ = _split()
    return SealedBox(PrivateKey(_bytes(FIXTURE["recipient_secret_key"]))).decrypt(seed_wrap)


def test_the_wrap_unseals_to_the_seed_and_not_to_the_dek() -> None:
    """The one property with almost no structural backstop, checked first.

    Opening proves the JS sealed box is libsodium's; the inequality proves it
    sealed the seed and not the key. A client that sealed the DEK produces
    byte-perfect containers and jobs that no provider can decrypt.
    """
    # NOT `len(seed_wrap) == SEED_WRAP_BYTES`: `split_container` slices a fixed
    # width behind a `MIN_CONTAINER_BYTES` guard, so it can never hand back
    # another one and that assertion could not fail. What *can* fail is the
    # fixture's own framing, so that is what is read — off the raw bytes, before
    # the split, so the split's own guards are not what is being tested.
    container = _bytes(FIXTURE["container"])
    assert container[0] == CONTAINER_VERSION
    assert len(container) > 1 + SEED_WRAP_BYTES  # a full-width wrap and a payload

    seed_wrap, _ = _split()
    seed = _unseal()
    assert seed.hex() == FIXTURE["seed"]
    assert seed.hex() != FIXTURE["dek"]


def test_the_dek_re_derives_and_the_payload_reads() -> None:
    _, ciphertext = _split()
    dek = derive_dek(_unseal(), FIXTURE["owner"])
    assert dek.hex() == FIXTURE["dek"]
    assert open_dek(ciphertext, dek).decode() == FIXTURE["plaintext"]


def test_the_commitment_and_job_id_reproduce() -> None:
    seed_wrap, ciphertext = _split()
    assert "0x" + commitment(seed_wrap, keccak(ciphertext)).hex() == FIXTURE["c"]
    assert content_job_id(FIXTURE["owner"], _bytes(FIXTURE["c"])) == FIXTURE["job_id"]

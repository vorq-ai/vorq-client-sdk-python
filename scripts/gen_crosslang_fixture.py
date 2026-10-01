#!/usr/bin/env python
"""Emit a container this repo built, for the JavaScript suite to open.

`tests/vectors/container-v1.json` pins the wrap as an **opaque blob** and asserts
nothing about its plaintext, so it cannot prove the seed rule and cannot prove
that another repo's ``SealedBox`` is libsodium's rather than merely
self-consistent. This fixture can: it carries the recipient's **private** key, so
the other side has to actually open the box.

The inputs are fixed so that a failure names a byte rather than a run. The
*output* is not reproducible and is not meant to be — a sealed box mints a fresh
ephemeral key and ``SecretBox`` a fresh nonce — so the emitted file is the
authority, and regenerating it is a real diff. Nothing outside it pins these
bytes: this is not a vector file, and it is not the coordinator's.

Regenerate with::

    PYTHONPATH=. .venv/bin/python scripts/gen_crosslang_fixture.py \
        > ../vorq-client-sdk-js/test/fixtures/crosslang.json

``PYTHONPATH=.`` because the package is imported from the tree — the same thing
pytest does when it prepends the rootdir.
"""

from __future__ import annotations

import json

from eth_utils import keccak
from nacl.encoding import HexEncoder
from nacl.public import PrivateKey

from vorq._container import (
    build_container,
    commitment,
    derive_dek,
    encrypt_under_dek,
    seal_seed_to,
)
from vorq._crypto import content_job_id

RECIPIENT_SECRET = bytes([0x11] * 32)
SEED = bytes(range(32))
OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
PLAINTEXT = b'{"v":"vorq-env-v1","input":"cross-language round trip"}'


def main() -> None:
    recipient = PrivateKey(RECIPIENT_SECRET)
    dek = derive_dek(SEED, OWNER)
    ciphertext = encrypt_under_dek(PLAINTEXT, dek)
    wrap = seal_seed_to(recipient.public_key.encode(HexEncoder).decode(), SEED)
    container = build_container(wrap, ciphertext)
    c = commitment(wrap, keccak(ciphertext))

    print(
        json.dumps(
            {
                "generated_by": "vorq-client-sdk-python/scripts/gen_crosslang_fixture.py",
                "why": (
                    "the vectors pin the wrap as an opaque blob; this fixture carries the "
                    "recipient's private key, so opening it proves the sealed box is "
                    "libsodium's and that the sealed 32 bytes are the seed"
                ),
                "recipient_secret_key": RECIPIENT_SECRET.hex(),
                "recipient_public_key": recipient.public_key.encode(HexEncoder).decode(),
                "owner": OWNER,
                "seed": SEED.hex(),
                "dek": dek.hex(),
                "plaintext": PLAINTEXT.decode(),
                "container": "0x" + container.hex(),
                "c": "0x" + c.hex(),
                "job_id": content_job_id(OWNER, c),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

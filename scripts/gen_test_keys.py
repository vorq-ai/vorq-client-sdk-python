#!/usr/bin/env python
"""Print a fresh set of throwaway private keys for `.env.test`.

    python scripts/gen_test_keys.py > .env.test

Generates one secp256k1 wallet (Ethereum signing) and two Curve25519 keys (our
sealed-box identity + a stand-in counterparty). Only private keys are printed —
addresses and public keys derive from them. Tests only; never point real funds
at a key printed here.
"""

from __future__ import annotations

from eth_account import Account
from nacl.encoding import HexEncoder
from nacl.public import PrivateKey


def _priv_hex(key: PrivateKey) -> str:
    return key.encode(HexEncoder).decode()


def main() -> None:
    wallet_key = Account.create().key.hex()
    if not wallet_key.startswith("0x"):
        wallet_key = "0x" + wallet_key

    print("# Throwaway test keys — never real funds.")
    print(f"VORQ_WALLET_KEY={wallet_key}")
    print(f"VORQ_CIPHER_KEY={_priv_hex(PrivateKey.generate())}")
    print(f"VORQ_RECIPIENT_KEY={_priv_hex(PrivateKey.generate())}")


if __name__ == "__main__":
    main()

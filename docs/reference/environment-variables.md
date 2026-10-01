---
title: Environment variables
description: The environment variables the vorq package reads.
---

| Variable | Read by | Meaning |
| --- | --- | --- |
| `VORQ_WALLET_KEY` | `Client()`, `WalletSigner()`, `mint_session_token()`, `sealing_http_client()` | Your wallet's secp256k1 private key, `0x` hex. The root credential: it signs sessions, orders, payments and cancels, and derives the result key. |
| `VORQ_CIPHER_KEY` | `SealedBoxCipher()`, and a `Client` without `cipher=` whose signer isn't a `WalletSigner` | A Curve25519 private key (hex) to hold the result key explicitly instead of deriving it. |
| `VORQ_PIN_GATEWAY` | `Client` | The gateway result bytes are read from, when `gateway=` isn't passed. Defaults to `https://ipfs.filebase.io`. An empty string disables the built-in gateway. |
| `VORQ_SETTLEMENT_MARGIN` | `vorq`, at import | Seconds added to the SLA window when computing an order's expiry. Default `3600`. The expiry is capped at 24 hours from now. |

A client built on `VORQ_WALLET_KEY` ignores `VORQ_CIPHER_KEY` unless you pass
`cipher=vorq.SealedBoxCipher()`.

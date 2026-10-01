---
title: Manage keys and sessions
description: Use a custom signer, hold the result key explicitly, reuse session tokens and choose the result gateway.
---

By default, `vorq.Client()` needs one secret, `VORQ_WALLET_KEY`, and derives everything else
from it. This page covers the cases where you want something different.

## Keep the wallet key in a KMS or remote signer

Pass any object that implements the [`Signer`](../reference/signing.md#signer) protocol:

```python
client = vorq.Client(signer=my_kms_signer, cipher=vorq.SealedBoxCipher())
```

The signer is the root credential: it signs the session handshake, orders, payment
authorizations and cancels. A custom signer can't derive the result key, so also pass a
`cipher=`, or set `VORQ_CIPHER_KEY`, which such a client reads automatically. Without a cipher,
`submit` raises `ValidationError`.

## Hold the result key explicitly

A client built on `VORQ_WALLET_KEY` derives its result key from the wallet. To keep a separate
Curve25519 key instead, for example so a reader process can open results without the wallet:

```bash
# generate a 32-byte Curve25519 private key once, and keep it as VORQ_CIPHER_KEY
export VORQ_CIPHER_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

```python
client = vorq.Client(cipher=vorq.SealedBoxCipher())  # reads $VORQ_CIPHER_KEY
```

An explicit `cipher=` always wins over the derived key. Results sealed to one key open only with
that key: when you switch between the derived key and an explicit one, keep the old key until
its jobs have settled.

## Reuse a session token

- `vorq.mint_session_token()` runs the wallet handshake once, synchronously, and returns a
  `vorq_sess_…` token. Use it to call routes that carry no prompt, such as `GET /v1/models`,
  from your own HTTP code.
- `vorq.Client.from_session_token(token, ...)` builds a client on an existing token. Without a
  `signer=`, the token is used as-is and never rotated, and the client can read jobs but can't
  submit or cancel.

See [Persist and resume jobs](./persist-and-resume-jobs.md#read-results-in-a-process-without-the-wallet-key)
for a read-only process.

## Choose where result bytes are read from

A settled job names its result by content address. The client reads those bytes from a storage
gateway, not from the coordinator, in this order of precedence:

1. `vorq.Client(gateway="https://ipfs.filebase.io")`;
2. the `VORQ_PIN_GATEWAY` environment variable;
3. the built-in public gateway, `https://ipfs.filebase.io`.

An empty string at either of the first two levels disables the gateway, and reading a result
then raises `VorqError`. See [Reading results](../reference/client.md#reading-results).

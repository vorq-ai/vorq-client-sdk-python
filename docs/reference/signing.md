---
title: Signers and ciphers
description: Reference for the Signer and Cipher protocols, WalletSigner, SealedBoxCipher, ChainContext, the EIP-712 types and the container format.
---

Orders are signed and payloads sealed on your machine. `Signer` and `Cipher` are protocols; the
SDK ships `WalletSigner` (EIP-712) and `SealedBoxCipher` (libsodium sealed boxes). A default
`vorq.Client()` builds both from `VORQ_WALLET_KEY`.

## `Signer`

```python
class Signer(Protocol):
    address: str
    def sign_order_v2(self, terms: OrderTerms, ctx: ChainContext) -> str: ...
    def sign_cancel(self, job_id: str, issued_at: int, ctx: ChainContext) -> str: ...
    def sign_payment_authorization(
        self, *, amount: int, job_id: str, expires_at: int, ctx: ChainContext
    ) -> str: ...
    def sign_nonce(self, nonce: str, chain_id: int) -> str: ...
```

Implement it to keep the wallet key in a KMS, an HSM or a remote signer. Every signature
returns a `0x`-prefixed hex string. Use a **dedicated wallet funded with your inference budget,
never a main wallet's key**.

## `Cipher`

```python
class Cipher(Protocol):
    @property
    def public_key(self) -> str: ...
    def encrypt(self, data: bytes) -> bytes: ...
    def decrypt(self, data: bytes) -> bytes: ...
```

The client's cipher is the result key: its `public_key` goes into every sealed envelope, and
`decrypt` opens results.

## `WalletSigner`

```python
signer = vorq.WalletSigner()                 # key from $VORQ_WALLET_KEY
signer = vorq.WalletSigner(private_key)      # explicit 0x hex key
signer = vorq.WalletSigner.generate()        # fresh throwaway wallet
```

| Member | Meaning |
| --- | --- |
| `WalletSigner(private_key=None, *, key_env="VORQ_WALLET_KEY")` | Takes the secp256k1 key from the argument or the environment variable. Raises `ValueError` if neither is set. |
| `.address` | The checksummed wallet address. |
| `.sign_order_v2(terms, ctx)` | Signs `Order` on the job registry's domain. |
| `.sign_cancel(job_id, issued_at, ctx)` | Signs `Cancel` on the same domain. |
| `.sign_payment_authorization(amount=, job_id=, expires_at=, ctx=)` | Signs `ReceiveWithAuthorization` (EIP-3009) on the payment token's domain. |
| `.sign_nonce(nonce, chain_id)` | Signs the session handshake. |

All signatures are 65 bytes, with `v` in {27, 28} and low `s`.

A `Client` built on a `WalletSigner` without `cipher=` derives its result key from the wallet:
a Curve25519 key seeded by `keccak256` of the wallet's signature over the fixed message
`VORQ-ENC-V1`. The same wallet always derives the same key.

## `SealedBoxCipher`

```python
cipher = vorq.SealedBoxCipher()                                  # own key from $VORQ_CIPHER_KEY
box = vorq.SealedBoxCipher(recipient_public_key=peer).encrypt(b"...")  # also needs an own key
```

| Member | Meaning |
| --- | --- |
| `SealedBoxCipher(private_key=None, *, recipient_public_key=None, key_env="VORQ_CIPHER_KEY")` | Own Curve25519 private key (hex) from the argument or the environment variable. Raises `ValueError` without one. |
| `SealedBoxCipher.generate()` | A fresh keypair. |
| `SealedBoxCipher.from_seed(seed)` | A keypair from 32 seed bytes. |
| `.public_key` | This cipher's public key, in hex. |
| `.encrypt(data)` | Seals to `recipient_public_key` (anonymous sender). Raises `ValueError` if none is set. |
| `.decrypt(data)` | Opens a box addressed to this cipher's own key. |

Results sealed to one key open only with that key. When you switch between a derived and an
explicit key, keep the old one until its jobs have settled.

## `ChainContext`

Returned by `await client.chain_context()`, read once from `GET /evm/chain`. Every on-chain
signature takes it; there is no default deployment.

| Field | Meaning |
| --- | --- |
| `chain_id` | The deployment's chain id. |
| `job_registry`, `provider_registry`, `ask_registry` | Contract addresses. |
| `usdc` | The payment token's address. |
| `decimals` | The payment token's decimals. A USD amount is `atomic / 10^decimals`. |
| `token_domain` | The payment token's EIP-712 `name` and `version`. |

The session handshake is signed under the chain id that `GET /auth/nonce` announces.

## EIP-712 types

| Artifact | Domain | Type |
| --- | --- | --- |
| `Order` | `{name: "VORQ Jobs", version: "2", chainId, verifyingContract: job_registry}` | `Order(bytes32 c,uint32 modelId,uint32 slaSecs,uint128 rateIn,uint128 rateOut,uint32 unitsIn,uint32 unitsOut,uint32 designated,uint64 expiresAt)` |
| `Cancel` | same as `Order` | `Cancel(bytes32 jobId,uint64 issuedAt)` |
| Payment authorization | `{name, version}` from `token_domain`, `chainId`, `verifyingContract: usdc` | `ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)` |
| `VorqSession` | `{name: "VORQ Session", version: "1", chainId}`, no `verifyingContract` | `VorqSession(address address,string nonce)` |

Test vectors for these types are in
[`tests/vectors/signing-v3.json`](https://github.com/vorq-ai/vorq-client-sdk-python/blob/main/tests/vectors/signing-v3.json).

**Order.**

- `c` is the container commitment (below), so the signature binds one exact payload.
- `modelId` is the catalog's numeric id and `slaSecs` the window in seconds.
- `rateIn` and `rateOut` are the atomic integers of the USD rates: `rate × 10^decimals`.
- `designated` is the provider's registry id, or `0` for an open order.
- `expiresAt` is now plus the SLA window plus `VORQ_SETTLEMENT_MARGIN`, capped at 24 hours.
- The job id is not signed; it is `keccak256(owner ‖ c)`.

**Payment authorization.** `from` is your address, `to` is `job_registry`, `value` is the quoted
USD amount as the token's atomic integer, `validAfter` is `0`, `validBefore` is `expiresAt + 1`, and `nonce` is the job id, which
makes the authorization single-use.

## Container format

```
container = 0x01 ‖ seed_wrap (80 bytes) ‖ ciphertext
c         = keccak256(0x01 ‖ seed_wrap ‖ keccak256(ciphertext))
job_id    = keccak256(owner ‖ c)
dek       = HKDF-SHA256(ikm = seed, salt = "", info = "vorq-dek" ‖ owner (20 bytes), L = 32)
```

- `seed_wrap` is a sealed box over a fresh 32-byte seed, addressed to the chosen provider's key
  or the coordinator's escrow key.
- `ciphertext` is the canonical JSON envelope `{v: "vorq-env-v1", owner, result_key, input}`
  (plus `custom_id` when set), encrypted under `dek` with a libsodium secret box.

Test vectors are in
[`tests/vectors/container-v1.json`](https://github.com/vorq-ai/vorq-client-sdk-python/blob/main/tests/vectors/container-v1.json).

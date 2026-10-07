---
title: Encryption and trust
description: What the client seals, who can open it, how results come back, and what travels in the clear.
---

The coordinator never accepts a plaintext prompt. The client seals every payload in your
process, and results come back sealed to a key only you hold.

## What is sealed

The client wraps your input in an envelope, `{v, owner, result_key, input}` plus your
`custom_id` if you set one, and seals it into a **container**:

1. A fresh random 32-byte **seed** is generated for the job.
2. The encryption key is derived from the seed and your wallet address
   (HKDF-SHA256), and the envelope is encrypted under it.
3. The seed is sealed (a libsodium sealed box) to the recipient: the matched provider's
   registered key, or, for an [open order](./bids-and-matching.md#resting-orders), the coordinator's
   escrow key.

Because the key is derived with the owner's address, a seed copied onto someone else's order
yields a key that opens nothing.

## What is signed

The order signs a **commitment** to the container rather than the payload itself:
`keccak256(version ‖ seed_wrap ‖ keccak256(ciphertext))`. The job id is
`keccak256(owner ‖ commitment)`. So the signature binds the order to one exact payload, and
the client knows the job id before it sends anything.

## Who can read what

| Party | Sees |
| --- | --- |
| Coordinator | The order terms (model id, SLA, rates, unit counts, designated provider, expiry), your address and the ciphertext. |
| Matched provider | The opened envelope: your input, your address and your result key. |
| Coordinator escrow (open orders only) | The seed, which it releases to the provider that claims the order. |
| Anyone who knows the result's content address | The sealed result bytes, which only your result key opens. |

Batch `metadata` is the one field you send that is stored in plaintext.

## How results come back

Your **result key** is a Curve25519 key. A client built on `VORQ_WALLET_KEY` derives it
deterministically from a wallet signature over a fixed message, so it survives restarts with
nothing to store. You can hold an explicit key instead (see
[Manage keys and sessions](../guides/manage-keys-and-sessions.md#hold-the-result-key-explicitly)).

The provider seals the result to the key named in your envelope. A settled job names its result
by content address, and the client reads the bytes from a storage gateway without any
authorization: the name is the only way to find them, and only your key opens them.

## Verification

The client verifies the keys it seals to when it has a [`Verifier`](../reference/verifier.md):

- **Escrow key.** Before sealing an open order, the client checks that the coordinator's
  announced escrow key is fresh and bound to its evidence. There is no fallback: an escrow key
  that doesn't verify means nothing is posted.
- **Provider attestation.** `submit(..., confidential=True)` seals only to a provider whose
  attestation evidence verifies against the on-chain allowlist. It fails closed: when nothing
  verifies, it raises before sealing. This release ships no validator for hardware evidence
  types, so outside the verifier's development `mock` mode a confidential submission raises
  `VerificationError`.

## Signatures

Every signature is made locally with your wallet, under EIP-712: the order and the cancel on the
job registry's domain, the payment authorization on the payment token's own domain, and the
session handshake on an off-chain domain that no contract accepts. The exact types are in
[Signers and ciphers](../reference/signing.md#eip-712-types).

---
title: Verifier
description: Reference for vorq.Verifier, which checks the coordinator's escrow key and provider attestation before the client seals to them.
---

Pass a `Verifier` to `vorq.Client` (or `sealing_http_client`) to post open orders and to use
`submit(..., confidential=True)`. Create one per client so its allowlist cache is shared.

```python
verifier = vorq.Verifier(
    base_url: str,
    *,
    mode: str = "structural",
    min_tcb_svn: int = 1,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
    allowlist_ttl_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
)

client = vorq.Client(base_url=base_url, verifier=verifier)
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `base_url` | — | Where chain state is read from (`/evm/allowlist`, `/evm/providers/{id}`). Usually the coordinator. |
| `mode` | `"structural"` | `"structural"` runs every check except vendor quote validation. `"mock"` also accepts mock evidence, for local development only. Other values raise `ValueError`. |
| `min_tcb_svn` | `1` | Minimum TCB security version the evidence must report. |
| `timeout` | `30.0` | Per-request HTTP timeout. |
| `transport` | `None` | Custom `httpx` transport. |
| `allowlist_ttl_s` | `60.0` | How long a fetched allowlist is reused. Keep it short so revocations take effect. |
| `clock`, `wall_clock` | `time.monotonic`, `time.time` | The clocks behind the cache TTL and the key freshness check. |

## Methods

| Method | Meaning |
| --- | --- |
| `await verifier.allowlist()` | The allowlist entries, re-read after the TTL. |
| `await verifier.refresh()` | Drops the cache and re-reads now. |
| `verifier.invalidate()` | Drops the cache. |
| `await verifier.aclose()` | Closes the HTTP client. |

## Escrow key checks

Before sealing an open order, the client reads `GET /key` (cached for 3 hours) and requires:

- a 32-byte hex `escrow_public_key`;
- a numeric `issued_at` within ±600 s of the local clock;
- `report_data` equal to `sha256(escrow_public_key ‖ "vorq-coordinator-escrow-v1")`;
- `debug: false`.

Evidence of type `static-coordinator-v1`, an operator-held escrow key with no measured image,
is accepted in every mode; what you trust is then the coordinator you chose and its operator.
`mock-coordinator-v1` is accepted only in `mock` mode, and must also match an active allowlist
entry and meet `min_tcb_svn`. Any other type is refused. A failure raises `EscrowKeyUnverified`
and nothing is posted.

## Confidential submissions

With `confidential=True`, a provider is eligible only if all of these hold:

- its evidence type has a validator;
- its measurement matches an `active`, non-revoked image entry on the on-chain allowlist;
- the evidence's `report_data` equals `sha256(box_key ‖ operator)` from the provider's own
  record;
- the evidence states `debug: false`;
- its TCB version is at least `min_tcb_svn`;
- the key in the challenge is the verified record's key.

Candidates are tried in the order the coordinator ranks them, and the first that verifies is
sealed to. Malformed evidence counts as a failed check; chain state that can't be read raises.

This release ships no validator for hardware evidence types. Only `mock-cvm-v1` evidence is
recognized, and only in `mock` mode, so in `structural` mode every provider is refused.

| Situation | Raised |
| --- | --- |
| The client has no `verifier` | `ValidationError`, before any request. |
| No candidate verified, or a pinned `provider=` failed | `VerificationError`. A failed pin is never replaced by another provider. |
| The probe named no candidate | `VerificationError`. A resting order is sealed to the escrow key, not an attested provider; submit without `confidential=True` to rest as an open order. |

In every case the refusal happens before the payload is sealed.

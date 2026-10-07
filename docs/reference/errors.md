---
title: Errors
description: Reference for the vorq exception hierarchy and when each exception is raised.
---

Every failed request or job raises a `VorqError`. Constructing a client, signer or cipher with
no key raises `ValueError`.

```
VorqError                     # base; .type, .status_code, .request_id
├── AuthenticationError       # 401
├── NotFoundError             # 404
├── StateConflictError        # 409
├── ValidationError           # 400, or a local refusal before any request
├── VerificationError         # a key or attestation could not be verified
│   └── EscrowKeyUnverified   # an open order could not be sealed; nothing was posted
├── ResultIntegrityError      # a result can't be read or opened (also a ValueError)
├── WaitTimeout               # a wait elapsed; .job_id
├── JobFailed                 # the job ended failed or cancelled; .error_type, .job_id
└── BatchFailed               # the batch input file was refused; .batch_id
```

All classes are importable from `vorq`.

## `VorqError`

| Attribute | Meaning |
| --- | --- |
| `.type` | The error type from the coordinator's error body, or the SDK's own type for a local error. |
| `.status_code` | The HTTP status the error arrived with, or `None` when the SDK raised it locally. |
| `.request_id` | The `x-request-id` correlation id. Include it in bug reports. |

Statuses without a subclass, such as `402`, `429` and `5xx`, raise a plain `VorqError`.

## When each is raised

| Exception | Raised when |
| --- | --- |
| `AuthenticationError` | The session token was rejected (a client with a signer first re-mints it once). |
| `NotFoundError` | An unknown job, model or file. |
| `StateConflictError` | An illegal state change, such as cancelling a claimed or ended job. |
| `ValidationError` | The coordinator answered `400`; or, locally: a param that fails the model's schema, a rate that is not a USD decimal string or `Decimal`, a model missing from the catalog, invalid media inputs or `units_out`, a submission without a signer or cipher, a cancel without a signer, `confidential=True` without a verifier, or an invalid batch line. |
| `VerificationError` | A pinned provider publishes no key, or a confidential submission found no provider it could verify. Raised before the payload is sealed. |
| `EscrowKeyUnverified` | An open order's escrow key didn't verify, or the client has no `verifier`. Nothing was posted. Configure a verifier, pin a provider with `provider=N`, or raise the ceilings. |
| `ResultIntegrityError` | A `completed` job names no `result_cid`; the bytes aren't a result object; the result doesn't open with this client's key; the result is sealed but no cipher is configured; or a frame isn't valid base64. |
| `WaitTimeout` | `result()` or a batch wait reached its timeout. The job keeps running; `.job_id` is the job (or batch) id. |
| `JobFailed` | `result()` found the job `failed` or `cancelled`. |
| `BatchFailed` | A batch ended `failed` because its input file was refused. Per-line failures are `JobError` values, not this. |

A gateway read that fails or can't connect raises a plain `VorqError`.

## `JobFailed.error_type`

| `.error_type` | Status | Meaning |
| --- | --- | --- |
| `provider_fail` | `failed` | The provider reported it could not deliver. |
| `reclaim` | `failed` | A provider claimed the job and didn't settle within the window. |
| `cancelled` | `cancelled` | You cancelled the job. |
| `expired` | `cancelled` | No provider claimed the job before it expired. |

When the job names no cause the SDK recognizes, `.error_type` is the status.

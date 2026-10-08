---
title: Client
description: Reference for vorq.Client, its constructors, submit, job, models and the request and retry behavior.
---

`vorq.Client` is async-only: every network method is a coroutine. The one exception is
`client.job()`, which makes no request. For synchronous code, see
[OpenAI transport](./openai-transport.md).

## Constructor

```python
client = vorq.Client(
    *,
    base_url: str = "https://api.vorq.co",
    timeout: float = 30.0,
    max_retries: int = 3,
    signer: Signer | None = None,
    cipher: Cipher | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    verifier: Verifier | None = None,
    gateway: str | None = None,
)
```

With `VORQ_WALLET_KEY` set, `vorq.Client()` builds a `WalletSigner` from it, mints the
`vorq_sess_…` session token from a wallet signature, re-mints it 60 seconds before it expires,
and derives the result cipher from the wallet. With no `signer=` and no `VORQ_WALLET_KEY`, it
raises `ValueError`.

| Parameter | Meaning |
| --- | --- |
| `base_url` | The coordinator's origin. Pass your coordinator's URL. |
| `timeout` | Connect and connection-pool timeout per request, in seconds. Read and write timeouts are fixed at 900 s so large uploads can finish. It doesn't bound how long a job takes; use `handle.result(timeout=...)`. |
| `max_retries` | Cap on automatic retries per request. See [Retry policy](#retry-policy). |
| `signer` | Overrides the wallet signer, for example with a KMS-backed signer. See [`Signer`](./signing.md#signer). |
| `cipher` | Overrides the result cipher. An explicit cipher always wins. Otherwise a `WalletSigner` derives one, and any other client uses `VORQ_CIPHER_KEY` if it is set. |
| `transport` | Replaces the underlying `httpx` transport: a proxy, a custom pool or a test double. |
| `verifier` | A [`vorq.Verifier`](./verifier.md). Required to post an [open order](../concepts/bids-and-matching.md#resting-orders), including every line of a batch without `providers`, and for `confidential=True`. |
| `gateway` | The storage gateway result bytes are read from. See [Reading results](#reading-results). |

The client is an async context manager (`async with vorq.Client(...) as client:`); otherwise
call `await client.aclose()`.

### `from_session_token()`

```python
client = vorq.Client.from_session_token(token: str, *, <same keyword arguments>) -> Client
```

Builds a client on a pre-minted `vorq_sess_…` token. The token is never rotated ahead of expiry.
With a `signer=`, a `401` re-mints it once; without one, the token is used as-is, and the client
can read jobs but raises `ValidationError` on `submit` and `cancel`. Without `cipher=`, the
client uses `VORQ_CIPHER_KEY` if it is set.

### `mint_session_token()`

```python
token = vorq.mint_session_token(
    *,
    signer: Signer | None = None,
    base_url: str = "https://api.vorq.co",
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> str
```

Runs the `/auth/nonce` → sign → `/auth/session` handshake once, synchronously, with `signer` or
`VORQ_WALLET_KEY`, and returns the token. Raises `ValueError` without a wallet and
`httpx.HTTPStatusError` when the coordinator refuses. Use it to call routes that carry no prompt
from your own HTTP code; prompts must go through `submit` or the sealing transport.

## `submit()`

```python
handle = await client.submit(
    model: str,
    input: str | dict,
    sla: str = "batch",
    max_rate_in: str | Decimal | None = None,
    max_rate_out: str | Decimal | None = None,
    provider: int | None = None,
    validate_params: bool = True,
    *,
    confidential: bool = False,
    units_out: int | None = None,
    custom_id: str | None = None,
) -> JobHandle
```

Seals, signs, pays for and posts one job to `POST /v1/jobs`, for any modality. The flow is
described in [Bids and matching](../concepts/bids-and-matching.md#how-an-order-is-matched).

```python
# One ceiling, no provider: never more than $0.60 per 1M input tokens.
handle = await client.submit(model="moonshotai/kimi-k3", input="Hello", max_rate_in="0.6")
```

A ceiling protects you from being overcharged: the order signs the matched provider's ask, and
never a rate above the ceiling on that side. Set it too low and no provider matches: the order
[rests](../concepts/bids-and-matching.md#resting-orders) and may expire without being served.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `model` | — | A model id as listed by [`models.list()`](#modelslist). The order signs the catalog's numeric `model_id`, so an unlisted id raises `ValidationError`. |
| `input` | — | A `str` is sent as `{"input": str}`. A `dict` is the model's own input object and is sent as-is. |
| `sla` | `"batch"` | `"async"` (`"1h"`) or `"batch"` (`"24h"`). A `"batch"` job usually takes minutes to a few hours; 24 hours is the maximum. Other `<n>h`, `<n>m` or `<n>s` strings are passed through for the network to validate. |
| `max_rate_in` | `None` | The most the order pays for the input side, in USD per 1,000,000 input units, as a decimal string (`"0.05"`) or a `Decimal`. `None` is no ceiling on that side. The order signs the ask of the first provider within the ceilings; when none is, it [rests](../concepts/bids-and-matching.md#resting-orders). An `int` or `float`, or more fraction digits than the payment token's decimals, raises `ValidationError`. |
| `max_rate_out` | `None` | The most it pays for the output side, same unit. |
| `provider` | `None` | Pins a provider by registry id: only its ask is considered, and a resting order is sealed to its registered key. A provider that publishes no key raises `VerificationError`. With no ceiling named, `ValidationError` is raised when it is not live for the model. |
| `validate_params` | `True` | Check a `dict` input against the model's published schema first. See [Local param validation](#local-param-validation). |
| `confidential` | `False` | Seal only to a provider whose attestation verifies. Requires `verifier=`. See [`Verifier`](./verifier.md#confidential-submissions). |
| `units_out` | `None` | Overrides the declared output units, zero included. Must be a non-negative `int`. See [Units](./units.md). |
| `custom_id` | `None` | Your own label. It travels sealed and comes back as `.custom_id` on the result. |

`submit` requires both a signer and a cipher and raises `ValidationError` before any request
without them. Sealed containers up to 15,679,488 bytes are posted inline as base64; larger ones
are first uploaded with `POST /v1/files` and referenced by content id.

Returns a [`JobHandle`](./job-handle.md).

### Local param validation

When the model's entry in `GET /v1/models` carries a `vorq.params_schema` (JSON Schema), a
`dict` input is checked against it before anything is sealed:

- A key the schema marks `false`, or a value that fails its subschema, raises
  `ValidationError`.
- `reasoning_max_tokens` must be below the output cap, and `min_tokens` must not exceed it.
- A key the schema doesn't list triggers a `UserWarning` and is sent unchanged.

When the model publishes no schema, or the model list can't be fetched, validation is skipped.

## `job()`

```python
handle = client.job(job_id: str) -> JobHandle
```

Re-attaches to an existing job. Makes no network call and never creates a job.

## `models`

### `models.list()`

```python
await client.models.list() -> list[dict]
```

Returns the `data` array of `GET /v1/models`. Each entry is an OpenAI-style model object (`id`,
`object`, `owned_by`) plus a `vorq` block carrying `model_id` (the numeric id an order signs)
and `enabled`. Pass the `id` as `submit(model=...)`.

### `models.params_schema()`

```python
await client.models.params_schema(model: str) -> dict | None
```

The model's `vorq.params_schema`, or `None` when the model isn't listed or publishes none. The
model list behind it is cached for 5 minutes.

## `batches`

`client.batches.submit(...)` and `client.batches.get(batch_id)`. See [Batches](./batches.md).

## `chain_context()`

```python
ctx = await client.chain_context() -> ChainContext
```

The deployment every signature belongs to, read once from `GET /evm/chain` and cached for the
client's life. See [`ChainContext`](./signing.md#chaincontext).

## `fetch_blob()`

```python
data = await client.fetch_blob(cid: str) -> bytes
```

Reads content-addressed bytes from the configured gateway. See [Reading results](#reading-results).

## Reading results

A settled job names its result by `result_cid`. The client reads it with
`GET {gateway}/ipfs/{cid}`, without authorization, and opens it with its cipher. The coordinator
serves no result bytes.

The gateway is, in order: the `gateway=` argument, then `VORQ_PIN_GATEWAY`, then the built-in
public gateway, `https://ipfs.filebase.io`. An empty string at either of the first two levels disables it, and reads then
raise `VorqError`.

A fresh result can take a few seconds to become readable, so a `404`, a `5xx` or a dropped
connection is retried: 8 attempts, 1.5 s apart. Any other error status raises on the first
answer. Every failure raises `VorqError`.

## Retry policy

- A request is retried only when the response carries `X-Vorq-Retryable: true`, with
  exponential backoff and jitter, up to `max_retries` times. Other errors raise immediately.
- A `401` re-mints the session token once, when the client holds a signer, and repeats the
  request.
- Submissions (`POST /v1/jobs`, `POST /v1/files`, `POST /v1/batches`) are never retried on an
  error status. If the connection drops while posting a job, the client reads the job back by
  its precomputed id and re-sends only if it doesn't exist.
- If the gas fee changes during a submission, the coordinator returns a new quote and the client
  re-signs the payment, up to 3 attempts in total.

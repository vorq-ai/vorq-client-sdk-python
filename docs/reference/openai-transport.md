---
title: OpenAI transport
description: Reference for vorq.sealing_http_client and SealingTransport, the sealed path for the stock openai package.
---

For a walkthrough, see [Use the OpenAI client](../guides/use-the-openai-client.md).

## `sealing_http_client()`

```python
http_client = vorq.sealing_http_client(
    *,
    base_url: str = "https://api.vorq.co",
    signer: Signer | None = None,
    cipher: Cipher | None = None,
    verifier: Verifier | None = None,
    timeout: float = 30.0,
    inner_transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Client
```

Returns an `httpx.Client` for `openai.OpenAI(http_client=...)`, whose transport is a
`vorq.SealingTransport` built from the same arguments.

| Parameter | Meaning |
| --- | --- |
| `base_url` | The bare coordinator origin, without `/v1`. The `openai.OpenAI(base_url=...)` you pair it with needs the `/v1` suffix. |
| `signer`, `cipher` | As on [`vorq.Client`](./client.md#constructor). By default the wallet comes from `VORQ_WALLET_KEY` and the result cipher is derived from it. |
| `verifier` | Needed for open orders: a ceiling named in the `vorq` block that no provider is within, with no provider pinned. |
| `timeout` | Per-request connect timeout, as on `vorq.Client`. |
| `inner_transport` | An `httpx` async transport for the native client underneath, for example a test double. |

**Sync-only.** Each call runs a fresh native `vorq.Client`, with its own session handshake, on
a private event loop. Called inside a running event loop, it raises `RuntimeError`, which the
`openai` package reports as `openai.APIConnectionError`.

## Intercepted routes

| Route | Behavior |
| --- | --- |
| `POST /v1/responses` | Seals and submits the request as a job. |
| `GET /v1/responses/{id}` | Reads the job; when it is completed, fetches and opens its result. |
| `POST /v1/responses/{id}/cancel` | Signs and relays a cancel. The response id is the job id. |

### Create

The request body becomes the model input, minus `model`, `background`, `vorq` and
`metadata`. A list-valued `input` is sealed under the key `messages`. The same
[local param validation](./client.md#local-param-validation) as `submit` runs, and can't be
skipped.

The `vorq` block (sent through `extra_body={"vorq": {...}}`):

| Key | Meaning |
| --- | --- |
| `sla` | The completion window. Default `"1h"`. |
| `max_rate_in` | The most the order pays for the input side, in USD per 1M units, a decimal string such as `"0.05"`. Optional: a side with no ceiling pays the provider's ask. See [Bids and matching](../concepts/bids-and-matching.md#how-an-order-is-matched). |
| `max_rate_out` | The most it pays for the output side, same unit. |
| `provider` | A provider id to pin. |

Other keys in the block are ignored.

- **`background=True`**: returns at once with a `queued` Response whose `id` is the job id.
- **Without `background`**: blocks until the job settles or its `sla` window elapses. A timeout
  surfaces as a `400`.

Refused with a `400` before anything is sealed:

| Option | Why | Instead |
| --- | --- | --- |
| `model` missing or not a string | The model decides which providers can serve the order. | `client.models.list()` |
| `stream=True` | A sealed result is one object, opened when the job settles. | `background=True` and poll, or block |
| `metadata` | A stable caller-chosen identifier would link your jobs across providers, and nothing on the network reads it. | Keep it locally, keyed by the response id |

### Response objects

Every Response carries a top-level `vorq` block echoing the job's terms as the coordinator
reports them.

- A text result becomes one `message` output item with `output_text`, plus `usage`.
- A media result becomes one `image_generation_call` item per frame, with the frame's base64
  in `result`. Video uses the same item type; read `content_type` from the native
  [`MediaResult`](./results.md#mediaresult) to tell them apart.
- An embeddings result renders no output items.
- A job that failed or was cancelled carries an `error` object whose `code` is the end cause.

## Forwarded routes

These routes carry no prompt and are forwarded to the coordinator over the transport's own
wallet session. Each is matched on method and path.

| Route | Why no prompt travels |
| --- | --- |
| `GET /v1/models` | A catalog read. |
| `POST /v1/batches`, `GET /v1/batches`, `GET /v1/batches/{id}` | A batch create names an already-uploaded, already-sealed file. |
| `POST /v1/batches/{id}/cancel` | No body. |
| `GET /v1/files/{id}`, `GET /v1/files/{id}/content` | A file object, and an output file whose results are sealed. |
| `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel` | A status read and a cancel. |

The optional `metadata` on a batch create is stored in plaintext. The `Authorization` header
your `openai` client sends is dropped. A successful forwarded response keeps its status, body,
content type and `x-request-id`; an error is returned as described in
[Errors and retries](#errors-and-retries).

## Refused routes

**Every other route gets a `400` from the transport itself, before the request body is read**,
so nothing leaves your process. That includes `chat.completions`, `files.create` (a batch input
file would go up in plaintext; use `client.batches.submit()`), `POST /v1/jobs`, and any route
the `openai` package adds later.

Both the intercepts and the forward list match `/v1/...` exactly. An `OpenAI(base_url=...)`
under a sub-path such as `https://host/api/v1` matches nothing, Responses included, and every
call is refused.

## Errors and retries

A coordinator error keeps its HTTP status, so the `openai` package raises its usual classes:

| Coordinator answer | `openai` exception |
| --- | --- |
| Cancel of a claimed job (`409`) | `openai.ConflictError` |
| Unknown response id (`404`) | `openai.NotFoundError` |
| `429` | `openai.RateLimitError` |
| `5xx` | `openai.InternalServerError` |

Errors the transport raises itself, including refusals and a synchronous wait that timed out,
are `400` / `openai.BadRequestError`.

`POST /v1/responses` and `POST /v1/batches` failures are marked not retryable, so the `openai`
package doesn't retry them: a retry would create a second job or batch and could bill twice.
Reads and cancels keep the package's normal retry behavior.

## Differences from OpenAI

| | OpenAI | VORQ |
| --- | --- | --- |
| Prompt delivery | Plaintext request body. | Sealed before it leaves your process. |
| Blocking create | Waits as long as the response takes. | Waits at most the job's `sla` window. |
| Response body | No VORQ fields. | A top-level `vorq` block. |
| `GET /v1/models` | Plain model list. | Each entry also carries a `vorq` block (`model_id`, `enabled`). |
| Cancel | Stops a background response. | Refused once a provider has claimed the job. |
| Streaming | Supported. | Refused. |

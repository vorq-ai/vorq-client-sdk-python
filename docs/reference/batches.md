---
title: Batches
description: Reference for client.batches, BatchHandle and JobError.
---

## `batches.submit()`

```python
batch = await client.batches.submit(
    requests: list[dict] | str,
    completion_window: str = "batch",
    *,
    providers: list[int] | None = None,
    metadata: dict[str, str] | None = None,
    validate_params: bool = True,
) -> BatchHandle
```

| Parameter | Meaning |
| --- | --- |
| `requests` | OpenAI batch lines, or a path to a JSONL file of them. Each line is `{"custom_id", "method", "url", "body": {"model", ...}}`. |
| `completion_window` | `"async"` / `"1h"` or `"batch"` / `"24h"`, like `submit(sla=...)`. |
| `providers` | Provider ids to assign priced lines to, round-robin. Without it, every priced line is an open order sealed to the coordinator's verified escrow key, which requires a client built with `verifier=`. Lines with no rates ignore it and go where the plan puts them. |
| `metadata` | Stored in plaintext on the batch record. |
| `validate_params` | Check each line's input against its model's schema, as `submit` does. |

Line rules, all checked before anything is sealed (violations raise `ValidationError`):

- `body.model` is required.
- `url` is `/v1/responses` (the default) or `/v1/embeddings`, and every line uses the same one.
- `custom_id` is optional, 1–64 characters and unique in the batch. It travels sealed.
- `max_rate_in`, `max_rate_out` and `units_out` in `body` set the line's order terms; the rest
  of `body` is the model input. The ceilings are USD per 1M units, as decimal strings (`"0.05"`)
  or `Decimal`, each optional; an `int` or `float` is refused.
- An empty batch is refused.

Every line is **planned**: one `POST /v1/batches` with no file sends, per model and pair of
ceilings, the line count and summed units, and the coordinator answers which providers within
the ceilings take how many lines at which ask. No provider gets more lines than its on-chain
capacity leaves free. Each planned line signs its provider's ask and is pinned to it.

A line the plan cannot place **rests** at its ceilings, spread by `providers`. A side with no
ceiling rests at the market rate, the cheapest live ask's, read by one more plan. If lines with
no ceiling at all do not all fit in the window, or there is no live ask to take a market rate
from, `ValidationError` is raised and nothing is signed.

Then every line is sealed, signed and paid for in your process. The fees on top of each line's
cap are read once, from one quote. The file is uploaded (`POST /v1/files`, `purpose=batch`) and
the batch created (`POST /v1/batches`).

## `batches.get()`

```python
batch = client.batches.get(batch_id: str) -> BatchHandle
```

Re-attaches to a batch. Makes no network call.

## `BatchHandle`

```python
batch.id -> str
batch.job_ids -> list[str] | None
batch.output_file_id -> str | None
batch.error_file_id -> str | None
batch.request_counts -> dict | None
await batch.status() -> str
await batch.results(timeout: float | None = None) -> list[TextResult | MediaResult | EmbeddingResult | JobError]
await batch.consume(on_result, on_error=None, timeout=None) -> None
await batch.cancel() -> None
```

| Member | Meaning |
| --- | --- |
| `job_ids` | Each line's job id, in input order. Set by `submit`; `None` on a re-attached handle. |
| `output_file_id`, `error_file_id`, `request_counts` | Copied from the batch object on each read. |
| `status()` | One `GET /v1/batches/{id}`; returns the batch status. |
| `results()` | Waits until the batch is terminal, then returns every line's result or `JobError`. |
| `consume()` | Same wait, then calls `on_result` or `on_error` per line. Callbacks may be plain functions or coroutines; coroutine callbacks run concurrently. |
| `cancel()` | Cancels lines that are still open. A line a provider has already claimed runs to its end. |

`results()` and `consume()`:

- Terminal statuses are `completed`, `failed`, `expired` and `cancelled`.
- Results arrive in file order (every settled line, then every failed one), not input order.
  Match on `.custom_id`, or on `.job_id` against `job_ids`.
- Waiting polls once a minute for the first 15 minutes, every 3 minutes for the rest of the
  first hour, and every 10 minutes after that. Each poll is one `GET /v1/batches/{id}`, whatever
  the line count.
- The default timeout is the batch's completion window. On expiry they raise `WaitTimeout`,
  whose `.job_id` is the batch id.
- A batch that ends `failed` had its input file refused, and raises `BatchFailed` with
  `.batch_id`.

## `JobError`

A line that never delivered.

| Field | Meaning |
| --- | --- |
| `.message` | Human-readable reason. |
| `.type` | For a line that became a job: `provider_fail`, `reclaim`, `cancelled` or `expired`. For a line refused before it became one: the coordinator's refusal code. |
| `.job_id` | The line's job id, when it has one. |
| `.custom_id` | `None`: the label is sealed, and a failed line has no sealed result to read it from. Match on `.job_id`. |
| `.raw` | The error object as received. |

---
title: JobHandle
description: Reference for vorq.JobHandle, the view of one job that you poll, wait on, read and cancel.
---

A `JobHandle` is returned by `client.submit()` and `client.job(job_id)`. It is only a view: the
job lives on the network.

```python
handle.id -> str
handle.task_cid -> str | None
await handle.status() -> str
await handle.result(timeout: float | None = None) -> TextResult | MediaResult | EmbeddingResult
await handle.cancel() -> None
```

## `id`

The job id, a `0x`-prefixed hex string. An attribute, not a method. Persist it before waiting.

## `task_cid`

The content address of the job's sealed input, as returned by the submission. `None` on a
handle from `client.job()`.

## `status()`

One `GET /v1/jobs/{id}`. Returns `queued`, `in_progress`, `completed`, `failed` or `cancelled`.
The first two are non-terminal.

## `result()`

Polls until the job is terminal, then returns its result:

- `completed`: fetches the bytes named by `result_cid`, opens them with the client's cipher and
  returns a [`TextResult`, `MediaResult` or `EmbeddingResult`](./results.md).
- `failed` or `cancelled`: raises `JobFailed`, whose `.error_type` is the end cause
  (`provider_fail`, `reclaim`, `cancelled` or `expired`), or the status when the job names no
  cause.

| Behavior | Value |
| --- | --- |
| Poll interval | The SLA window ÷ 60, held between 2 and 60 seconds. |
| SLA window | Known from `submit`. A re-attached handle reads it from the job, and assumes `1h` if the job states none. |
| Default `timeout` | The SLA window. Pass `timeout=` explicitly when re-attaching to a longer job. |
| On timeout | Raises `WaitTimeout` with `.job_id`. Nothing is cancelled; call `result()` again. |

A `completed` job that names no `result_cid`, or whose result can't be opened, raises
`ResultIntegrityError`.

## `cancel()`

Signs `Cancel(jobId, issuedAt)` with the client's wallet and posts `{issued_at, signature}` to
`POST /v1/jobs/{id}/cancel`, which the coordinator relays to the chain.

- Without a signer, raises `ValidationError` before sending anything.
- `issued_at` is the current Unix time and must be within ±600 s of block time.
- A job a provider has already claimed, or one that has ended, raises `StateConflictError`.

See [Cancel a job](../guides/cancel-a-job.md).

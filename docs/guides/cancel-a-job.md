---
title: Cancel a job
description: Cancel a job that no provider has claimed yet, and handle the refusals.
---

```python
try:
    await client.job(job_id).cancel()
except vorq.StateConflictError:
    print("too late: a provider has claimed the job, or it has already ended")
```

`cancel()` signs `Cancel(jobId, issuedAt)` with your wallet and posts it to the coordinator,
which relays it to the chain. Things to know:

- **Only unclaimed jobs can be cancelled.** Once a provider has claimed a job, it ends by
  settlement, provider failure or reclaim after its SLA window, and the cancel is refused with
  `StateConflictError`.
- **It needs the owning wallet.** A client without a signer, such as one built with
  `Client.from_session_token(token)` alone, raises `ValidationError` before sending anything.
- **Your clock matters.** `issued_at` is stamped from your system clock and must be within
  ±600 seconds of the chain's block time.

After a successful cancel, the job's status is `cancelled` and `handle.result()` raises
`JobFailed` with `error_type="cancelled"`.

To cancel a whole batch, call `await batch.cancel()`; see [Submit a batch](./submit-a-batch.md#re-attach-check-and-cancel).
Through the OpenAI client, use `client.responses.cancel(job_id)`.

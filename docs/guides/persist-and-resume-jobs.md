---
title: Persist and resume jobs
description: Save job ids before waiting, bound your waits and re-attach to a job from any process.
---

A job lives on the network, and a `JobHandle` is only a view of it. Losing a handle, or the
process that held it, loses nothing as long as you kept the job id.

## Save the id before you wait

```python
handle = await client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input="...", sla="batch",
                             max_rate_in="0.65", max_rate_out="1.3")
db.save_job(id=handle.id, status="submitted")   # before any await on the result
```

`handle.result()` keeps the calling coroutine waiting until the job settles, up to 24 hours on
the `batch` tier. A service shouldn't assume it outlives that window.

## Bound the wait

```python
try:
    result = await handle.result(timeout=900)
    db.update_job(id=handle.id, status="completed", output=result.text)
except vorq.WaitTimeout as e:
    db.update_job(id=e.job_id, status="waiting")   # re-attach later; do not resubmit
except vorq.JobFailed as e:
    db.update_job(id=e.job_id, status="failed", cause=e.error_type)
```

`WaitTimeout` cancels nothing. The job keeps running and you can call `result()` again.

## Re-attach from any process

```python
result = await client.job(job_id).result(timeout=86400)
```

`client.job()` makes no network call and never creates a job, so a rerun after a crash picks
up the same job instead of submitting a duplicate. A re-attached handle reads the job's SLA
window from the job itself and uses it as the default timeout.

Two rules make this safe:

- **Persist the id before you start waiting.**
- **Key every write by job id**, so replaying a write changes nothing.

## Read results in a process without the wallet key

Results are sealed to your result key, not to your wallet. A process that only reads results
can hold a session token and that key:

```python
import os
import vorq

async with vorq.Client.from_session_token(
    os.environ["VORQ_SESSION_TOKEN"],       # minted elsewhere, e.g. with vorq.mint_session_token()
    base_url="https://api.vorq.co",
    cipher=vorq.SealedBoxCipher(),          # the result key, from $VORQ_CIPHER_KEY
) as client:
    result = await client.job(job_id).result(timeout=600)
```

This works only if the submitting client sealed results to that same key, that is, it was
built with `cipher=vorq.SealedBoxCipher()` too. A client without a signer can read jobs but
can't submit or cancel, and its token is never rotated. See
[Manage keys and sessions](./manage-keys-and-sessions.md).

## Run many jobs at once

`await handle.result()` suspends one coroutine, not a thread, so fan-out is plain `asyncio`:

```python
handles = await asyncio.gather(*[
    client.submit(model="deepseek-ai/deepseek-v4-pro:fp8", input=p, sla="async",
                  max_rate_in="1", max_rate_out="2")
    for p in prompts
])
for h in handles:
    db.save_job(id=h.id, status="submitted")
results = await asyncio.gather(*[h.result() for h in handles])
```

Each waiting job costs one `GET` per poll interval: once a minute at first, and on the `24h`
tier every 3 minutes after 15 minutes of waiting and every 10 minutes after an hour. A batch
costs one `GET` per interval for all of its lines.

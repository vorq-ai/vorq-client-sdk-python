---
title: Job lifecycle
description: How a VORQ job moves from submission to settlement, and what the SLA window, statuses and end causes mean.
---

Every VORQ job is submit-and-poll. `submit()` returns as soon as the order is on the book, and
the result is read later, from any process that knows the job id.

## From submit to result

1. **Submit.** The client seals the payload, signs the order and a payment authorization, and
   posts them to the coordinator, which relays the order on-chain. The answer carries the job
   id.
2. **Queued.** The order waits for a provider whose ask it clears.
3. **In progress.** A provider has claimed the job. From here it can no longer be cancelled.
4. **Settled.** The provider delivers, seals the result to your result key, and settles. The
   job reports `completed` and names its result by a content address, `result_cid`.
5. **Read.** `handle.result()` fetches the bytes behind `result_cid` from a storage gateway and
   opens them with your key.

The job id is computed by the client before anything is sent, from your wallet address and a
commitment to the sealed payload. That is why a client whose connection drops mid-submit can
ask whether its job landed instead of posting it twice.

## SLA tiers

| Tier | Window | Use it for |
| --- | --- | --- |
| `async` (`"1h"`) | up to 1 hour | work you want back in minutes |
| `batch` (`"24h"`) | up to 24 hours | offline and bulk work |

Both tiers work the same way; the tier only sets the completion window. The window is a
maximum, and jobs usually finish sooner: a `batch` job typically takes minutes to a few hours. The window is signed into the order.

An order also carries an expiry: the SLA window plus a settlement margin (one hour by default),
capped at 24 hours from submission. An order no provider claims by then ends as `expired`.

## Statuses and end causes

`handle.status()` returns one of five statuses. `queued` and `in_progress` are non-terminal.

| Status | End cause (`JobFailed.error_type`) | Meaning |
| --- | --- | --- |
| `completed` | — | The provider delivered and settled. |
| `failed` | `provider_fail` | The provider reported it could not deliver. |
| `failed` | `reclaim` | A provider claimed the job and did not settle within the window. |
| `cancelled` | `cancelled` | You cancelled the job before it was claimed. |
| `cancelled` | `expired` | No provider claimed the job before it expired. |

Branch on the end cause, not the status: two different facts share each non-`completed` status.

## Waiting

`handle.result()` polls the coordinator. A `1h` job is read once a minute. A `24h` job is read
once a minute for the first 15 minutes of the wait, every 3 minutes for the rest of the first
hour, and every 10 minutes after that. A batch waits on the same schedule as a `24h` job, with
one read of the batch per poll whatever its line count. By default the wait lasts the job's
whole window, then raises `WaitTimeout`. Timing out cancels nothing.

## Jobs are durable, handles are not

The network stores every job. A `JobHandle` holds nothing a rerun couldn't rebuild from the
job id, so the production pattern is to persist the id before waiting and re-attach with
`client.job(job_id)` later. See [Persist and resume jobs](../guides/persist-and-resume-jobs.md).

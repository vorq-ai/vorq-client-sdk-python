---
title: Use the OpenAI client
description: Point the stock openai Python package at VORQ through the sealing transport, synchronously or in background mode.
---

The coordinator never accepts a plaintext prompt. To use the stock `openai` package, route it
through `vorq.sealing_http_client()`, a drop-in `http_client` that seals each Responses request
in your process. It is also the SDK's **synchronous** path: no `asyncio` needed.

## Install

```bash
pip install vorq openai
```

Set `VORQ_WALLET_KEY` as in the [Quickstart](../quickstart.md).

## Configure the client

```python
from openai import OpenAI
import vorq

client = OpenAI(
    base_url="https://api.vorq.co/v1",
    api_key="unused",                    # any non-empty string; the wallet authenticates
    http_client=vorq.sealing_http_client(),
)
```

- `sealing_http_client()` talks to `https://api.vorq.co` and signs with `VORQ_WALLET_KEY`.
- To post a bid that rests until a provider takes it, pass
  `verifier=vorq.Verifier("https://api.vorq.co")` as well: such an order is sealed to the
  coordinator's escrow key, which the client verifies first.

## Create a response and wait for it

```python
resp = client.responses.create(
    model="moonshotai/kimi-k3",
    input="Say hello.",
    extra_body={"vorq": {"sla": "batch"}},
)
print(resp.output_text)
```

Without `background`, the call blocks until the job settles, for at most the job's `sla`
window. If the job hasn't settled by then, the call raises `openai.BadRequestError`.

The `vorq` block carries the order terms the Responses schema has no field for: `sla`,
`rate_in`, `rate_out` and an optional `provider` id.

- `sla` defaults to `"1h"` on this path.
- `rate_in` and `rate_out` are USD per 1M units as decimal strings, such as `"0.05"`. A JSON
  number is refused.
- With no rates, the order takes the market: the first provider the coordinator ranks, at its
  own ask. See [Bids and matching](../concepts/bids-and-matching.md#no-bid-named).

## Create a response in the background

Use `background=True` whenever the wait might outlive your connection, for example on the
`"24h"` window:

```python
import time

resp = client.responses.create(
    model="moonshotai/kimi-k3",
    input="Summarize the attached notes in three bullet points.",
    background=True,
    extra_body={"vorq": {"sla": "batch"}},
)
job_id = resp.id                      # the response id is the job id; persist it
while resp.status in {"queued", "in_progress"}:
    time.sleep(30)
    resp = client.responses.retrieve(job_id)
print(resp.status, resp.output_text)
```

In either mode, a job that didn't deliver comes back as a Response with status `failed` or
`cancelled` and an `error` object whose `code` is the cause (`provider_fail`, `reclaim`,
`cancelled` or `expired`). It is not raised as an exception.

`client.responses.cancel(job_id)` cancels a job no provider has claimed yet. Once a provider has
claimed it, the cancel raises `openai.ConflictError`.

## Media and batches

- A media job comes back as `image_generation_call` output items, one per frame, with the
  frame's base64 in `result`. Video uses the same item type.
- Batches are created with the native `client.batches.submit()`; the stock client can then
  retrieve, list and cancel them and read their output files. See
  [Submit a batch](./submit-a-batch.md#with-the-openai-client).

## What doesn't work

- Calling the transport inside a running event loop. It raises, and `openai` reports an
  `APIConnectionError`. In async code, use `vorq.Client`.
- `stream=True`, request `metadata`, and every route other than Responses and a short list of
  read-only routes. These are refused with a `400` before anything is sent.
- Automatic retries of a failed create. Retry it yourself if you want one.

The full rules, including the forwarded routes and the error mapping, are in the
[OpenAI transport reference](../reference/openai-transport.md).

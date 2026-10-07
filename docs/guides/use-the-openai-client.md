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
- To let an order rest when no provider is within its ceilings, pass
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
`max_rate_in`, `max_rate_out` and an optional `provider` id.

```python
resp = client.responses.create(
    model="moonshotai/kimi-k3",
    input="Say hello.",
    extra_body={"vorq": {"sla": "batch", "max_rate_in": "0.6"}},
)
```

`max_rate_in` protects you from being overcharged: the order never signs an input rate above
it, and pays less when a provider asks less. Set it too low and no provider matches: the order
[rests](../concepts/bids-and-matching.md#resting-orders) and may expire without being served.

- `sla` defaults to `"1h"` on this path.
- `max_rate_in` and `max_rate_out` are the most the order pays, in USD per 1M units, as decimal
  strings such as `"0.05"`. A JSON number is refused. Each is optional.
- The order signs the ask of the first provider the coordinator ranks within the ceilings. See
  [Bids and matching](../concepts/bids-and-matching.md#how-an-order-is-matched).

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

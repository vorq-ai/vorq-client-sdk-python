---
title: Submit a batch
description: Seal and submit many requests as one batch, then collect and match the results.
---

A batch submits many requests in one upload. Every line is sealed, signed and paid for in your
process before the file leaves it.

## Submit the batch

Pass OpenAI-style batch lines, or the path to a JSONL file of them:

```python
batch = await client.batches.submit([
    {"custom_id": "en", "url": "/v1/responses",
     "body": {"model": "moonshotai/kimi-k3", "input": "Hello"}},
    {"custom_id": "fr", "url": "/v1/responses",
     "body": {"model": "moonshotai/kimi-k3", "input": "Bonjour"}},
], "batch")
print("batch:", batch.id)   # persist this, with batch.job_ids
```

In each line:

- `url` is `/v1/responses` (the default) or `/v1/embeddings`. One batch uses one endpoint.
- `body.model` is required. `rate_in`, `rate_out` and `units_out` in `body` set that line's
  order terms; everything else in `body` is the model input. Rates are USD per 1M units, as
  decimal strings (`"0.05"`) or `Decimal`.
- A line with neither rate takes the market. Before sealing, the client asks the coordinator for
  a plan: which providers take how many of those lines, and at which ask. A provider is never
  given more lines than its on-chain capacity leaves free, and each line is pinned to the
  provider it was planned to. If the network cannot take all of a model's unpriced lines in the
  window, the batch raises `ValidationError` before anything is signed.
- `custom_id` is optional: 1–64 characters, unique in the batch. It travels sealed inside the
  line and comes back on the opened result.

## Choose who serves it

- **Lines with no rates** go where the plan puts them; `providers` does not apply to them.
- **Priced lines without `providers`** are open orders sealed to the coordinator's escrow key,
  which any provider clearing its terms can claim. The client must be built with
  `verifier=vorq.Verifier(base_url)`.
- **Priced lines with `providers=[3, 7]`** are assigned round-robin to those provider ids, and each
  line is sealed to its provider's key.

## Collect the results

`results()` waits until the batch is terminal, then returns every line:

```python
for item in await batch.results():
    if isinstance(item, vorq.JobError):
        print("failed:", item.job_id, item.type, item.message)
    else:
        print(item.custom_id, item.text)
```

Results arrive in file order (every settled line, then every failed one), not input order.
Match a result on `.custom_id`, and a failed line on `.job_id` against `batch.job_ids`, which
lists each line's job id in input order. A failed line never carries its `custom_id`, because the
label is sealed.

To handle lines as callbacks instead, use `consume()`. Coroutine callbacks run concurrently:

```python
async def save(result):
    ...

await batch.consume(save, on_error=lambda err: print(err.job_id, err.type))
```

## Re-attach, check and cancel

```python
batch = client.batches.get(batch_id)   # no network call
print(await batch.status())            # one GET; terminal: completed, failed, expired, cancelled
results = await batch.results(timeout=3600)
```

`results()` and `consume()` wait up to the batch's completion window by default and raise
`WaitTimeout` when a bound elapses; nothing is cancelled. `await batch.cancel()` cancels lines
that are still open; a line a provider has already claimed runs to its end.

A batch whose input file was refused ends `failed` and raises `BatchFailed`.

## With the OpenAI client

The stock `openai` client can't create a batch through the sealing transport, because it
would upload a plaintext file. Submit with `client.batches.submit()`, then read it with either
client: `batches.retrieve`, `batches.list`, `batches.cancel` and
`files.content(output_file_id)` are forwarded. See
[OpenAI transport](../reference/openai-transport.md#forwarded-routes).

Full signatures: [Batches reference](../reference/batches.md).

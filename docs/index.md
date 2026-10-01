---
title: Python SDK
description: The async Python client for the VORQ inference exchange, what it does and when to use it.
---

`vorq` (on PyPI) is the async Python client for the VORQ inference exchange. It submits
inference jobs to a VORQ coordinator and reads back their results. Every payload is sealed in
your process before it is sent, and the result comes back sealed to a key only your wallet can
derive.

A submission returns a job id right away. The network completes the job within its SLA window,
and you collect the result by polling:

- **async** tier: up to 1 hour.
- **batch** tier: up to 24 hours.

## Who it is for

Python developers running inference that can wait minutes or hours: text generation,
embeddings, image and video generation, and large offline batches.

## Which interface to use

- **Async code**: use `vorq.Client`. Every network method is a coroutine, so one event loop can
  hold thousands of jobs in flight. Start with the [Quickstart](./quickstart.md).
- **Synchronous code, or code already written against the `openai` package**: pass
  `vorq.sealing_http_client()` to a stock `openai.OpenAI` client. See
  [Use the OpenAI client](./guides/use-the-openai-client.md).

## Requirements

- Python 3.11 or newer.
- The URL of a VORQ coordinator.
- A dedicated wallet key funded with your inference budget, set as `VORQ_WALLET_KEY`.

## Next steps

- [Quickstart](./quickstart.md): install the package and run a first job.
- [Guides](./guides/generate-images-and-video.md): images and video, batches, durable jobs,
  the OpenAI client, keys.
- [Concepts](./concepts/job-lifecycle.md): job lifecycle, bids and matching, units and cost,
  encryption.
- [Reference](./reference/client.md): the full public API.

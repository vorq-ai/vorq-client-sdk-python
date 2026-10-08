# vorq

Async Python client for the VORQ inference exchange.

[![PyPI](https://img.shields.io/pypi/v/vorq)](https://pypi.org/project/vorq/)

`vorq` submits inference jobs to a VORQ coordinator and reads back their results. Every
submission is sealed in your process before it is sent, so only the provider that runs the job
can read the prompt. A submission returns a job id right away, and the network completes the job
within its SLA window: up to 1 hour on the `async` tier, up to 24 hours on the `batch` tier
(usually minutes to a few hours).

## Features

- **Async client.** Every network method on `vorq.Client` is a coroutine, so one event loop can
  hold thousands of jobs in flight.
- **One flow for every modality.** `client.submit()` returns a `JobHandle`, and
  `handle.result()` returns a `TextResult`, `MediaResult` or `EmbeddingResult`.
- **End-to-end sealing.** Each payload is sealed to the provider that runs the job, or to the
  coordinator's verified escrow key when the order rests open. The result comes back sealed to a
  key derived from your wallet.
- **Wallet authentication.** Set `VORQ_WALLET_KEY`. The client mints and rotates its session
  token and signs every order and payment authorization on your machine.
- **Batches.** `client.batches.submit()` seals each line of an OpenAI-style batch file before
  anything is uploaded.
- **OpenAI compatibility.** `vorq.sealing_http_client()` lets the stock `openai` package use
  the Responses API against VORQ, with synchronous calls and sealed payloads.
- **Durable jobs.** You can re-attach to any job from its id with `client.job(id)`, from any
  process.

## Installation

```bash
pip install vorq
```

Requires Python 3.11 or newer. The OpenAI-compatible path also needs `pip install openai`.

## Quick start

```bash
export VORQ_WALLET_KEY=0x...   # a dedicated wallet funded with your inference budget
```

```python
import asyncio
import vorq

async def main():
    async with vorq.Client() as client:
        handle = await client.submit(
            model="moonshotai/kimi-k3",
            input="Summarize the plot of Hamlet in three bullet points.",
            sla="batch",
        )
        print("job:", handle.id)        # persist this before waiting

        result = await handle.result()  # polls until the job settles
        print(result.text)
        print(result.usage, result.cost)

asyncio.run(main())
```

VORQ runs on the Base Sepolia testnet: fund the wallet with free test USDC from
[faucet.circle.com](https://faucet.circle.com) (pick USDC and Base Sepolia). No ETH is needed.

## Documentation

Full documentation is at [docs.vorq.co/docs/python](https://docs.vorq.co/docs/python):

- [Quickstart](https://docs.vorq.co/docs/python/quickstart)
- Guides: [images and video](https://docs.vorq.co/docs/python/guides/generate-images-and-video),
  [batches](https://docs.vorq.co/docs/python/guides/submit-a-batch),
  [persisting and resuming jobs](https://docs.vorq.co/docs/python/guides/persist-and-resume-jobs),
  [the OpenAI client](https://docs.vorq.co/docs/python/guides/use-the-openai-client)
- Concepts: [job lifecycle](https://docs.vorq.co/docs/python/concepts/job-lifecycle),
  [bids and matching](https://docs.vorq.co/docs/python/concepts/bids-and-matching),
  [encryption and trust](https://docs.vorq.co/docs/python/concepts/encryption)
- Reference: [Client](https://docs.vorq.co/docs/python/reference/client),
  [errors](https://docs.vorq.co/docs/python/reference/errors),
  [environment variables](https://docs.vorq.co/docs/python/reference/environment-variables)

## Contributing

```bash
git clone https://github.com/vorq-ai/vorq-client-sdk-python.git
cd vorq-client-sdk-python
python -m venv .venv && source .venv/bin/activate
pip install -e . --group dev                  # pip >= 25.1; the dev group is not an extra
python scripts/gen_test_keys.py > .env.test   # throwaway keys for the unit tests
pytest
```

The unit tests run against an in-process mock transport and need no network.

## License

[Apache-2.0](https://github.com/vorq-ai/vorq-client-sdk-python/blob/main/LICENSE).

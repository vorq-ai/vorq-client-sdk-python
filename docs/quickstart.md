---
title: Quickstart
description: Install the vorq package, set your wallet key and run your first text job.
---

> **VORQ runs on the Base Sepolia testnet.** Jobs are paid in test USDC, which is free. Fund
> your wallet before step 3: open [faucet.circle.com](https://faucet.circle.com), pick **USDC**
> and **Base Sepolia**, paste your wallet address and send. No ETH is needed: the coordinator
> pays the gas.

This page takes you from an empty environment to a finished text job.

## 1. Install

```bash
pip install vorq
```

`vorq` requires Python 3.11 or newer.

## 2. Set your wallet key

```bash
export VORQ_WALLET_KEY=0x<your wallet private key>
```

Use a **dedicated wallet funded with your inference budget, never a main wallet's key**. It is
the only key you configure. On your machine, the client uses it to:

- sign the session handshake, then mint and rotate the session token;
- sign every order, payment authorization and cancel;
- derive the key your results come back sealed to.

## 3. Submit a job and read the result

Save this as `hello.py`:

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
        print("job:", handle.id)

        result = await handle.result()  # polls until the job settles
        print(result.text)
        print(result.usage, "cost:", result.cost)


asyncio.run(main())
```

Run it:

```bash
python hello.py
```

The script prints the job id immediately, then the generated text once a provider has settled
the job. `result.cost` is what the job cost in USD, as a decimal string, before the
protocol and gas fees.

## What just happened

- `vorq.Client()` talks to the VORQ coordinator at `https://api.vorq.co` and signs with
  `VORQ_WALLET_KEY`.
- `model` must be an id from `await client.models.list()`.
- `submit` asks the coordinator for the market in the 24-hour window (`sla="batch"`) and signs
  the first provider's own ask. To cap what you pay, pass `max_rate_in` and `max_rate_out`; see
  [Bids and matching](./concepts/bids-and-matching.md).
- `handle.result()` polls until the job settles, for up to the job's SLA window. A `batch`
  job usually takes minutes to a few hours; 24 hours is the maximum.

## Open the cabinet with MetaMask

The cabinet at [vorq.co/app](https://vorq.co/app) shows your jobs, results and spending. To use
it with the wallet from step 2:

1. **Add Base Sepolia.** In MetaMask, open the network selector, then **Add network → Add a
   network manually**, and fill in:
   - Network name: `Base Sepolia`
   - RPC URL: `https://sepolia.base.org`
   - Chain ID: `84532`
   - Currency symbol: `ETH`
   - Block explorer: `https://sepolia.basescan.org`

   Save, then switch to Base Sepolia.
2. **Import your wallet.** Open the account menu, choose **Add account → Import account** and
   paste your `VORQ_WALLET_KEY`.
3. **Add the USDC token.** Open **Tokens → Import tokens → Custom token** and enter
   `0x036CbD53842c5426634e7929541eC2318f3dCF7e`. The symbol (`USDC`) and decimals (`6`) fill in
   by themselves. Select **Next**, then **Import**.
4. **Sign in.** Open [vorq.co/app](https://vorq.co/app), select **Connect wallet** and sign the
   message. It moves no funds and costs no gas. Your job is under **Jobs**; opening a result asks
   for one more signature, which derives the same result key as the client.

## Next steps

- Save `handle.id` before you wait, so another process can pick the job up:
  [Persist and resume jobs](./guides/persist-and-resume-jobs.md).
- Generate images and video: [Generate images and video](./guides/generate-images-and-video.md).
- Send many requests at once: [Submit a batch](./guides/submit-a-batch.md).
- Use the synchronous `openai` client instead: [Use the OpenAI client](./guides/use-the-openai-client.md).
- Every parameter of `submit`: [Client reference](./reference/client.md#submit).

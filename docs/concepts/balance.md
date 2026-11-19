---
title: Balance and deposits
description: How jobs are paid from a prepaid USDC balance, how deposits and withdrawals work, and why the client never needs ETH.
---

Jobs are paid from your wallet's **prepaid balance**, held in USDC by the job registry
contract. You never send a transaction yourself: every deposit and withdrawal is a signature
the coordinator relays, so the wallet holds USDC on Base and never needs ETH.

## How a job is paid

1. A submission's quote states the job's `amount` (cap + protocol fee + gas fee, see
   [What you pay](./bids-and-matching.md#what-you-pay)) and your `available` balance.
2. When a provider claims the order, `amount` is locked from your balance.
3. At settlement the job is charged what it used, and the rest returns to your balance. A job
   that fails or is reclaimed returns everything but the gas fee.

An order that is never claimed locks nothing and costs nothing. Until it is claimed it counts
as `reserved`, so `available` is your balance less what your open orders will lock.

## Deposits

A deposit is one EIP-3009 `ReceiveWithAuthorization` for USDC, payable only to the job
registry, signed on your machine. The coordinator relays it and a flat `deposit_fee` is taken
from it to cover that relay; the rest is credited to your balance.

By default the client deposits for you. When a submission's quote shows the balance short, it
signs one deposit for exactly the shortfall plus the fee, then posts the job. A batch checks its
whole total against your balance and deposits the shortfall once, before the upload.

To fund the balance yourself instead:

```python
async with vorq.Client(auto_deposit=False) as client:
    await client.deposit("5")          # credits $5; signs $5 + deposit_fee
    print(await client.balance())      # {"balance": "5", "available": "5", ...}
    handle = await client.submit(model="moonshotai/kimi-k3.1-pro:mxfp4", input="Hello")
```

With `auto_deposit=False`, a submission the balance doesn't cover raises
[`InsufficientBalance`](../reference/errors.md) before anything is posted, carrying `balance`,
`required` and `shortfall` as USD decimal strings.

## Withdrawals

`await client.withdraw("2.5")` moves USDC from your available balance back to your wallet at any
time. It signs `Withdraw(amount, issuedAt)` and the coordinator relays it. A claimed job's
amount is already locked, so a withdrawal can never leave a job unfunded; asking for more than
`available` is refused.

See [Client](../reference/client.md#balance) for the method reference.

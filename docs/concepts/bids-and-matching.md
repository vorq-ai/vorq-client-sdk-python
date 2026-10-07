---
title: Bids and matching
description: How you cap what an order pays, how the coordinator matches it to a provider, and what happens when no provider is within your ceilings.
---

A VORQ order names the most it will pay. When a provider asks at or under that, the order pays
the provider's ask; when none does, it rests as a limit order until one accepts it.

## Cap what you pay

The simplest order names one ceiling and no provider:

```python
handle = await client.submit(
    model="moonshotai/kimi-k3",
    input="Summarize the plot of Hamlet in three bullet points.",
    max_rate_in="0.6",      # never more than $0.60 per 1M input tokens
)
```

`max_rate_in` protects you from being overcharged: the order never signs an input rate above
it, whatever providers are asking when it is posted, and when a provider asks less you pay
that. It caps the input side only; the output side pays the matched provider's ask unless you
name `max_rate_out` too. This matters most in a [batch](../guides/submit-a-batch.md), where
every line is signed and paid for in one call.

Set it too low and no provider matches: the order [rests](#resting-orders) and may expire
without being served.

## Ceilings are USD per 1M units

`max_rate_in` and `max_rate_out` are ceilings in **USD per 1,000,000 units of work**. A unit of
work is a token for text, a pixel for images and a pixel-second for video
(see [Units and cost](./units-and-cost.md)).

- Pass a ceiling as a decimal string (`"0.05"`) or a `Decimal`. An `int` or `float` raises
  `ValidationError`.
- A ceiling may have at most as many fraction digits as the payment token has decimals (6 for
  USDC). A finer one is refused, not rounded.
- Each ceiling is optional. A side you leave out has no ceiling: you pay the provider's ask on
  that side, whatever it is. Most text jobs cost more on the output side, so name
  `max_rate_out` when you want to bound it.
- The order signs each rate as the token's atomic integer, `rate × 10^decimals`: `"0.05"` is
  `50000` at 6 decimals.

## How an order is matched

`submit()` talks to the coordinator in a short exchange:

1. **Probe.** The client asks for the market: the job's model, window and units, and your
   ceilings, with no payload and no signature. The coordinator answers `402` with the
   **candidates**: every live provider with a free slot whose ask is at or under your ceilings,
   ranked by what this job would cost at their ask, and among equal prices the provider picked
   least recently first.
2. **Seal and sign.** The client seals the payload to the first candidate, names it as the
   order's designated provider, signs the order at **that provider's ask** and asks for the
   order's quote. You never pay more than the ask, and never more than a ceiling you named.
3. **Pay and post.** The client signs a payment authorization for the quoted amount and posts
   the order, the authorization and the sealed payload together. The payload crosses the wire
   once.

With `submit(..., provider=N)` the probe is pinned to provider `N`, so the only candidate is
that provider's ask.

## Resting orders

When the probe names no candidate, no live provider is within your ceilings, and the order is
posted to **rest** until a provider accepts it or it expires. An order signs both rates, so it
rests at:

| Ceilings named | Rates the order rests at |
| --- | --- |
| both | your two ceilings |
| one | your ceiling on that side, and the **market rate** on the other: the rate of the cheapest live ask for this job |
| none | nothing to rest at: `submit` raises `ValidationError` before anything is signed |

With one ceiling named and no live ask for the model in the window, there is no market rate to
take, and `submit` raises `ValidationError` as well.

A resting order is paid at the rates it signed, by whichever provider claims it:

- Without `provider=N` it is an **open order**. The payload's key material is sealed to the
  coordinator's escrow key instead of a provider, and no provider is designated. The escrow
  hands it to the provider that claims the order.
- The client seals to the escrow key only after verifying it, which needs a
  `verifier=vorq.Verifier(base_url)` on the client. If the key doesn't verify, or there is no
  verifier, `submit` raises `EscrowKeyUnverified` and **nothing is posted**. The client never
  falls back to a provider on its own; re-targeting with `provider=N` or higher ceilings is
  your decision.
- With `provider=N` it rests sealed to that provider's registered key and only that provider
  can claim it.
- An order that expires unclaimed is refunded less the gas fee.

The trade-off is trust scope: a matched order is readable only by the provider it was sealed
to, while an open order is also readable by the coordinator's escrow.

## What you pay

The quote is:

```
cap    = (units_in × rate_in + units_out × rate_out) / 1,000,000, rounded up to the token's
         smallest unit, and at least one of it
amount = cap + protocol fee + gas fee
```

`cap` is the most the job can charge for the work itself, fixed by the terms you signed. The
protocol fee is a share of the cap, and the gas fee covers the coordinator relaying your order.
The quote states every amount as a USD decimal string.

The client derives every member of the payment authorization itself; the quote supplies only
the amount. A quote whose authorization disagrees with the client's own derivation is refused
before anything is signed. If the gas fee moves while you sign, the coordinator answers with a
new quote and the client re-signs only the payment, up to three attempts, without re-sealing or
re-uploading the payload.

The payment authorization is single-use: its nonce is the job id, and only the job registry can
execute it.

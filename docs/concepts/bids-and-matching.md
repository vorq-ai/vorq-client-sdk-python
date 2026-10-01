---
title: Bids and matching
description: How your bid is expressed, how the coordinator matches it to a provider, and what happens when nothing clears it.
---

A VORQ order is a limit order. You sign the price you are willing to pay, and a provider whose
published ask is at or below it can claim the job.

## Rates are USD per 1M units

`rate_in` and `rate_out` are prices in **USD per 1,000,000 units of work**. A unit of work is a
token for text, a pixel for images and a pixel-second for video
(see [Units and cost](./units-and-cost.md)).

- Pass a rate as a decimal string (`"0.05"`) or a `Decimal`. An `int` or `float` raises
  `ValidationError`.
- A rate may have at most as many fraction digits as the payment token has decimals (6 for
  USDC). A finer one is refused, not rounded.
- Leave out **both** rates and the order takes the market (see [No bid named](#no-bid-named)).
  Leave out only one and that side is signed as **zero**, which is a real bid: no provider asking
  more will claim it, and you find out when it expires rather than from an error.
- The order signs each rate as the token's atomic integer, `rate × 10^decimals`: `"0.05"` is
  `50000` at 6 decimals.

## How an order is matched

`submit()` talks to the coordinator in a short challenge exchange:

1. **Probe.** For an order with no pinned provider, the client first sends the signed terms
   alone, with no payload. The coordinator answers `402` with the **clearing candidates**: live
   providers whose ask clears your rates, ranked by the coordinator.
2. **Seal and sign.** The client seals the payload to the first candidate, names it as the
   order's designated provider, signs the real order and asks for that order's quote.
3. **Pay and post.** The client signs a payment authorization for the quoted amount and posts
   the order, the authorization and the sealed payload together. The payload crosses the wire
   once.

With rates and `submit(..., provider=N)` the probe is skipped: the payload is sealed to provider
`N`'s registered key.

## No bid named

With neither `rate_in` nor `rate_out`, the client first asks the coordinator for the market: an
unsigned probe with no rates, so it costs no signature. The answer lists every live provider with
a free slot and an ask in the order's window, ranked by what this job would cost at their ask,
and among equal prices the provider picked least recently first. The client bids the first
candidate's own ask and pins it, so providers at the same price take turns.

With `provider=N` as well, the probe is pinned too, and the bid is that provider's ask. When no
provider is live for the model in the window, `submit` raises `ValidationError` before anything
is signed; pass rates to post a bid that rests instead.

## Open bids

When you name rates and the challenge names no candidates, nothing on the book clears your bid,
and the order is posted as an **open bid** that rests until a provider accepts it or it expires:

- The payload's key material is sealed to the coordinator's escrow key instead of a provider,
  and no provider is designated. The escrow hands it to the provider that claims the order.
- The client seals to the escrow key only after verifying it, which needs a
  `verifier=vorq.Verifier(base_url)` on the client. If the key doesn't verify, or there is no
  verifier, `submit` raises `EscrowKeyUnverified` and **nothing is posted**. The client never
  falls back to a provider on its own; re-targeting with `provider=N` or a higher bid is your
  decision.

The trade-off is trust scope: a matched order is readable only by the provider it was sealed
to, while an open bid is also readable by the coordinator's escrow.

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

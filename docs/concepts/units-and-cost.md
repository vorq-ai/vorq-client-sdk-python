---
title: Units and cost
description: Why the client declares an order's units, how it counts them for text and media, and how a result's cost is computed.
---

The coordinator can't count tokens or pixels inside a sealed payload, so the client declares
two counts on every order, `units_in` and `units_out`, and signs them. Both are ceilings: they fix
the most the job can charge, and whatever settlement doesn't use is refunded. The provider checks
them after opening the payload.

## One shape decides both counts

The client reads the request once and classifies it:

- **text** when it names an output-token ceiling (`max_output_tokens`, `max_tokens` or
  `max_completion_tokens`);
- otherwise **video** when it names `duration` or `duration_secs`;
- otherwise **image** when it names `num_images`, `width`, `resolution` or a reference asset;
- otherwise **text**.

Both counts follow from that one decision, so a request is never priced as text on one side and
as pixels on the other. A string `input` is always text.

| Shape | `units_in` | `units_out` |
| --- | --- | --- |
| text | the input's canonical JSON length in bytes | the output-token ceiling, or 4096 |
| image | reference pixel-seconds, or 0 | frame pixels × `num_images` |
| video | reference pixel-seconds, or 0 | frame pixels × seconds |

For text, `units_in` is a ceiling on the prompt, not a count. Text runs about four bytes a token
and the JSON wrapper adds more, so the byte length sits above the model's own prompt count, chat
template included, for anything longer than a few words. Settlement bills the model's own prompt
count, never more than `units_in`, and refunds the rest. For media, the
prompt's length doesn't count: the input side is the reference assets you attach, and that
count is what settles. Frame sizes, defaults and
asset rules are in [Units and media inputs](../reference/units.md).

`submit(..., units_out=N)` overrides the output count, zero included. An embeddings job uses
`units_out=0`, since it has no output side to buy.

## Counts are caps

`units_in` is the most input the order pays for, and `units_out` the most output. A text model
may stop early. For media, the
provider settles what it actually delivered, never more than the cap: `aspect_ratio: "adaptive"`
and `duration: "auto"` are priced at the largest frame and longest clip they allow.

**Reasoning is billed as output.** On reasoning models, thinking tokens count toward output
tokens and spend the same `units_out` your output cap declares. Bound them with
`reasoning_max_tokens`.

## Cost

The worst case is known at submit time. See [What you pay](./bids-and-matching.md#what-you-pay).

After settlement, each result carries `.cost`: a USD decimal string, computed exactly by the
client from the counts and rates the job settled on, as the job row reports them. It excludes the
protocol and gas fees.
`.gas_fee` is the flat gas fee the job was posted under, as a `Decimal` in USD. A claimed job
pays it whether it settles, fails or is reclaimed; a job cancelled or expired before any
provider claimed it pays none.
`.fee` is the protocol fee settlement took on top of `.cost`, as a `Decimal` in USD, read from
the chain's settlement: a share of what the job charged, not of the cap the quote reserved.

Every result type uses one formula:

```
cost = ((prompt_tok − cached_tok) × rate_in
        + cached_tok × rate_in_cached
        + completion_tok × rate_out) / 1,000,000
```

- `prompt_tok` is the model's own prompt count, at most `units_in`. For media it is the
  reference units the order declared.
- `cached_tok` is the part of the prompt the model served from its cache, billed at
  `rate_in_cached`, which is never above `rate_in`. It is 0 when the model reports none, and
  always 0 for media.
- `completion_tok` is the delivered output: tokens for text, pixels or pixel-seconds for media,
  at most `units_out`. An embedding settles at 0.

On chain the charge is that sum rounded up to the token's smallest unit, and at least one unit.
The unused part of the cap is refunded.

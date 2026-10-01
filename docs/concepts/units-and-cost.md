---
title: Units and cost
description: Why the client declares an order's units, how it counts them for text and media, and how a result's cost is computed.
---

The coordinator can't count tokens or pixels inside a sealed payload, so the client declares
two counts on every order, `units_in` and `units_out`, and signs them. They fix the most the job
can charge. The provider checks them after opening the payload.

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
| text | the input's canonical JSON length in bytes ÷ 4 (at least 1) | the output-token ceiling, or 4096 |
| image | reference pixel-seconds, or 0 | frame pixels × `num_images` |
| video | reference pixel-seconds, or 0 | frame pixels × seconds |

For text, `units_in` is an estimate from the prompt's size. For media, the prompt's length
doesn't count: the input side is the reference assets you attach. Frame sizes, defaults and
asset rules are in [Units and media inputs](../reference/units.md).

`submit(..., units_out=N)` overrides the output count, zero included. An embeddings job uses
`units_out=0`, since it has no output side to buy.

## Output counts are caps

`units_out` is the most output the order pays for. A text model may stop early. For media, the
provider settles what it actually delivered, never more than the cap: `aspect_ratio: "adaptive"`
and `duration: "auto"` are priced at the largest frame and longest clip they allow.

**Reasoning is billed as output.** On reasoning models, thinking tokens count toward output
tokens and spend the same `units_out` your output cap declares. Bound them with
`reasoning_max_tokens`.

## Cost

The worst case is known at submit time. See [What you pay](./bids-and-matching.md#what-you-pay).

After settlement, each result carries `.cost`: a USD decimal string, computed exactly by the
client from the signed rates and the result's own counts. It excludes the protocol and gas fees.
`.gas_fee` is the flat gas fee the job was posted under, as a `Decimal` in USD. A claimed job
pays it whether it settles, fails or is reclaimed; a job cancelled or expired before any
provider claimed it pays none.
`.fee` is the protocol fee settlement took on top of `.cost`, as a `Decimal` in USD, read from
the chain's settlement: a share of what the job charged, not of the cap the quote reserved.

| Result | `.cost` |
| --- | --- |
| `TextResult` | `(input_tokens × rate_in + output_tokens × rate_out) / 1,000,000` |
| `MediaResult` | `delivered units × rate_out / 1,000,000` |
| `EmbeddingResult` | `prompt_tokens × rate_in / 1,000,000` |

For media, delivered units are the output pixels (images) or pixel-seconds (video) of the
returned frames, or the provider's stated settled units when those are lower.

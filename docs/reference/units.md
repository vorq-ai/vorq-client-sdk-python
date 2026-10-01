---
title: Units and media inputs
description: The exact rules the client uses to count an order's units, with frame sizes, defaults and reference asset limits.
---

The client declares `units_in` and `units_out` on every order from the input's shape. Why, and
how they turn into cost, is in [Units and cost](../concepts/units-and-cost.md). All violations
below raise `ValidationError` before anything is sealed.

## Shape

| Shape | The input names |
| --- | --- |
| text | `max_output_tokens`, `max_tokens` or `max_completion_tokens` (checked first) |
| video | otherwise `duration` or `duration_secs` |
| image | otherwise `num_images`, `width`, `resolution` or any reference asset |
| text | otherwise |

## Counts

| Shape | `units_in` | `units_out` |
| --- | --- | --- |
| text | `max(1, len(canonical JSON of the input) // 4)` | the first ceiling present, in the order above, or `4096` |
| image | [reference pixel-seconds](#reference-assets), or `0` | frame pixels × `num_images` (default 1) |
| video | reference pixel-seconds, or `0` | frame pixels × duration seconds |

`submit(..., units_out=N)` replaces the output count; it doesn't change `units_in`.

## Frame pixels

- **Explicit `width` / `height`** always win. Each defaults to 1024 when only the other is given.
- Otherwise a **`resolution`** tier (`480p`, `720p`, `1080p`, `4k`), resolved against
  `aspect_ratio`, from the table below.
- Otherwise `1024 × 1024`.

| `aspect_ratio` | `480p` | `720p` | `1080p` | `4k` |
| --- | --- | --- | --- | --- |
| `21:9` | 976×416 | 1472×632 | 2200×944 | 4400×1888 |
| `16:9` | 854×480 | 1280×720 | 1920×1080 | 3840×2160 |
| `4:3` | 736×552 | 1112×832 | 1664×1248 | 3328×2496 |
| `1:1` | 640×640 | 960×960 | 1440×1440 | 2880×2880 |
| `3:4` | 552×736 | 832×1112 | 1248×1664 | 2496×3328 |
| `9:16` | 480×854 | 720×1280 | 1080×1920 | 2160×3840 |

A tier is a pixel budget: every ratio in a tier has about the same area.

- `aspect_ratio: "auto"`, or no `aspect_ratio`, picks the row closest to the first
  pixel-bearing reference's declared shape, or `16:9` when there is no reference.
- `aspect_ratio: "adaptive"` is priced at the tier's largest frame.
- Any other `aspect_ratio`, or an unknown `resolution`, is refused.

## Duration

- `duration_secs` wins over `duration`.
- A whole number, or a string of ASCII digits (`5` or `"5"`). A fractional number is cut to
  whole seconds. Anything else, such as `"7.5"` or `"+5"`, is refused.
- Missing, empty or not positive: 5 seconds.
- `"auto"`: priced at 15 seconds, a cap the delivered clip settles under.

## Reference assets

Reference assets travel as base64 inside the sealed payload, never as URLs.

| Key | Holds | Kind |
| --- | --- | --- |
| `image`, `end_image` | one asset | still |
| `video` | one asset | clip |
| `reference_images` | a list, up to 9 | still |
| `reference_videos` | a list, up to 3 | clip |
| `reference_audios` | a list, up to 3 | audio |

A singular key holding something other than an object, or a list key holding something other
than a list, is not treated as a reference.

Every asset:

- needs `b64` (non-empty) and `media_type`;
- if a still or a clip, needs integer `width` and `height` of at least 1;
- if a clip, needs `duration_secs`, a positive integer rounded up to whole seconds.

`units_in` is the sum over stills and clips of `width × height × seconds`, where a still counts
as one second (or its own `duration_secs` if given). Audio counts zero.

| Limit | Value |
| --- | --- |
| Pixel-bearing assets per request | 12 |
| Pixels per asset | 8,294,400 (3840×2160) |
| Seconds per clip | 60 |
| Decoded bytes per audio asset | 15,728,640 (15 MiB) |

The provider decodes each asset after opening the payload and fails the job if it is larger or
longer than declared.

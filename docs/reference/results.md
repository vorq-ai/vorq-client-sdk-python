---
title: Results
description: Reference for TextResult, MediaResult and EmbeddingResult, the values a settled job returns.
---

`handle.result()` and the batch methods return one of three dataclasses, chosen by the shape of
the opened result: an embeddings response becomes an `EmbeddingResult`, a result with `images`
or `video` a `MediaResult`, and anything else a `TextResult`.

## Common attributes

| Attribute | Type | Meaning |
| --- | --- | --- |
| `.rates` | `tuple[Decimal \| None, Decimal \| None]` | The order's signed `(rate_in, rate_out)`, in USD per 1M units. |
| `.cost` | `str` | USD decimal string, computed exactly from the signed rates and the result's counts. Excludes the protocol and gas fees. See [Units and cost](../concepts/units-and-cost.md#cost). |
| `.gas_fee` | `Decimal` | The flat gas fee the job was posted under, in USD, paid on top of `.cost`. |
| `.fee` | `Decimal` | The protocol fee settlement took, in USD, paid on top of `.cost`. |
| `.provider` | `int \| str \| None` | Registry id of the provider that settled the job. |
| `.job_id` | `str \| None` | The job that produced the result. |
| `.custom_id` | `str \| None` | Your `custom_id`, read back from the sealed result. `None` when you set none. |
| `.raw` | `dict` | The opened result object. |

## `TextResult`

| Attribute | Type | Meaning |
| --- | --- | --- |
| `.text` | `str` | The generated text, joined from the output items. |
| `.output` | `list` | The raw output items: Responses output items, or `choices` for a chat-completion result. |
| `.usage` | `dict` | `input_tokens`, `output_tokens` and `total_tokens`. |

## `MediaResult`

The frames travel inside the sealed result as base64, so these methods make no network call.

| Member | Type | Meaning |
| --- | --- | --- |
| `.frames` | `list[dict]` | The delivered frames in order: one per image, or one entry for a video. Each has `b64`, `content_type`, `width` and `height`, plus `duration_secs` for video. |
| `.seed` | `int \| None` | The seed the model used, when reported. |
| `.bytes()` | `list[bytes]` | The frames decoded, in order. A frame with missing or invalid base64 raises `ResultIntegrityError`. |
| `.download(dir)` | `list[Path]` | Creates `dir` if needed and writes each frame as `0`, `1`, … with a suffix from its `content_type` (`.png`, `.jpg`, `.webp`, `.mp4`, `.webm`, otherwise `.bin`). Returns the paths. |

## `EmbeddingResult`

| Member | Type | Meaning |
| --- | --- | --- |
| `.embeddings` | `list[dict]` | The response's `data` array, unchanged. |
| `.model` | `str \| None` | The model named in the response. |
| `.prompt_tokens` | `int` | Input tokens. |
| `.bytes()` | `list[bytes]` | The vectors decoded from base64. Raises `VorqError` when the response used `encoding_format="float"`; read `.embeddings` instead. |

## `JobError`

A batch line that never delivered. See [Batches](./batches.md#joberror).

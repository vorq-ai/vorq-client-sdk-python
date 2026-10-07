---
title: Generate images and video
description: Submit image and video jobs, attach reference assets and save the returned frames.
---

Media jobs use the same `client.submit()` call as text. The difference is the input: pass a
`dict` in the model's own input shape, and the client reads that shape to decide what the order
buys. Pick a model id from `await client.models.list()`.

## Generate an image

```python
handle = await client.submit(
    model=IMAGE_MODEL,                 # an image model id from client.models.list()
    input={
        "prompt": "a lighthouse in fog, oil painting",
        "width": 1024,
        "height": 768,
        "num_images": 1,
        "seed": 42,
    },
    sla="batch",
    max_rate_out="0.05",                   # USD per 1,000,000 output pixels
)
result = await handle.result()        # a MediaResult
paths = result.download("out/")       # writes out/0.png, ...
print(paths, result.seed, result.cost)
```

An image order buys `width × height × num_images` output pixels, so `max_rate_out` is a ceiling per
million pixels. Width and height each default to 1024 when you leave them out.

## Generate a video

A request becomes a video order when it names `duration` (or `duration_secs`):

```python
handle = await client.submit(
    model=VIDEO_MODEL,
    input={
        "prompt": "a slow pan across a mountain valley at dawn",
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "duration": 5,
    },
    sla="batch",
    max_rate_out="0.002",                  # USD per 1,000,000 pixel-seconds
)
result = await handle.result()
frame = result.frames[0]              # a video comes back as a single frame entry
print(frame["content_type"], frame["duration_secs"])
result.download("out/")
```

A video order buys `frame pixels × seconds`. Here that is `1280 × 720 × 5`. The resolution
tiers, aspect ratios and defaults are listed in [Units and media inputs](../reference/units.md).

## Attach reference assets

Reference images, clips and audio travel as base64 bytes inside the sealed payload, never as
URLs. Each asset declares its own media type and dimensions:

```python
import base64
from pathlib import Path

def b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()

handle = await client.submit(
    model=VIDEO_MODEL,
    input={
        "prompt": "the flower opens",
        "image": {"b64": b64("first.png"), "media_type": "image/png", "width": 1280, "height": 720},
        "end_image": {"b64": b64("last.png"), "media_type": "image/png", "width": 1280, "height": 720},
        "resolution": "720p",
        "duration": 5,
    },
    max_rate_in="0.001",                   # USD per 1,000,000 reference pixel-seconds
    max_rate_out="0.002",
)
```

References are the input side of a media order: each still counts its pixels once, each clip its
pixels times its seconds. A clip must state `duration_secs`. The provider decodes every asset
after opening the payload and fails the job if an asset is larger or longer than declared.
Asset keys and limits are in [Units and media inputs](../reference/units.md#reference-assets).

## Read the frames

The frames are inside the sealed result, so reading them makes no network call:

- `result.frames`: one dict per image (or one for a video) with `b64`, `content_type`, `width`
  and `height`, plus `duration_secs` for video.
- `result.bytes()`: the decoded frames, in order.
- `result.download(dir)`: writes each frame as `0`, `1`, … with the file suffix its
  `content_type` implies, and returns the paths.

See [`MediaResult`](../reference/results.md#mediaresult).

"""Reference-conditioned media: what a request is asking for, in billable units.

A media request names its shape the way the rest of the industry names it — a
resolution tier and a whole number of seconds — while the chain bills in pixels.
This module is the translation, and it is shared rather than invented here: every
number comes out of ``media-units-v1.json``, which the provider daemon reads from
its own copy of the same file. The client signs the units; the provider prices the
work against them; a disagreement is a provider paid for something it did not do.

**Reference assets ride as bytes, never as a URL.** The whole input is sealed into
a container only the claiming provider can open, so a link would hand the
reference to anyone who fetched the job and defeat the point. Base64 in the input
needs no new transport either: the container inlines under ``INLINE_MAX_BYTES``
and uploads by cid above it, which is machinery that already exists.

**A reference declares its own dimensions and this SDK believes it.** That is
deliberate — it keeps an image decoder out of both clients, and out of the parity
surface between them, which would be far harder to keep honest than a table of
twelve frame sizes. The declaration is a *claim*: the provider decodes the
reference after decrypting it and refuses a job whose reference is larger than
the units paid for it.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import _media_units as mu
from .errors import ValidationError


#: A duration written as a string: ASCII digits and nothing else.
_WHOLE_SECONDS = re.compile(r"[0-9]{1,9}")


def _invalid(message: str) -> ValidationError:
    return ValidationError(message, type="invalid_request_error")


def assets(model_input: dict) -> list[tuple[str, str, Any]]:
    """Every reference this request carries, as ``(label, kind, asset)`` in the
    order they are counted: the singular keys, then each list key's elements.

    ``kind`` is ``"still"``, ``"clip"`` or ``"audio"`` and comes from the *key*,
    never from the asset's own ``media_type`` — the key is what the caller meant,
    and the type is a claim the provider checks.

    A singular key holding anything other than an object is not a reference and is
    left alone: this SDK forwards a payload verbatim, and a model that happens to
    take an ``image`` string for some other purpose must not have its request
    refused by an accounting helper. A list key holding anything other than a list
    is left alone for the same reason; its elements are not, because a list of
    references with a non-reference in it is a mistake and not another protocol.
    """
    found: list[tuple[str, str, Any]] = []
    for key in mu.REFERENCE_KEYS:
        if isinstance(model_input.get(key), dict):
            found.append((key, _kind(key), model_input[key]))
    for key in mu.REFERENCE_LIST_KEYS:
        if isinstance(model_input.get(key), list):
            found.extend((f"{key}[{i}]", _kind(key), item)
                         for i, item in enumerate(model_input[key]))
    return found


def _kind(key: str) -> str:
    return "clip" if key in mu.CLIP_KEYS else "audio" if key in mu.AUDIO_KEYS else "still"


def references(model_input: dict) -> list[dict]:
    """The pixel-bearing references, in counting order — what ``auto`` measures."""
    return [asset for _, kind, asset in assets(model_input)
            if kind != "audio" and isinstance(asset, dict)]


def _dimension(asset: dict, field: str, key: str) -> int:
    value = asset.get(field)
    # `bool` first: `True` is a perfectly good `1` to `int()`, and an order is not
    # the place to guess what a caller meant by a flag where a width belongs.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(
            f"{key}.{field} must be a positive integer — a reference states its own "
            f"dimensions, and they are what the input side of the order is priced on"
        )
    return value


def reference_units(model_input: dict) -> int:
    """``units_in`` for a reference-conditioned request: total reference pixel-seconds.

    A still frame counts as its pixels for one second, so ``rate_in`` prices one
    input pixel-second whether the reference moves or not, and a start frame plus
    an end frame is simply the sum. Sound has no pixels and counts zero. A request
    carrying no reference declares ``0`` — there is no input quantity to buy, and
    the chain takes zero.

    The caps are applied here, before anything is sealed or signed. They are not
    politeness: ``units_in`` is a ``uint32`` and a few kilobytes of flat-colour PNG
    can *claim* a hundred thousand pixels a side, so without them an honest-looking
    request could overflow the field the order is signed over.
    """
    for key, most in mu.REFERENCE_LIST_KEYS.items():
        held = model_input.get(key)
        if isinstance(held, list) and len(held) > most:
            raise _invalid(f"{key} holds at most {most} references, and this one holds {len(held)}")
    found = assets(model_input)
    pixel_bearing = sum(1 for _, kind, _ in found if kind != "audio")
    if pixel_bearing > mu.MAX_REFERENCE_ASSETS:
        raise _invalid(
            f"a request carries at most {mu.MAX_REFERENCE_ASSETS} reference assets "
            f"(reference_assets), and this one carries {pixel_bearing}"
        )

    total = 0
    for key, kind, asset in found:
        if not isinstance(asset, dict):
            raise _invalid(f"{key} must be a reference object")
        if not isinstance(asset.get("b64"), str) or not asset["b64"]:
            raise _invalid(f"{key}.b64 must carry the reference's bytes, base64-encoded")
        if not isinstance(asset.get("media_type"), str) or not asset["media_type"].strip():
            raise _invalid(f"{key}.media_type must name the reference's media type")
        if kind == "audio":
            if len(asset["b64"]) * 3 // 4 > mu.MAX_REFERENCE_AUDIO_BYTES:
                raise _invalid(
                    f"{key} is larger than the {mu.MAX_REFERENCE_AUDIO_BYTES} bytes a "
                    f"reference sound may be (reference_audio_bytes)"
                )
            continue

        pixels = _dimension(asset, "width", key) * _dimension(asset, "height", key)
        if pixels > mu.MAX_REFERENCE_PIXELS:
            raise _invalid(
                f"{key} declares {pixels} pixels; the most a reference may carry is "
                f"{mu.MAX_REFERENCE_PIXELS} (reference_pixels)"
            )

        seconds = asset.get("duration_secs")
        if seconds is None:
            if kind == "clip":
                raise _invalid(
                    f"{key}.duration_secs must say how long the reference clip runs, in "
                    "whole seconds rounded up — the provider reads the clip's real length "
                    "and hands back a job whose clip outruns it"
                )
            seconds = 1                      # a still is one pixel-second per pixel
        elif isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            raise _invalid(f"{key}.duration_secs must be a positive integer of seconds")
        elif seconds > mu.MAX_REFERENCE_DURATION_S:
            raise _invalid(
                f"{key} declares {seconds} seconds; the longest reference clip is "
                f"{mu.MAX_REFERENCE_DURATION_S} (reference_duration_s)"
            )
        total += pixels * seconds
    return total


def resolve_aspect_ratio(declared: Any, assets: list[dict]) -> str:
    """Which row of the frame table this request is shaped by.

    An explicit ratio is taken as written and checked against the table — a
    spelling the table does not name is refused rather than silently defaulted,
    because defaulting would quote pixels the caller never asked for.

    ``auto``, and an absent ratio, are resolved from the **first reference's
    declared dimensions**, which is the one measurement both the client and the
    provider hold. The comparison is between logarithms of the ratios, which is
    what makes it scale-symmetric: 1280×720 and 3840×2160 are one shape and must
    choose one row, and an absolute comparison would let the larger reference sit
    further from 16:9 than a square one sits from 1:1.
    """
    if declared == mu.ADAPTIVE_ASPECT:
        return mu.ADAPTIVE_ASPECT
    if declared is not None and declared != "auto":
        if not isinstance(declared, str) or declared not in mu.FRAMES:
            raise _invalid(
                f"aspect_ratio {declared!r} is not one this network prices; "
                f"it is one of {', '.join(mu.AUTO_ORDER)}, 'auto' or '{mu.ADAPTIVE_ASPECT}'"
            )
        return declared
    if not assets:
        return mu.AUTO_FALLBACK
    first = assets[0]
    target = math.log(_dimension(first, "width", "reference")
                      / _dimension(first, "height", "reference"))
    # Ties break toward the earlier entry of `auto_order`, which the index in the
    # sort key is doing — `min` alone would be stable, but relying on that is
    # relying on a language's sort rather than on the table's own order.
    return min(
        mu.AUTO_ORDER,
        key=lambda a: (abs(target - math.log(mu.FRAMES[a]["1080p"][0]
                                             / mu.FRAMES[a]["1080p"][1])),
                       mu.AUTO_ORDER.index(a)),
    )


def frame_pixels(model_input: dict) -> int:
    """The pixels in one output frame.

    Explicit ``width``/``height`` win over a tier, always: a caller who named
    pixels gets those pixels, and ``resolution`` is a convenience rather than an
    override. Each dimension falls back independently, which is the behaviour the
    first media jobs shipped with and which their numbers still depend on.
    """
    if "width" in model_input or "height" in model_input:
        return (int(model_input.get("width") or mu.DEFAULT_DIM)
                * int(model_input.get("height") or mu.DEFAULT_DIM))
    tier = model_input.get("resolution")
    if tier is not None:
        if tier not in mu.RESOLUTIONS:
            raise _invalid(
                f"resolution {tier!r} is not one this network prices; "
                f"it is one of {', '.join(mu.RESOLUTIONS)}"
            )
        aspect = resolve_aspect_ratio(model_input.get("aspect_ratio"), references(model_input))
        if aspect == mu.ADAPTIVE_ASPECT:
            # The model keeps the reference's own shape, which no row names. The
            # tier's largest frame is the cap; the delivered clip settles under it.
            return max(w * h for w, h in (row[tier] for row in mu.FRAMES.values()))
        width, height = mu.FRAMES[aspect][tier]
        return width * height
    return mu.DEFAULT_DIM * mu.DEFAULT_DIM


def duration_secs(model_input: dict) -> int:
    """How many seconds of output this request buys.

    ``duration_secs`` is the canonical spelling and wins; ``duration`` is the one
    the prevailing interface uses and is accepted as a whole number or its decimal
    string, because that interface serializes it both ways and ``5`` and ``"5"``
    are the same request.
    """
    raw = model_input.get("duration_secs")
    if raw is None:                          # null is absent, as everywhere else
        raw = model_input.get("duration")
    if raw is None or raw == "":
        return mu.DEFAULT_DURATION_S
    if raw == mu.AUTO_DURATION:
        # The model chooses the length; the order signs the most it may choose.
        return mu.AUTO_DURATION_S
    # Narrower than `int()` on purpose. `int()` reads "+5", " 5 ", "1_0" and "٣";
    # the other client's parser reads "7.5" and "1e1" instead. The provider
    # re-derives this number, so a spelling two parsers disagree about is escrow
    # for seconds nobody will render.
    if isinstance(raw, str) and _WHOLE_SECONDS.fullmatch(raw):
        seconds = int(raw)
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        seconds = int(raw)
    else:
        raise _invalid(f"duration {raw!r} must be a whole number of seconds")
    return seconds if seconds > 0 else mu.DEFAULT_DURATION_S


def shape(model_input: dict) -> str:
    """``"text"``, ``"image"`` or ``"video"`` — **one** decision, driving both units.

    Deriving the two sides separately is how a request ends up priced as text on
    the output leg and as pixels on the input leg, which is a bill no party agrees
    on. So the shape is decided once, here, and both scalars follow from it.

    An output-token ceiling wins outright: a request naming one is token-metered
    whatever else it carries. Then a duration makes it video, and pixels, a tier
    or a reference make it an image. Nothing else is media.

    This is a guess, and it has to be: ``GET /v1/models`` serves no modality, so
    the request's own shape is the only signal a client has about what it is
    ordering. Asking for a clip therefore means saying how long it is.
    """
    if any(key in model_input for key in mu.OUTPUT_CEILING_KEYS):
        return "text"
    if "duration" in model_input or "duration_secs" in model_input:
        return "video"
    if (any(key in model_input for key in ("num_images", "width", "resolution"))
            or assets(model_input)):
        return "image"
    return "text"

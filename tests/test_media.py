"""Media unit declaration, against the table every party shares.

The `cases` array in `media-units-v1.json` is the parity guard: this suite, the
JS suite and the daemon's all assert their own derivation against the same
literal expectations, so a divergence between two implementations fails in each
of their own repos rather than surfacing on a live chain as a provider paid for
work it did not do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vorq import _media_units as mu
from vorq._client import _declare_units
from vorq._media import resolve_aspect_ratio
from vorq.errors import ValidationError

CASES = json.loads(
    (Path(mu.__file__).parent / "media-units-v1.json").read_text()
)["cases"]


def _ref(width, height, **extra):
    return {"b64": "AA==", "media_type": "image/png", "width": width, "height": height, **extra}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_shared_cases_derive_the_units_they_pin(case):
    units_in, units_out = _declare_units(case["input"], case.get("units_out_override"))
    assert (units_in, units_out) == (case["units_in"], case["units_out"]), case["why"]


def test_the_case_list_has_not_quietly_shrunk():
    """Losing a case is silent — the parametrize above just runs fewer tests. The
    shapes named here are the ones this round exists to price correctly."""
    names = {c["name"] for c in CASES}
    assert {"legacy-image-pixels", "tiered-video-string-duration", "reference-image-to-video",
            "reference-start-and-end-frame", "reference-clip", "plain-text-is-untouched",
            "an-explicit-zero-output-survives"} <= names


# --- aspect resolution ---------------------------------------------------------


@pytest.mark.parametrize("w,h,expected", [
    (1280, 720, "16:9"), (1920, 1080, "16:9"),      # one shape, two sizes
    (1080, 1920, "9:16"), (720, 1280, "9:16"),
    (640, 480, "4:3"), (900, 1200, "3:4"),
    (1000, 1000, "1:1"), (2560, 1080, "21:9"),
])
def test_auto_reads_the_references_shape(w, h, expected):
    assert resolve_aspect_ratio(None, [_ref(w, h)]) == expected
    assert resolve_aspect_ratio("auto", [_ref(w, h)]) == expected


def test_auto_is_scale_symmetric():
    """Comparing log-ratios, not ratios: without it a 4000x2250 reference sits
    further from 16:9 in absolute terms than a 100x100 one sits from 1:1, and the
    metric starts preferring whichever row happens to be numerically closest."""
    assert resolve_aspect_ratio("auto", [_ref(1280, 720)]) == \
           resolve_aspect_ratio("auto", [_ref(3840, 2160)])


def test_auto_falls_back_when_there_is_nothing_to_measure():
    assert resolve_aspect_ratio("auto", []) == mu.AUTO_FALLBACK == "16:9"


def test_an_explicit_ratio_is_never_second_guessed():
    assert resolve_aspect_ratio("1:1", [_ref(1280, 720)]) == "1:1"


def test_a_ratio_the_table_does_not_name_is_refused():
    with pytest.raises(ValidationError, match="aspect_ratio"):
        resolve_aspect_ratio("9:21", [])


# --- caps ----------------------------------------------------------------------


def test_a_reference_past_the_pixel_cap_is_refused_before_anything_is_sealed():
    """Client-side, at declare time. The caps exist so the convention cannot
    overflow the uint32 the order signs — and a caller learns that from a named
    error rather than from a container they already paid to upload."""
    side = 4000
    with pytest.raises(ValidationError, match="reference_pixels|too large"):
        _declare_units({"prompt": "x", "image": _ref(side, side), "duration": 5})


def test_more_references_than_the_cap_allows_are_refused():
    too_many = mu.MAX_REFERENCE_ASSETS - 2
    with pytest.raises(ValidationError, match="reference_assets"):
        _declare_units({"prompt": "x", "image": _ref(64, 64), "end_image": _ref(64, 64),
                        "video": _ref(64, 64, media_type="video/mp4", duration_secs=1),
                        "reference_images": [_ref(64, 64)] * 9,
                        "reference_videos": [_ref(64, 64, media_type="video/mp4", duration_secs=1)],
                        "duration": 5})
    assert too_many > 0


def test_a_reference_list_longer_than_its_own_cap_is_refused():
    with pytest.raises(ValidationError, match="reference_images"):
        _declare_units({"prompt": "x", "reference_images": [_ref(64, 64)] * 10, "duration": 5})


def test_a_listed_clip_must_say_how_long_it_runs_too():
    clip = {"b64": "AAAA", "media_type": "video/mp4", "width": 64, "height": 64}
    with pytest.raises(ValidationError, match=r"reference_videos\[0\].duration_secs"):
        _declare_units({"prompt": "x", "reference_videos": [clip], "duration": 5})


def test_a_list_of_references_with_a_non_reference_in_it_is_refused():
    with pytest.raises(ValidationError, match=r"reference_images\[1\]"):
        _declare_units({"prompt": "x", "reference_images": [_ref(64, 64), "http://x/y.png"],
                        "duration": 5})


def test_reference_sound_is_bounded_in_bytes_because_nothing_else_bounds_it():
    big = "A" * (mu.MAX_REFERENCE_AUDIO_BYTES * 4 // 3 + 8)
    with pytest.raises(ValidationError, match="reference_audio_bytes"):
        _declare_units({"prompt": "x", "reference_audios": [{"b64": big, "media_type": "audio/wav"}],
                        "resolution": "480p", "duration": 5})


def test_a_reference_clip_past_the_duration_cap_is_refused():
    with pytest.raises(ValidationError, match="duration"):
        _declare_units({"prompt": "x",
                        "video": _ref(64, 64, media_type="video/mp4",
                                      duration_secs=mu.MAX_REFERENCE_DURATION_S + 1),
                        "duration": 5})


@pytest.mark.parametrize("bad", [
    {"b64": "AA==", "media_type": "image/png", "height": 720},           # no width
    {"b64": "AA==", "media_type": "image/png", "width": 0, "height": 720},
    {"b64": "AA==", "media_type": "image/png", "width": -8, "height": 720},
    {"b64": "AA==", "media_type": "image/png", "width": "1280", "height": 720},
    {"media_type": "image/png", "width": 1280, "height": 720},           # no bytes
    {"b64": "AA==", "width": 1280, "height": 720},                       # no media type
])
def test_a_reference_that_does_not_declare_itself_is_refused(bad):
    """Declared dimensions are what let this SDK price a reference without
    decoding it — so an asset that omits or fudges them is refused here rather
    than priced as zero and settled as a surprise."""
    with pytest.raises(ValidationError):
        _declare_units({"prompt": "x", "image": bad, "duration": 5})


def test_a_reference_at_the_pixel_cap_exactly_is_allowed():
    units_in, _ = _declare_units({"prompt": "x", "image": _ref(3840, 2160), "duration": 5})
    assert units_in == mu.MAX_REFERENCE_PIXELS


def test_the_worst_case_a_caller_can_declare_still_fits_a_uint32():
    """The caps' whole purpose, asserted end to end rather than argued."""
    units_in, _ = _declare_units({
        "prompt": "x",
        "video": _ref(3840, 2160, media_type="video/mp4",
                      duration_secs=mu.MAX_REFERENCE_DURATION_S),
        "duration": 5,
    })
    assert units_in == 497_664_000 < 2**32 - 1


# --- what a duration is, spelled the same in three languages --------------------


@pytest.mark.parametrize("written", ["7.5", "1e1", "+5", " 5 ", "1_0", "٣", "five", True, [5]])
def test_a_duration_that_is_not_whole_seconds_is_refused(written):
    """`int()` reads most of these and JavaScript's `Number()` reads a different
    most of them. The provider re-derives the clip's length itself, so a spelling
    two parsers disagree about is escrow for seconds that will not be rendered.
    """
    with pytest.raises(ValidationError, match="duration"):
        _declare_units({"prompt": "x", "resolution": "720p", "duration": written})


def test_an_aspect_ratio_of_the_wrong_type_is_refused_not_crashed_on():
    with pytest.raises(ValidationError, match="aspect_ratio"):
        _declare_units({"prompt": "x", "resolution": "720p", "aspect_ratio": [], "duration": 5})


def test_a_reference_clip_must_say_how_long_it_runs():
    """Left out, the clip is priced as a one-second still — and the provider reads
    its real length after the claim and hands the job back. Better refused here,
    before anything is sealed or escrowed."""
    clip = {"b64": "AAAA", "media_type": "video/mp4", "width": 64, "height": 64}
    with pytest.raises(ValidationError, match="video.duration_secs"):
        _declare_units({"prompt": "x", "video": clip, "duration": 5})

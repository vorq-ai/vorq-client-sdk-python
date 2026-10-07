"""SLA tier-name aliases and window pacing.

The SDK accepts tier names (``"async"`` / ``"batch"``) or raw windows
(``"1h"`` / ``"24h"``) anywhere an SLA is taken, and normalizes to the raw
window before sending. Unknown strings pass through verbatim so a future
network-added window needs no SDK update.
"""

from __future__ import annotations

import time

_TIER_ALIASES = {"async": "1h", "batch": "24h"}
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def normalize_sla(value: str) -> str:
    """Resolve a tier alias to its raw window; pass everything else through."""
    return _TIER_ALIASES.get(value, value)


def sla_seconds(window: str) -> int:
    """Duration of an SLA window in seconds.

    Accepts a raw window (``"1h"``, ``"30m"``, ``"45s"``) or a tier alias.
    Unparseable strings fall back to one hour.
    """
    window = normalize_sla(window)
    unit = window[-1:]
    if unit in _UNIT_SECONDS:
        try:
            return int(window[:-1]) * _UNIT_SECONDS[unit]
        except ValueError:
            pass
    return 3600


#: The longest a window shorter than a day waits between two reads of one job.
#:
#: Sixty seconds, which is exactly what the ``"1h"`` window polls at
#: (``3600 / 60``); shorter windows poll faster, down to two seconds.
MAX_POLL_INTERVAL_SECONDS = 60.0

#: The long-wait schedule: ``(waited less than, interval)`` in seconds, read top
#: down, and the last interval once every bound is passed. Once a minute for the
#: first fifteen minutes, every three minutes for the rest of the first hour,
#: every ten minutes after it.
#:
#: Stepped by time spent waiting, never by the window: work that has not come
#: back in an hour is not about to, and a day of once-a-minute reads is 1440
#: requests where this spends 168. The loops clamp every sleep to what is left
#: of the timeout, so a long interval never sleeps past the deadline.
BATCH_POLL_SCHEDULE = ((900.0, 60.0), (3600.0, 180.0))
BATCH_POLL_INTERVAL_SECONDS = 600.0

#: Windows this long are paced by the schedule above rather than by the window.
_SCHEDULED_WINDOW_SECONDS = 86400


def now() -> float:
    """The monotonic clock both wait loops read, in seconds."""
    return time.monotonic()


def batch_poll_interval(elapsed: float) -> float:
    """Poll interval ``elapsed`` seconds into a wait, per :data:`BATCH_POLL_SCHEDULE`."""
    for bound, interval in BATCH_POLL_SCHEDULE:
        if elapsed < bound:
            return interval
    return BATCH_POLL_INTERVAL_SECONDS


def poll_interval(window: str, elapsed: float) -> float:
    """Poll interval for one job, ``elapsed`` seconds into the wait.

    A window of a day or longer follows :func:`batch_poll_interval`. A shorter
    one is paced by the window — ``sla_seconds / 60``, held in
    ``[2, MAX_POLL_INTERVAL_SECONDS]`` seconds.
    """
    if sla_seconds(window) >= _SCHEDULED_WINDOW_SECONDS:
        return batch_poll_interval(elapsed)
    return min(max(sla_seconds(window) / 60, 2.0), MAX_POLL_INTERVAL_SECONDS)


#: The two windows the tiers name, so a round trip through the wire comes back
#: spelled the way the caller asked for it rather than as ``"86400s"``.
_SECONDS_WINDOW = {3600: "1h", 86400: "24h"}


def window_from_seconds(seconds: object) -> str | None:
    """The window a job's ``vorq.sla_secs`` names, or ``None`` if unusable.

    **The job row carries seconds, not a window.** ``clientJob`` projects the
    chain's ``slaSecs`` as an integer and there is no ``sla`` string anywhere on
    it — the window vocabulary is this SDK's own, used on the way in. Reading a
    key the node does not send leaves the pacing unknown, and an unknown pacing
    defaults to one hour: a re-attached ``24h`` job would then be polled once a
    minute and, worse, given a one-hour default timeout, so ``result()`` would
    raise :class:`~vorq.errors.WaitTimeout` on a job that is running normally.

    A duration the two tiers do not name comes back as ``"<n>s"``, which the
    grammar above already parses — so a window the network adds later needs no
    change here.
    """
    try:
        secs = int(str(seconds))
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    return _SECONDS_WINDOW.get(secs) or f"{secs}s"

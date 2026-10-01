"""SLA tier-name aliases and window pacing.

The SDK accepts tier names (``"async"`` / ``"batch"``) or raw windows
(``"1h"`` / ``"24h"``) anywhere an SLA is taken, and normalizes to the raw
window before sending. Unknown strings pass through verbatim so a future
network-added window needs no SDK update.
"""

from __future__ import annotations

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


#: The longest this SDK will wait between two reads of one job.
#:
#: Sixty seconds, and the number is not new: it is exactly what the ``"1h"``
#: window has always polled at (``3600 / 60``). The cap therefore introduces no
#: pacing — it stops a **longer** window from being polled **more slowly** than
#: the fast one, which is what ``sla_seconds / 60`` unbounded actually did: a
#: ``"24h"`` job slept 1440 s, so a job settling one second after a read was
#: reported settled twenty-four minutes later. ``result()``'s default timeout is
#: the job's own SLA and the loop sleeps before it re-reads, so that last sleep
#: could consume the remaining budget and raise ``WaitTimeout`` on a job that
#: had finished well inside its window.
#:
#: The cost of the cap is request volume and it is small: at most 1440 reads of
#: ``GET /v1/jobs/{id}`` over a whole day, which is the same total the ``"1h"``
#: window already spends in an hour.
MAX_POLL_INTERVAL_SECONDS = 60.0


def poll_interval(window: str) -> float:
    """SLA-paced poll interval, held in ``[2, MAX_POLL_INTERVAL_SECONDS]`` seconds."""
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

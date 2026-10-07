import pytest
from vorq._sla import batch_poll_interval, normalize_sla, poll_interval, sla_seconds


class TestNormalizeSla:
    def test_tier_aliases_map_to_raw_windows(self):
        assert normalize_sla("async") == "1h"
        assert normalize_sla("batch") == "24h"

    def test_raw_windows_pass_through(self):
        assert normalize_sla("1h") == "1h"
        assert normalize_sla("24h") == "24h"

    def test_unknown_strings_pass_through_verbatim(self):
        assert normalize_sla("3h") == "3h"
        assert normalize_sla("weird") == "weird"


class TestSlaSeconds:
    def test_known_windows(self):
        assert sla_seconds("1h") == 3600
        assert sla_seconds("24h") == 86400

    def test_parses_minute_and_second_windows(self):
        assert sla_seconds("30m") == 1800
        assert sla_seconds("45s") == 45

    def test_accepts_tier_alias(self):
        assert sla_seconds("async") == 3600
        assert sla_seconds("batch") == 86400


class TestPollInterval:
    def test_async_tier_polls_once_a_minute(self):
        assert poll_interval("1h", 0) == 60.0
        assert poll_interval("1h", 3599) == 60.0

    def test_windows_between_the_floor_and_the_cap_still_pace_by_sla(self):
        assert poll_interval("30m", 0) == 30.0
        assert poll_interval("10m", 0) == 10.0

    def test_floors_at_two_seconds(self):
        assert poll_interval("45s", 0) == 2.0

    @pytest.mark.parametrize("elapsed, interval", [
        (0, 60.0), (899, 60.0), (900, 180.0), (3599, 180.0), (3600, 600.0), (86399, 600.0),
    ])
    def test_the_batch_schedule_steps_at_fifteen_minutes_and_one_hour(self, elapsed, interval):
        assert batch_poll_interval(elapsed) == interval

    @pytest.mark.parametrize("window", ["24h", "batch", "168h"])
    def test_a_window_of_a_day_or_longer_follows_the_batch_schedule(self, window):
        # Unknown windows pass through `sla_seconds` verbatim (a governance-added
        # window needs no SDK update), so the rule is the duration and not the
        # two windows this SDK happens to name today.
        assert poll_interval(window, 0) == 60.0
        assert poll_interval(window, 900) == 180.0
        assert poll_interval(window, 3600) == 600.0

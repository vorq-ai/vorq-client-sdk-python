from vorq._sla import MAX_POLL_INTERVAL_SECONDS, normalize_sla, sla_seconds, poll_interval


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
        assert poll_interval("1h") == 60.0

    def test_the_batch_tier_is_capped_at_the_same_minute(self):
        # Was 1440.0 — twenty-four minutes between reads. `result()`'s default
        # timeout is the job's own SLA and the loop sleeps before it re-reads, so
        # the last sleep could eat the remaining budget and raise WaitTimeout on
        # a job that had already settled.
        assert poll_interval("24h") == MAX_POLL_INTERVAL_SECONDS

    def test_a_window_longer_than_a_day_is_capped_too(self):
        # Unknown windows pass through `sla_seconds` verbatim (a governance-added
        # window needs no SDK update), so the cap has to bound the formula rather
        # than the two windows this SDK happens to name today.
        assert poll_interval("168h") == MAX_POLL_INTERVAL_SECONDS

    def test_windows_between_the_floor_and_the_cap_still_pace_by_sla(self):
        assert poll_interval("30m") == 30.0
        assert poll_interval("10m") == 10.0

    def test_floors_at_two_seconds(self):
        assert poll_interval("45s") == 2.0

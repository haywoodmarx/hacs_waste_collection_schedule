"""Unit tests for selwyn_govt_nz source — pure logic, no live API calls.

Following the stockport pattern: adds the package to sys.path and imports the
real waste_collection_schedule package. pytest has stdlib calendar in sys.modules
before these imports run, so the waste_collection_schedule/calendar.py shadow
that breaks direct python -c invocations doesn't occur here.

Run as part of the default suite:
    pytest tests/test_selwyn_govt_nz.py
"""

import datetime
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "waste_collection_schedule",
        )
    )
)

from waste_collection_schedule.exceptions import (  # noqa: E402
    SourceArgAmbiguousWithSuggestions,
    SourceArgumentException,
    SourceArgumentNotFound,
)
from waste_collection_schedule.service.ArcGis import ArcGisQueryError  # noqa: E402
from waste_collection_schedule.source import selwyn_govt_nz  # noqa: E402
from waste_collection_schedule.source.selwyn_govt_nz import (  # noqa: E402
    ANCHOR_CYCLE_A,
    Source,
    _BinSchedule,
    _adjusted_first_collection,
    _canonical_bin_label,
    _falls_in_cycle_a,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Reference dates relative to the anchor. ANCHOR_CYCLE_A is a Tuesday.
_CYCLE_A = ANCHOR_CYCLE_A                               # 2026-05-05 — Cycle A
_CYCLE_B = ANCHOR_CYCLE_A + datetime.timedelta(days=7)  # 2026-05-12 — Cycle B


class FixedDate(datetime.date):
    """Pins date.today() to ANCHOR_CYCLE_A so projection tests are deterministic."""

    @classmethod
    def today(cls):
        return datetime.date(2026, 5, 5)  # ANCHOR_CYCLE_A, Tuesday, Cycle A


def _attrs(
    charge_type="Rubbish 240L bin",
    day="Tuesday",
    frequency="Weekly",
    schedule="1",
    address="15 Meijer Drive Lincoln",
):
    return {
        "ChargeType": charge_type,
        "COLLECTION_DAY": day,
        "COLLECTION_FREQUENCY": frequency,
        "COLLECTION_SCHEDULE": schedule,
        "Address_full": address,
    }


# ---------------------------------------------------------------------------
# _canonical_bin_label
# ---------------------------------------------------------------------------


class TestCanonicalBinLabel:
    def test_rubbish_240l(self):
        assert _canonical_bin_label("Rubbish 240L bin") == "Rubbish"

    def test_rubbish_80l(self):
        assert _canonical_bin_label("Rubbish 80L bin") == "Rubbish"

    def test_rubbish_prefix_case_insensitive(self):
        assert _canonical_bin_label("RUBBISH 240L") == "Rubbish"

    def test_recycling(self):
        assert _canonical_bin_label("Recycling") == "Recycling"

    def test_recycling_whitespace_and_case(self):
        assert _canonical_bin_label("  RECYCLING  ") == "Recycling"

    def test_organic(self):
        assert _canonical_bin_label("Organic") == "Organic"

    def test_refuse_uniform_charge_is_none(self):
        assert _canonical_bin_label("Refuse Uniform Charge") is None

    def test_empty_string_is_none(self):
        assert _canonical_bin_label("") is None

    def test_unknown_type_is_none(self):
        assert _canonical_bin_label("Future Bin Type 2030") is None


# ---------------------------------------------------------------------------
# _falls_in_cycle_a
# ---------------------------------------------------------------------------


class TestFallsInCycleA:
    def test_anchor_date_is_cycle_a(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A) is True

    def test_one_week_after_anchor_is_cycle_b(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=7)) is False

    def test_two_weeks_after_anchor_is_cycle_a(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=14)) is True

    def test_one_week_before_anchor_is_cycle_b(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A - datetime.timedelta(days=7)) is False

    def test_two_weeks_before_anchor_is_cycle_a(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A - datetime.timedelta(days=14)) is True

    def test_midweek_in_cycle_a_week(self):
        # Thursday of the same Cycle A week
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=2)) is True

    def test_midweek_in_cycle_b_week(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=9)) is False


# ---------------------------------------------------------------------------
# _adjusted_first_collection
# ---------------------------------------------------------------------------
#
# The organic-sched=2 bug (first discovered on user's home address): the
# original code hard-coded organic_collects_on_cycle_a = True regardless of
# schedule, which silently wrong-cycled every sched=2 organic address.
# Verified against the ArcGIS API and the council website for:
#   - "13 Guinevere Drive Lincoln"  (organic sched=2, Cycle B)
#   - "5B Moore Street Rolleston"   (recycling sched=1, Cycle B)


class TestAdjustedFirstCollection:

    # Weekly bins always collect on the candidate date regardless of cycle.

    def test_weekly_rubbish_no_shift(self):
        for candidate in (_CYCLE_A, _CYCLE_B):
            assert _adjusted_first_collection(candidate, "Rubbish", "", "Weekly") == candidate

    def test_weekly_recycling_no_shift(self):
        for candidate in (_CYCLE_A, _CYCLE_B):
            assert _adjusted_first_collection(candidate, "Recycling", "", "Weekly") == candidate

    # Organic: sched=1 → Cycle A (not inverted); sched=2 → Cycle B.

    def test_organic_sched1_candidate_cycle_a_no_shift(self):
        result = _adjusted_first_collection(_CYCLE_A, "Organic", "1", "Fortnightly")
        assert result == _CYCLE_A

    def test_organic_sched1_candidate_cycle_b_shifts_to_cycle_a(self):
        result = _adjusted_first_collection(_CYCLE_B, "Organic", "1", "Fortnightly")
        assert result == _CYCLE_B + datetime.timedelta(days=7)  # 2026-05-19, Cycle A

    def test_organic_sched2_candidate_cycle_b_no_shift(self):
        result = _adjusted_first_collection(_CYCLE_B, "Organic", "2", "Fortnightly")
        assert result == _CYCLE_B

    def test_organic_sched2_candidate_cycle_a_shifts_to_cycle_b(self):
        # Regression: original code returned _CYCLE_A unchanged (hard-coded True).
        result = _adjusted_first_collection(_CYCLE_A, "Organic", "2", "Fortnightly")
        assert result == _CYCLE_A + datetime.timedelta(days=7)  # 2026-05-12, Cycle B

    # Recycling: sched=2 → Cycle A (inverted); sched=1 → Cycle B.

    def test_recycling_sched2_candidate_cycle_a_no_shift(self):
        result = _adjusted_first_collection(_CYCLE_A, "Recycling", "2", "Fortnightly")
        assert result == _CYCLE_A

    def test_recycling_sched2_candidate_cycle_b_shifts_to_cycle_a(self):
        result = _adjusted_first_collection(_CYCLE_B, "Recycling", "2", "Fortnightly")
        assert result == _CYCLE_B + datetime.timedelta(days=7)  # 2026-05-19, Cycle A

    def test_recycling_sched1_candidate_cycle_b_no_shift(self):
        result = _adjusted_first_collection(_CYCLE_B, "Recycling", "1", "Fortnightly")
        assert result == _CYCLE_B

    def test_recycling_sched1_candidate_cycle_a_shifts_to_cycle_b(self):
        result = _adjusted_first_collection(_CYCLE_A, "Recycling", "1", "Fortnightly")
        assert result == _CYCLE_A + datetime.timedelta(days=7)  # 2026-05-12, Cycle B

    # Unknown fortnightly labels fall through without shifting.

    def test_unknown_fortnightly_label_no_shift(self):
        for candidate in (_CYCLE_A, _CYCLE_B):
            result = _adjusted_first_collection(candidate, "FutureBin", "1", "Fortnightly")
            assert result == candidate


# ---------------------------------------------------------------------------
# Source.__init__ — address normalisation
# ---------------------------------------------------------------------------


class TestSourceAddressNormalisation:
    def test_strips_leading_and_trailing_whitespace(self):
        assert Source("  15 Meijer Drive Lincoln  ")._address == "15 Meijer Drive Lincoln"

    def test_collapses_internal_whitespace(self):
        assert Source("15  Meijer  Drive  Lincoln")._address == "15 Meijer Drive Lincoln"


# ---------------------------------------------------------------------------
# _collect_unique_bin_schedules
# ---------------------------------------------------------------------------


class TestCollectUniqueBinSchedules:

    def _src(self):
        return Source("15 Meijer Drive Lincoln")

    def test_drops_refuse_uniform_charge(self):
        schedules = self._src()._collect_unique_bin_schedules(
            [_attrs(charge_type="Refuse Uniform Charge")]
        )
        assert schedules == []

    def test_drops_unknown_charge_type(self):
        assert self._src()._collect_unique_bin_schedules([_attrs(charge_type="Novelty Bin")]) == []

    def test_deduplicates_rubbish_bin_sizes(self):
        # Regression: before the schedule-normalisation fix, weekly bins with
        # different COLLECTION_SCHEDULE values (240L=1, 80L=2) formed distinct
        # _BinSchedule keys and both survived deduplication, producing two
        # Rubbish entries per collection date.
        features = [
            _attrs(charge_type="Rubbish 240L bin", frequency="Weekly", schedule="1"),
            _attrs(charge_type="Rubbish 80L bin", frequency="Weekly", schedule="2"),
        ]
        schedules = self._src()._collect_unique_bin_schedules(features)
        assert len(schedules) == 1
        assert schedules[0].label == "Rubbish"

    def test_keeps_distinct_fortnightly_cycles(self):
        # sched=1 and sched=2 are different fortnightly services — must NOT collapse.
        features = [
            _attrs(charge_type="Recycling", frequency="Fortnightly", schedule="1"),
            _attrs(charge_type="Recycling", frequency="Fortnightly", schedule="2"),
        ]
        schedules = self._src()._collect_unique_bin_schedules(features)
        assert len(schedules) == 2

    def test_skips_unknown_collection_day(self):
        assert self._src()._collect_unique_bin_schedules([_attrs(day="Someday")]) == []

    def test_recognises_all_standard_weekdays(self):
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            schedules = self._src()._collect_unique_bin_schedules([_attrs(day=day)])
            assert len(schedules) == 1, f"Expected 1 schedule for {day}"

    def test_weekly_schedule_normalised_to_empty(self):
        # Weekly bins must store schedule="" so the two rubbish sizes share the
        # same _BinSchedule key regardless of their raw COLLECTION_SCHEDULE value.
        schedules = self._src()._collect_unique_bin_schedules(
            [_attrs(frequency="Weekly", schedule="2")]
        )
        assert schedules[0].schedule == ""

    def test_fortnightly_schedule_preserved(self):
        schedules = self._src()._collect_unique_bin_schedules(
            [_attrs(charge_type="Organic", frequency="Fortnightly", schedule="2")]
        )
        assert schedules[0].schedule == "2"


# ---------------------------------------------------------------------------
# _query_features_for_address — error paths (mocked)
# ---------------------------------------------------------------------------


class TestQueryFeaturesErrorPaths:

    def test_address_with_quote_raises(self):
        with pytest.raises(SourceArgumentException):
            Source("O'Brien Street Lincoln")._query_features_for_address()

    def test_not_found_raises(self):
        with patch(
            "waste_collection_schedule.source.selwyn_govt_nz.query_feature_layer",
            side_effect=ArcGisQueryError("no features"),
        ):
            with pytest.raises(SourceArgumentNotFound):
                Source("Nonexistent Address Lincoln")._query_features_for_address()

    def test_ambiguous_address_raises(self):
        rows = [
            _attrs(address="15 Meijer Drive Lincoln"),
            _attrs(address="16 Meijer Drive Lincoln"),
        ]
        with patch(
            "waste_collection_schedule.source.selwyn_govt_nz.query_feature_layer",
            return_value=rows,
        ):
            with pytest.raises(SourceArgAmbiguousWithSuggestions):
                Source("Meijer Drive")._query_features_for_address()

    def test_ambiguous_suggestions_contain_both_addresses(self):
        rows = [
            _attrs(address="15 Meijer Drive Lincoln"),
            _attrs(address="16 Meijer Drive Lincoln"),
        ]
        with patch(
            "waste_collection_schedule.source.selwyn_govt_nz.query_feature_layer",
            return_value=rows,
        ):
            with pytest.raises(SourceArgAmbiguousWithSuggestions) as exc_info:
                Source("Meijer Drive")._query_features_for_address()
        assert "15 Meijer Drive Lincoln" in exc_info.value.suggestions
        assert "16 Meijer Drive Lincoln" in exc_info.value.suggestions

    def test_single_address_returned_unchanged(self):
        rows = [_attrs(address="15 Meijer Drive Lincoln")]
        with patch(
            "waste_collection_schedule.source.selwyn_govt_nz.query_feature_layer",
            return_value=rows,
        ):
            result = Source("15 Meijer Drive Lincoln")._query_features_for_address()
        assert result == rows


# ---------------------------------------------------------------------------
# _generate_collection_entries
# ---------------------------------------------------------------------------
#
# today is pinned to 2026-05-05 (ANCHOR_CYCLE_A, Tuesday, Cycle A) so that
# first-collection dates can be asserted exactly without depending on the
# calendar date when tests run.


class TestGenerateCollectionEntries:

    def _src(self):
        return Source("15 Meijer Drive Lincoln")

    def test_weekly_first_entry_is_today_when_today_matches_weekday(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        # today = 2026-05-05 (Tuesday). A Tuesday weekly bin collects today.
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 5)

    def test_weekly_step_is_7_days(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        entries = self._src()._generate_collection_entries([schedule])
        dates = sorted(e.date for e in entries)
        steps = {(dates[i] - dates[i - 1]).days for i in range(1, min(6, len(dates)))}
        assert steps == {7}

    def test_fortnightly_step_is_14_days(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Recycling", weekday=1, frequency="Fortnightly", schedule="1")
        entries = self._src()._generate_collection_entries([schedule])
        dates = sorted(e.date for e in entries)
        steps = {(dates[i] - dates[i - 1]).days for i in range(1, min(6, len(dates)))}
        assert steps == {14}

    def test_organic_sched1_first_entry_is_cycle_a(self, monkeypatch):
        # today = 2026-05-05 (Cycle A Tuesday). Organic sched=1 → Cycle A.
        # Candidate = today. Cycle already correct → no shift.
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Organic", weekday=1, frequency="Fortnightly", schedule="1")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 5)  # Cycle A

    def test_organic_sched2_first_entry_is_cycle_b(self, monkeypatch):
        # today = 2026-05-05 (Cycle A Tuesday). Organic sched=2 → Cycle B.
        # Candidate lands on Cycle A → shifted +7 to 2026-05-12 (Cycle B).
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Organic", weekday=1, frequency="Fortnightly", schedule="2")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 12)  # Cycle B

    def test_all_dates_within_projection_window(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        today = datetime.date(2026, 5, 5)
        end = today + datetime.timedelta(days=365)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        for e in self._src()._generate_collection_entries([schedule]):
            assert today <= e.date <= end

    def test_all_entries_land_on_correct_weekday(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        entries = self._src()._generate_collection_entries([schedule])
        for e in entries:
            assert e.date.weekday() == 1, f"{e.date} is not a Tuesday"

    def test_icons_match_icon_map(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedules = [
            _BinSchedule("Rubbish", 1, "Weekly", ""),
            _BinSchedule("Recycling", 1, "Fortnightly", "2"),
            _BinSchedule("Organic", 1, "Fortnightly", "1"),
        ]
        entries = self._src()._generate_collection_entries(schedules)
        icon_by_type = {e.type: e.icon for e in entries}
        assert icon_by_type["Rubbish"] == "mdi:trash-can"
        assert icon_by_type["Recycling"] == "mdi:recycle"
        assert icon_by_type["Organic"] == "mdi:leaf"

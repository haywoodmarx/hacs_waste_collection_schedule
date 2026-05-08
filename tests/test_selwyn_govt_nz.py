"""Unit tests for selwyn_govt_nz — pure logic, no live API calls."""

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

_CYCLE_A = ANCHOR_CYCLE_A                               # 2026-05-05 — Cycle A
_CYCLE_B = ANCHOR_CYCLE_A + datetime.timedelta(days=7)  # 2026-05-12 — Cycle B


class FixedDate(datetime.date):
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

    def test_recycling(self):
        assert _canonical_bin_label("Recycling") == "Recycling"

    def test_organic(self):
        assert _canonical_bin_label("Organic") == "Organic"

    def test_refuse_uniform_charge_is_none(self):
        assert _canonical_bin_label("Refuse Uniform Charge") is None


# ---------------------------------------------------------------------------
# _falls_in_cycle_a
# ---------------------------------------------------------------------------


class TestFallsInCycleA:
    def test_anchor_date_is_cycle_a(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A) is True

    def test_one_week_after_is_cycle_b(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=7)) is False

    def test_two_weeks_after_is_cycle_a(self):
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=14)) is True

    def test_one_week_before_is_cycle_b(self):
        # Verifies Python floor-division behaviour with negative offsets.
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A - datetime.timedelta(days=7)) is False

    def test_midweek_maps_to_same_cycle_as_week_start(self):
        # Cycle is per-week, not per-day.
        assert _falls_in_cycle_a(ANCHOR_CYCLE_A + datetime.timedelta(days=2)) is True


# ---------------------------------------------------------------------------
# _adjusted_first_collection
# ---------------------------------------------------------------------------


class TestAdjustedFirstCollection:

    def test_weekly_no_shift(self):
        assert _adjusted_first_collection(_CYCLE_A, "Rubbish", "", "Weekly") == _CYCLE_A

    # Organic: sched=1 → Cycle A (not inverted); sched=2 → Cycle B.

    def test_organic_sched1_cycle_a_no_shift(self):
        assert _adjusted_first_collection(_CYCLE_A, "Organic", "1", "Fortnightly") == _CYCLE_A

    def test_organic_sched1_cycle_b_shifts(self):
        assert _adjusted_first_collection(_CYCLE_B, "Organic", "1", "Fortnightly") == _CYCLE_B + datetime.timedelta(days=7)

    def test_organic_sched2_cycle_b_no_shift(self):
        assert _adjusted_first_collection(_CYCLE_B, "Organic", "2", "Fortnightly") == _CYCLE_B

    def test_organic_sched2_cycle_a_shifts(self):
        # sched=2 organic collects on Cycle B; if candidate is Cycle A it must shift.
        assert _adjusted_first_collection(_CYCLE_A, "Organic", "2", "Fortnightly") == _CYCLE_A + datetime.timedelta(days=7)

    # Recycling: sched=2 → Cycle A (inverted from organic); sched=1 → Cycle B.

    def test_recycling_sched2_cycle_a_no_shift(self):
        assert _adjusted_first_collection(_CYCLE_A, "Recycling", "2", "Fortnightly") == _CYCLE_A

    def test_recycling_sched2_cycle_b_shifts(self):
        assert _adjusted_first_collection(_CYCLE_B, "Recycling", "2", "Fortnightly") == _CYCLE_B + datetime.timedelta(days=7)

    def test_recycling_sched1_cycle_b_no_shift(self):
        assert _adjusted_first_collection(_CYCLE_B, "Recycling", "1", "Fortnightly") == _CYCLE_B

    def test_recycling_sched1_cycle_a_shifts(self):
        assert _adjusted_first_collection(_CYCLE_A, "Recycling", "1", "Fortnightly") == _CYCLE_A + datetime.timedelta(days=7)

    def test_unknown_fortnightly_label_no_shift(self):
        # Unknown future bin types must not be shifted; fall through unmodified.
        assert _adjusted_first_collection(_CYCLE_A, "FutureBin", "1", "Fortnightly") == _CYCLE_A


# ---------------------------------------------------------------------------
# Source.__init__ — address normalisation
# ---------------------------------------------------------------------------


class TestSourceAddressNormalisation:
    def test_strips_whitespace(self):
        assert Source("  15 Meijer Drive Lincoln  ")._address == "15 Meijer Drive Lincoln"

    def test_collapses_internal_whitespace(self):
        assert Source("15  Meijer  Drive  Lincoln")._address == "15 Meijer Drive Lincoln"


# ---------------------------------------------------------------------------
# _collect_unique_bin_schedules
# ---------------------------------------------------------------------------


class TestCollectUniqueBinSchedules:

    def _src(self):
        return Source("15 Meijer Drive Lincoln")

    def test_drops_billing_row(self):
        assert self._src()._collect_unique_bin_schedules([_attrs(charge_type="Refuse Uniform Charge")]) == []

    def test_deduplicates_rubbish_bin_sizes(self):
        # The 240L and 80L bins share a collection day but appear as separate API rows.
        # Weekly schedule values differ between them, so without normalising schedule=""
        # for weekly bins they would form distinct keys and generate duplicate dates.
        features = [
            _attrs(charge_type="Rubbish 240L bin", frequency="Weekly", schedule="1"),
            _attrs(charge_type="Rubbish 80L bin", frequency="Weekly", schedule="2"),
        ]
        schedules = self._src()._collect_unique_bin_schedules(features)
        assert len(schedules) == 1
        assert schedules[0].label == "Rubbish"

    def test_fortnightly_cycles_not_deduplicated(self):
        # sched=1 and sched=2 are genuinely different fortnightly collection weeks.
        features = [
            _attrs(charge_type="Recycling", frequency="Fortnightly", schedule="1"),
            _attrs(charge_type="Recycling", frequency="Fortnightly", schedule="2"),
        ]
        assert len(self._src()._collect_unique_bin_schedules(features)) == 2

    def test_skips_unknown_collection_day(self):
        assert self._src()._collect_unique_bin_schedules([_attrs(day="Someday")]) == []

    def test_non_tuesday_weekday_recognised(self):
        # Different towns use different days; verify the lookup isn't Tuesday-specific.
        schedules = self._src()._collect_unique_bin_schedules([_attrs(day="Thursday")])
        assert len(schedules) == 1
        assert schedules[0].weekday == 3

    def test_weekly_schedule_normalised_to_empty(self):
        schedules = self._src()._collect_unique_bin_schedules([_attrs(frequency="Weekly", schedule="2")])
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

    def test_ambiguous_address_raises_with_suggestions(self):
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


# ---------------------------------------------------------------------------
# _generate_collection_entries
# ---------------------------------------------------------------------------


class TestGenerateCollectionEntries:

    def _src(self):
        return Source("15 Meijer Drive Lincoln")

    def test_weekly_collects_today_when_weekday_matches(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 5)

    def test_weekly_step(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        dates = sorted(e.date for e in self._src()._generate_collection_entries([schedule]))
        assert {(dates[i] - dates[i - 1]).days for i in range(1, 5)} == {7}

    def test_fortnightly_step(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Recycling", weekday=1, frequency="Fortnightly", schedule="1")
        dates = sorted(e.date for e in self._src()._generate_collection_entries([schedule]))
        assert {(dates[i] - dates[i - 1]).days for i in range(1, 5)} == {14}

    def test_organic_sched1_first_date_is_cycle_a(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Organic", weekday=1, frequency="Fortnightly", schedule="1")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 5)

    def test_organic_sched2_first_date_is_cycle_b(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Organic", weekday=1, frequency="Fortnightly", schedule="2")
        entries = self._src()._generate_collection_entries([schedule])
        assert min(e.date for e in entries) == datetime.date(2026, 5, 12)

    def test_entries_land_on_correct_weekday(self, monkeypatch):
        monkeypatch.setattr(selwyn_govt_nz.datetime, "date", FixedDate)
        schedule = _BinSchedule("Rubbish", weekday=1, frequency="Weekly", schedule="")
        for e in self._src()._generate_collection_entries([schedule]):
            assert e.date.weekday() == 1

"""Selwyn District Council kerbside collection source.

Reads the public ArcGIS feature service backing the council collection-day
lookup and projects ~12 months of collection dates locally.

Known limitations:
- Public-holiday collection-day shifts are not applied. The council publishes
  shifts only as free-form text on a Cloudflare-protected HTML page; scraping
  it would silently drift out of date when the council reformats the page,
  with no signal to the user. A few Friday-route addresses will see one or
  two incorrect dates per year (Good Friday, Christmas, New Year's Day).
- Address ambiguity (multiple matches) is surfaced as
  SourceArgAmbiguousWithSuggestions; the user must disambiguate manually.
"""

import datetime
import logging
import re
from dataclasses import dataclass

from waste_collection_schedule import Collection  # type: ignore[attr-defined]
from waste_collection_schedule.exceptions import (
    SourceArgAmbiguousWithSuggestions,
    SourceArgumentException,
    SourceArgumentNotFound,
)
from waste_collection_schedule.service.ArcGis import ArcGisQueryError, query_feature_layer

_LOGGER = logging.getLogger(__name__)

TITLE = "Selwyn District Council"
DESCRIPTION = "Source for Selwyn District Council kerbside collection schedules."
URL = "https://www.selwyn.govt.nz"
TEST_CASES = {
    # Tuesday Lincoln, recycling sched=1 (Cycle B), organic sched=1 (Cycle A)
    "15 Meijer Drive Lincoln": {"address": "15 Meijer Drive Lincoln"},
    # Tuesday Lincoln, recycling sched=2 (Cycle A), organic sched=2 (Cycle B)
    "13 Guinevere Drive Lincoln": {"address": "13 Guinevere Drive Lincoln"},
    # Friday Rolleston, recycling sched=1 (Cycle B), organic sched=1 (Cycle A)
    "5B Moore Street Rolleston": {"address": "5B Moore Street Rolleston"},
    # Thursday Darfield, schedule 1 (kept for day-of-week coverage)
    "9 Adams Road Darfield": {"address": "9 Adams Road Darfield"},
}

ICON_MAP = {
    "Rubbish": "mdi:trash-can",
    "Recycling": "mdi:recycle",
    "Organic": "mdi:leaf",
}

HOW_TO_GET_ARGUMENTS_DESCRIPTION = {
    "en": (
        "Enter your full kerbside address as it appears on the council "
        "lookup at https://www.selwyn.govt.nz/services/rubbish-recycling"
        "-And-organics/kerbside-collections/collection-days-and-routes "
        '(e.g. "15 Meijer Drive Lincoln"). Partial addresses also work '
        "but may match multiple properties."
    ),
}

PARAM_DESCRIPTIONS = {
    "en": {
        "address": "Your full Selwyn District kerbside address.",
    },
}

PARAM_TRANSLATIONS = {
    "en": {
        "address": "Address",
    },
}

_FEATURE_LAYER_URL = "https://gis.selwyn.govt.nz/arcgis/rest/services/SDC_Public/Refuse_address/MapServer/0"

# Selwyn DC COLLECTION_SCHEDULE semantics (verified empirically; see project
# Research note). Only fortnightly bins have a meaningful cycle:
#   - organic: sched="1" -> Cycle A; sched="2" -> Cycle B
#   - recycling: INVERTED — sched="2" -> Cycle A; sched="1" -> Cycle B
ANCHOR_CYCLE_A = datetime.date(2026, 5, 5)  # Tuesday, Cycle A

PROJECTION_DAYS = 365

_WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class _BinSchedule:
    """A unique kerbside service.

    Captures which bin is collected, on which weekday, how often, and
    (for fortnightly bins) which cycle.
    """

    label: str
    weekday: int
    frequency: str
    schedule: str


def _canonical_bin_label(charge_type: str) -> str | None:
    """Map an API ``ChargeType`` value to a canonical bin label.

    Collapses the two rubbish bin sizes into a single ``"Rubbish"`` label
    and drops billing-only and unknown charge types (which return ``None``).
    """
    normalised = charge_type.strip().lower()
    if normalised.startswith("rubbish"):
        return "Rubbish"
    if normalised == "recycling":
        return "Recycling"
    if normalised == "organic":
        return "Organic"
    # ``refuse uniform charge`` is a billing line, not a real collection;
    # any other value is an unknown future ChargeType. Drop both silently.
    return None


def _falls_in_cycle_a(date: datetime.date) -> bool:
    """Return ``True`` if ``date`` falls in Cycle A of the rotation.

    Cycle A is the same week as the anchor date 2026-05-05.
    """
    days_from_anchor = (date - ANCHOR_CYCLE_A).days
    full_weeks_from_anchor = days_from_anchor // 7
    return full_weeks_from_anchor % 2 == 0


def _adjusted_first_collection(
    candidate: datetime.date,
    label: str,
    schedule: str,
    frequency: str,
) -> datetime.date:
    """Return the first collection date, shifted to the correct cycle.

    If ``candidate`` falls on the wrong cycle for the given bin and
    schedule, it is shifted forward by one week. Weekly bins always
    collect on the candidate date. For fortnightly bins:

    - **Organic** has non-inverted semantics: ``schedule == "1"`` means
      Cycle A, ``schedule == "2"`` means Cycle B.
    - **Recycling** has *inverted* semantics: ``schedule == "2"`` means
      Cycle A, ``schedule == "1"`` means Cycle B.
    - Future fortnightly bin types fall through unshifted.
    """
    if frequency != "Fortnightly":
        return candidate

    candidate_falls_in_cycle_a = _falls_in_cycle_a(candidate)
    one_week_forward = candidate + datetime.timedelta(days=7)

    if label in ("Organic", "Recycling"):
        # Organic: sched=1 → Cycle A (not inverted). Recycling: sched=2 → Cycle A (inverted).
        collects_on_cycle_a = (schedule == "1") if label == "Organic" else (schedule == "2")
        return candidate if candidate_falls_in_cycle_a == collects_on_cycle_a else one_week_forward

    return candidate


class Source:
    """Selwyn District Council source. Configured with a single ``address``."""

    def __init__(self, address: str) -> None:
        self._address = re.sub(r"\s+", " ", address).strip()

    def fetch(self) -> list[Collection]:
        """Return projected collection entries for the configured address.

        The pipeline runs in three stages:

        1. Query the council ArcGIS service for all bin services at the address.
        2. Reduce the raw rows to a unique set of bin schedules.
        3. Project each schedule forward ``PROJECTION_DAYS`` days.
        """
        features = self._query_features_for_address()
        bin_schedules = self._collect_unique_bin_schedules(features)
        return self._generate_collection_entries(bin_schedules)

    def _query_features_for_address(self) -> list[dict]:
        """Query the ArcGIS service and return matching attribute dicts.

        Raises:
            SourceArgumentException: if the address contains a single quote
                (would break the SQL ``where`` clause).
            SourceArgumentNotFound: if no rows match.
            SourceArgAmbiguousWithSuggestions: if more than one distinct
                ``Address_full`` matches.
        """
        if "'" in self._address:
            raise SourceArgumentException("address", "Address may not contain quotes")

        where_clause = f"LOWER(Address_full) LIKE '{self._address.lower()}%'"

        try:
            features = query_feature_layer(
                _FEATURE_LAYER_URL, where=where_clause, timeout=30
            )
        except ArcGisQueryError:
            raise SourceArgumentNotFound("address", self._address)

        _LOGGER.debug("Selwyn API returned %d features", len(features))

        distinct_addresses = sorted({a["Address_full"] for a in features})
        if len(distinct_addresses) > 1:
            raise SourceArgAmbiguousWithSuggestions(
                "address",
                self._address,
                suggestions=distinct_addresses,
            )

        return features

    def _collect_unique_bin_schedules(self, features: list[dict]) -> list[_BinSchedule]:
        """Reduce raw API features to a unique list of ``_BinSchedule``.

        Drops billing-only and unknown ``ChargeType`` rows. Collapses the
        two rubbish bin sizes (240 L and 80 L) into a single Rubbish entry
        when their day/frequency/schedule match. Skips rows whose
        ``COLLECTION_DAY`` value is not a recognised English weekday name.
        """
        seen_schedules: set[_BinSchedule] = set()
        unique_schedules: list[_BinSchedule] = []

        for attributes in features:
            label = _canonical_bin_label(attributes.get("ChargeType", ""))
            if label is None:
                continue

            day_name = attributes.get("COLLECTION_DAY", "")
            normalised_day = day_name.strip().lower()
            weekday = _WEEKDAY_NUMBERS.get(normalised_day)
            if weekday is None:
                _LOGGER.warning("Unknown COLLECTION_DAY %r — skipping row", day_name)
                continue

            frequency = attributes.get("COLLECTION_FREQUENCY", "Weekly")
            # schedule only matters for fortnightly cycle selection; normalise
            # it away for weekly bins so duplicate sizes collapse correctly.
            schedule = (
                str(attributes.get("COLLECTION_SCHEDULE", "1"))
                if frequency == "Fortnightly"
                else ""
            )

            bin_schedule = _BinSchedule(
                label=label,
                weekday=weekday,
                frequency=frequency,
                schedule=schedule,
            )
            if bin_schedule in seen_schedules:
                continue

            seen_schedules.add(bin_schedule)
            unique_schedules.append(bin_schedule)

        return unique_schedules

    def _generate_collection_entries(
        self, bin_schedules: list[_BinSchedule]
    ) -> list[Collection]:
        """Project each schedule forward and return collection entries.

        Each ``_BinSchedule`` is projected forward ``PROJECTION_DAYS`` days
        and emitted as :class:`Collection` entries.
        """
        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=PROJECTION_DAYS)

        entries: list[Collection] = []

        for schedule in bin_schedules:
            days_until_first_collection = (schedule.weekday - today.weekday()) % 7
            candidate_first = today + datetime.timedelta(
                days=days_until_first_collection
            )
            first_collection_date = _adjusted_first_collection(
                candidate=candidate_first,
                label=schedule.label,
                schedule=schedule.schedule,
                frequency=schedule.frequency,
            )

            step_days = 7 if schedule.frequency == "Weekly" else 14
            step = datetime.timedelta(days=step_days)
            icon = ICON_MAP.get(schedule.label)

            collection_date = first_collection_date
            while collection_date <= end_date:
                entries.append(
                    Collection(
                        date=collection_date,
                        t=schedule.label,
                        icon=icon,
                    )
                )
                collection_date += step

        return entries

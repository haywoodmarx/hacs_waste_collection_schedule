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

import requests
from waste_collection_schedule import Collection  # type: ignore[attr-defined]
from waste_collection_schedule.exceptions import (
    SourceArgAmbiguousWithSuggestions,
    SourceArgumentException,
    SourceArgumentNotFound,
)

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

API_URL = "https://gis.selwyn.govt.nz/arcgis/rest/services/SDC_Public/Refuse_address/MapServer/0/query"

# Selwyn DC schedule field semantics (verified empirically against the
# council website; see project Research note):
#   - rubbish/refuse uniform charge: weekly; schedule ignored
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

    label: str  # "Rubbish" | "Recycling" | "Organic"
    weekday: int  # Python weekday: Monday=0 ... Sunday=6
    frequency: str  # "Weekly" | "Fortnightly"
    schedule: str  # COLLECTION_SCHEDULE: "1" or "2"


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

    if label == "Organic":
        organic_collects_on_cycle_a = (
            schedule == "1"
        )  # NOT inverted: sched=1 -> Cycle A
        cycle_is_correct = candidate_falls_in_cycle_a == organic_collects_on_cycle_a
        return candidate if cycle_is_correct else one_week_forward

    if label == "Recycling":
        recycling_collects_on_cycle_a = schedule == "2"  # INVERTED: sched=2 -> Cycle A
        cycle_is_correct = candidate_falls_in_cycle_a == recycling_collects_on_cycle_a
        return candidate if cycle_is_correct else one_week_forward

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
        """Query the ArcGIS service and return matching feature dicts.

        Raises:
            SourceArgumentException: if the address contains a single quote
                (would break the SQL ``where`` clause).
            SourceArgumentNotFound: if no rows match.
            SourceArgAmbiguousWithSuggestions: if more than one distinct
                ``Address_full`` matches.
        """
        if "'" in self._address:
            raise SourceArgumentException("address", "Address may not contain quotes")

        address_pattern = f"{self._address.lower()}%"
        where_clause = f"LOWER(Address_full) LIKE '{address_pattern}'"

        params = {
            "f": "json",
            "where": where_clause,
            "outFields": "*",
            "returnGeometry": "false",
        }

        _LOGGER.debug("Selwyn API request: %s params=%s", API_URL, params)

        try:
            response = requests.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as err:
            raise Exception(f"Selwyn API request failed: {err}") from err

        features = payload.get("features", [])
        _LOGGER.debug("Selwyn API returned %d features", len(features))

        if not features:
            raise SourceArgumentNotFound("address", self._address)

        full_addresses = (f["attributes"]["Address_full"] for f in features)
        distinct_addresses = sorted(set(full_addresses))
        address_is_ambiguous = len(distinct_addresses) > 1
        if address_is_ambiguous:
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

        for feature in features:
            attributes = feature["attributes"]

            label = _canonical_bin_label(attributes.get("ChargeType", ""))
            if label is None:
                # Billing-only or unknown ChargeType — drop the row.
                continue

            day_name = attributes.get("COLLECTION_DAY", "")
            normalised_day = day_name.strip().lower()
            weekday = _WEEKDAY_NUMBERS.get(normalised_day)
            if weekday is None:
                _LOGGER.warning("Unknown COLLECTION_DAY %r — skipping row", day_name)
                continue

            frequency = attributes.get("COLLECTION_FREQUENCY", "Weekly")
            schedule = str(attributes.get("COLLECTION_SCHEDULE", "1"))

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
        and emitted as :class:`Collection` entries, deduplicated on
        ``(date, label)`` pairs.
        """
        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=PROJECTION_DAYS)

        emitted: set[tuple[datetime.date, str]] = set()
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

            collection_date = first_collection_date
            while collection_date <= end_date:
                pair = (collection_date, schedule.label)
                if pair in emitted:
                    collection_date += step
                    continue

                emitted.add(pair)
                entries.append(
                    Collection(
                        date=collection_date,
                        t=schedule.label,
                        icon=ICON_MAP.get(schedule.label),
                    )
                )
                collection_date += step

        return entries

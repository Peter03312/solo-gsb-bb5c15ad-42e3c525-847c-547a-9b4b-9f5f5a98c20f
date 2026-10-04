"""Time helpers.

All timestamps are normalised to a fixed-width UTC ISO-8601 string
(``YYYY-MM-DDTHH:MM:SS.ffffffZ``) so that lexicographic ordering of the
stored text is identical to chronological ordering.  This lets SQLite
compare time windows with plain string comparisons.
"""

from __future__ import annotations

from datetime import datetime, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def to_utc(dt: datetime) -> datetime:
    """Interpret naive datetimes as UTC and convert aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Normalise a datetime to the canonical fixed-width UTC string."""
    return to_utc(dt).strftime(ISO_FMT) + "Z"


def parse_iso(value: str) -> datetime:
    """Parse a canonical string produced by :func:`to_iso`."""
    return datetime.strptime(value, ISO_FMT + "Z").replace(tzinfo=timezone.utc)


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))

"""Time handling for settlement.

All domain instants are normalized to timezone-aware UTC ``datetime`` values.
Local wall-clock values are only accepted when a timezone is supplied, because a
naive timestamp at a DST boundary is not a single instant.  Ambiguous local
times (the repeated hour at a fall-back transition) are resolved with
``fold=0`` and the decision is recorded in provenance, so repeated runs produce
the same answer.  Non-existent local times (the skipped hour at spring-forward)
are rejected rather than silently shifted.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimeError(ValueError):
    """Raised when a timestamp cannot be mapped to exactly one instant."""


# Parsing is a pure function of the payload, but timezone ambiguity is worth
# surfacing in the audit.  A context-local collector keeps that side channel
# out of every call signature and avoids global process state.
_tz_notes: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar("grid_flex_tz_notes", default=None)


@contextmanager
def collect_timezone_notes():
    bucket: list[dict[str, Any]] = []
    token = _tz_notes.set(bucket)
    try:
        yield bucket
    finally:
        _tz_notes.reset(token)


def _record_note(field: str, note: dict[str, Any]) -> None:
    bucket = _tz_notes.get()
    if bucket is not None:
        payload = {"code": "dst_ambiguous", "field": field, "severity": "warning", **note}
        if payload not in bucket:
            bucket.append(payload)


def _as_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def parse_timestamp(
    value: Any,
    *,
    default_tz: Optional[str] = None,
    field: str = "timestamp",
) -> datetime:
    """Parse an ISO-8601 timestamp into UTC.

    ``Z`` suffixes are accepted.  Offset-less values require ``default_tz`` and
    are interpreted as local wall-clock times.  The function is deliberately
    strict about DST gaps and records ambiguity via :func:`localize_with_note`.
    """

    if value is None or value == "":
        raise TimeError(f"{field}: missing timestamp")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if not default_tz:
                raise TimeError(f"{field}: naive datetime requires a timezone")
            instant, note = localize_naive(value, default_tz, field=field)
            if note:
                _record_note(field, note)
            return instant
        return value.astimezone(timezone.utc)

    text = _as_text(value)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeError(f"{field}: invalid ISO-8601 timestamp {value!r}") from exc

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)

    if not default_tz:
        raise TimeError(f"{field}: offset-less timestamp {value!r} requires a timezone")
    instant, note = localize_naive(parsed, default_tz, field=field)
    if note:
        _record_note(field, note)
    return instant


def localize_naive(
    naive: datetime, tz_name: str, *, field: str = "timestamp"
) -> tuple[datetime, dict[str, Any]]:
    """Attach an IANA timezone with deterministic DST handling.

    Returns ``(utc_instant, note)``.  ``note`` is empty for an unambiguous
    value, contains ``"dst_ambiguous"`` for the repeated fall-back hour, or
    raises for a skipped spring-forward hour.
    """

    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise TimeError(f"{field}: unknown timezone {tz_name!r}") from exc

    note: dict[str, Any] = {}
    fold0 = naive.replace(tzinfo=zone, fold=0)
    fold1 = naive.replace(tzinfo=zone, fold=1)

    # A non-existent wall time round-trips to a different local wall time when
    # the UTC instant is converted back to the same zone.
    roundtrip = fold0.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive:
        raise TimeError(
            f"{field}: local time {naive.isoformat()} does not exist in {tz_name} "
            "(DST spring-forward gap)"
        )

    if fold0.utcoffset() != fold1.utcoffset():
        chosen = fold0
        note = {
            "dst_ambiguous": True,
            "timezone": tz_name,
            "resolution": "fold=0 (first occurrence)",
            "utc_offset_seconds": int(chosen.utcoffset().total_seconds()),
        }
    else:
        chosen = naive.replace(tzinfo=zone)
    return chosen.astimezone(timezone.utc), note


def format_instant(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_optional(value: Any, *, tz: Optional[str] = None, field: str = "timestamp") -> Optional[datetime]:
    if value is None or value == "":
        return None
    return parse_timestamp(value, default_tz=tz, field=field)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise TimeError("instant is missing timezone information")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Interval:
    """A half-open interval ``[start, end)`` in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start)
        end = ensure_utc(self.end)
        if end < start:
            raise TimeError(f"interval ends before it starts: {start.isoformat()} -> {end.isoformat()}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def duration_hours(self):
        from decimal import Decimal

        return Decimal(self.duration_seconds) / Decimal(3600)

    @property
    def empty(self) -> bool:
        return self.end <= self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> Optional["Interval"]:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end <= start:
            return None
        return Interval(start, end)

    def contains(self, other: "Interval") -> bool:
        return self.start <= other.start and other.end <= self.end

    def clip_to(self, window: "Interval") -> Optional["Interval"]:
        return self.intersection(window)

    def local_bounds(self, tz_name: str) -> tuple[str, str]:
        zone = ZoneInfo(tz_name)
        return (
            self.start.astimezone(zone).isoformat(),
            self.end.astimezone(zone).isoformat(),
        )

    def as_dict(self, *, timezone_name: Optional[str] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "start": format_instant(self.start),
            "end": format_instant(self.end),
            "duration_seconds": self.duration_seconds,
        }
        if timezone_name:
            local_start, local_end = self.local_bounds(timezone_name)
            payload["timezone"] = timezone_name
            payload["local_start"] = local_start
            payload["local_end"] = local_end
        return payload


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Return sorted, non-overlapping intervals; touching intervals merge."""

    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    merged: list[Interval] = []
    for item in ordered:
        if item.empty:
            continue
        if not merged or item.start > merged[-1].end:
            merged.append(item)
        else:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, item.end))
    return merged


def split_in_local_midnight(window: Interval, tz_name: str) -> list[Interval]:
    """Split an interval at local midnights, preserving exact UTC duration.

    This is used when a rate or rule changes per local calendar day.  The split
    points are derived by walking local dates, so a DST day correctly contains
    23 or 25 hours.
    """

    zone = ZoneInfo(tz_name)
    if window.empty:
        return []
    local_start = window.start.astimezone(zone)
    local_end = window.end.astimezone(zone)
    boundaries: list[datetime] = []
    day = local_start.date()
    while day <= local_end.date():
        midnight = datetime(day.year, day.month, day.day, tzinfo=zone)
        instant = midnight.astimezone(timezone.utc)
        if window.start < instant < window.end:
            boundaries.append(instant)
        day += timedelta(days=1)
    points = [window.start, *sorted(set(boundaries)), window.end]
    return [Interval(points[i], points[i + 1]) for i in range(len(points) - 1) if points[i] < points[i + 1]]


def weighted_average(samples: Iterable[tuple[Interval, "Decimal"]], window: Interval) -> "Decimal":
    """Duration-weighted average of interval values clipped to ``window``."""

    from decimal import Decimal

    numerator = Decimal(0)
    seconds = 0
    for interval, value in samples:
        clipped = interval.clip_to(window)
        if clipped is None:
            continue
        numerator += value * Decimal(clipped.duration_seconds)
        seconds += clipped.duration_seconds
    if seconds == 0:
        return Decimal(0)
    return numerator / Decimal(seconds)

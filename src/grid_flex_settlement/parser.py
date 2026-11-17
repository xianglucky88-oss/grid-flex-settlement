"""Input validation and normalization.

The parser is strict about ambiguity but tolerant of record order and of
unknown extra fields.  It performs no monetary calculation; it only turns a
JSON payload into frozen, fingerprinted domain objects.

Two input shapes are supported:

* the **event-level schema** (``schema_version >= 2`` or the presence of
  ``resources``/``events``), described in the README; and
* the **legacy single-resource shape** used by the original fixture
  (``readings`` + ``event``), which is lifted into one synthetic event and one
  synthetic resource.  This keeps the original CLI contract working while the
  settlement engine handles both through the same path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from .canon import fingerprint
from .decimalutil import AmountError, dec
from .models import (
    BaselineVersion,
    Correction,
    DispatchInstruction,
    EligibilityWindow,
    Event,
    ExecutionSample,
    PerformanceBand,
    PriorBill,
    RateVersion,
    Reading,
    Sample,
    SettlementRequest,
    SettlementRules,
)
from .timeutil import Interval, TimeError, parse_optional, parse_timestamp, weighted_average


def _mean_level(samples, window) -> Decimal:
    return weighted_average([(sample.interval, sample.kw) for sample in samples], window)


class ParseError(ValueError):
    """Raised when the payload as a whole cannot be interpreted."""


class RecordError(ValueError):
    """Raised when one record is invalid; other records still settle."""

    def __init__(self, message: str, *, location: str = "record") -> None:
        super().__init__(message)
        self.location = location


def _record_ok(raw: dict) -> bool:
    return isinstance(raw, dict)


def _get(raw: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def _require(raw: dict, *names: str) -> Any:
    value = _get(raw, *names)
    if value is None:
        raise RecordError(f"missing required field {names[0]!r}")
    return value


def _as_int(value: Any, *, field: str, default: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise RecordError(f"{field}: boolean is not an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RecordError(f"{field}: invalid integer {value!r}") from exc
    if result < 0:
        raise RecordError(f"{field}: must be >= 0")
    return result


def _parse_interval(
    raw: dict,
    *,
    tz: Optional[str],
    start_names: tuple[str, ...] = ("start",),
    end_names: tuple[str, ...] = ("end",),
    field: str = "window",
) -> Interval:
    start = parse_timestamp(_require(raw, *start_names), default_tz=tz, field=f"{field}.start")
    end = parse_timestamp(_require(raw, *end_names), default_tz=tz, field=f"{field}.end")
    return Interval(start, end)


def _parse_window_value(value: Any, *, tz: Optional[str], field: str) -> Interval:
    if not isinstance(value, dict):
        raise RecordError(f"{field}: window must be an object with start/end")
    start = parse_timestamp(_require(value, "start"), default_tz=tz, field=f"{field}.start")
    end = parse_timestamp(_require(value, "end"), default_tz=tz, field=f"{field}.end")
    return Interval(start, end)


def _parse_sample(
    raw: dict,
    *,
    tz: Optional[str],
    default_duration_minutes: Optional[int],
    field: str,
    kw_names: tuple[str, ...] = ("kw", "value"),
) -> Sample:
    start = parse_timestamp(_require(raw, "at", "start"), default_tz=tz, field=f"{field}.at")
    end_raw = _get(raw, "until", "end")
    if end_raw is not None:
        end = parse_timestamp(end_raw, default_tz=tz, field=f"{field}.end")
    elif default_duration_minutes is not None:
        end = start + timedelta(minutes=int(default_duration_minutes))
    else:
        raise RecordError(f"{field}: missing end/until and no sample duration configured")
    kw = dec(_require(raw, *kw_names), field=f"{field}.kw")
    return Sample(
        interval=Interval(start, end),
        kw=kw,
        sample_id=str(_get(raw, "id", "sample_id", default="")).strip() or None,
        received_at=parse_optional(_get(raw, "received_at", "received"), tz=tz, field=f"{field}.received_at"),
        version=_as_int(_get(raw, "version"), field=f"{field}.version"),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def load_records(payload: dict) -> list[dict]:
    """Backwards-compatible helper: return validated reading rows.

    The legacy API returned one normalised row per reading.  It is retained for
    callers of the original module while the settlement engine uses
    :func:`parse_payload`.
    """

    if not isinstance(payload, dict):
        raise ParseError("payload must be an object")
    readings = payload.get("readings", [])
    if not isinstance(readings, list):
        raise ParseError("readings must be a list")
    rows = [dict(row) for row in readings if isinstance(row, dict)]
    for row in rows:
        if "at" not in row:
            raise ParseError("reading row missing 'at'")
        parse_timestamp(row["at"], default_tz=payload.get("timezone"), field="readings.at")
        dec(row.get("kw"), field="readings.kw")
    return rows


def is_event_level(payload: dict) -> bool:
    return bool(
        isinstance(payload, dict)
        and (
            int(payload.get("schema_version", 1) or 1) >= 2
            or any(key in payload for key in ("resources", "events", "event_versions", "eligibilities"))
        )
    )


def parse_payload(payload: dict) -> SettlementRequest:
    if not isinstance(payload, dict):
        raise ParseError("payload must be an object")
    if is_event_level(payload):
        return _parse_event_level(payload)
    return _parse_legacy(payload)


# ---------------------------------------------------------------------------
# Legacy adapter
# ---------------------------------------------------------------------------


def _parse_legacy(payload: dict) -> SettlementRequest:
    tz = payload.get("timezone")
    rows = load_records(payload)
    event_raw = payload.get("event") or {}
    if not isinstance(event_raw, dict):
        raise ParseError("event must be an object")
    if not rows:
        raise ParseError("legacy payload requires at least one reading")

    ordered = sorted(rows, key=lambda row: parse_timestamp(row["at"], default_tz=tz, field="readings.at"))
    event_start = parse_timestamp(
        _get(event_raw, "start", default=ordered[-1]["at"]),
        default_tz=tz,
        field="event.start",
    )
    event_end = parse_optional(_get(event_raw, "end"), tz=tz, field="event.end")
    sample_end = parse_timestamp(
        _get(event_raw, "sample_end", default=None) or _fallback_sample_end(ordered, tz),
        default_tz=tz,
        field="event.end",
    )
    event_end = event_end or sample_end
    window = Interval(event_start, event_end)

    baseline_rows = max(1, _as_int(_get(event_raw, "baseline_window"), field="event.baseline_window", default=1))
    event_points = [row for row in ordered if parse_timestamp(row["at"], default_tz=tz) < event_start]
    baseline_rows = min(baseline_rows, max(1, len(event_points)))
    baseline_window = payload.get("baseline_window")
    if isinstance(baseline_window, dict):
        baseline_interval = _parse_window_value(baseline_window, tz=tz, field="baseline_window")
    else:
        selected = event_points[-baseline_rows:]
        baseline_interval = Interval(
            parse_timestamp(selected[0]["at"], default_tz=tz),
            parse_timestamp(selected[-1]["at"], default_tz=tz) + timedelta(minutes=5),
        )

    rate_value = dec(_get(event_raw, "rate", default=0), field="event.rate")
    currency = str(payload.get("currency", "USD"))
    capacity_rate = dec(
        _get(event_raw, "capacity_rate", default=rate_value),
        field="event.capacity_rate",
    )

    event = Event(
        event_id=str(_get(event_raw, "id", "event_id", default="legacy-event")),
        version=1,
        window=window,
        timezone=str(tz or "UTC"),
        direction="reduce",
        status="active",
        capacity_kw=None,
        issued_at=None,
        baseline_window=baseline_interval,
        baseline_method="mean",
        raw=dict(event_raw),
    )

    resource_id = str(_get(event_raw, "resource_id", default=payload.get("resource_id", "legacy-resource")))
    eligibility = EligibilityWindow(
        eligibility_id="legacy-eligibility",
        resource_id=resource_id,
        window=Interval(window.start - timedelta(days=1), window.end + timedelta(days=1)),
        version=1,
        status="active",
        event_id=event.event_id,
    )

    baseline_samples: list[Sample] = []
    for index, row in enumerate(selected):
        start = parse_timestamp(row["at"], default_tz=tz)
        if index + 1 < len(selected):
            end = parse_timestamp(selected[index + 1]["at"], default_tz=tz)
        else:
            end = start + timedelta(minutes=5)
        baseline_samples.append(
            Sample(
                interval=Interval(start, end),
                kw=dec(row["kw"], field="readings.kw"),
                sample_id=str(row.get("id", f"reading-{index}")),
                received_at=None,
            )
        )

    baseline = BaselineVersion(
        baseline_id="legacy-baseline",
        resource_id=resource_id,
        version=1,
        method="mean",
        status="active",
        issued_at=baseline_interval.start,
        effective_at=baseline_interval.start,
        samples=tuple(baseline_samples),
        event_id=event.event_id,
        window=baseline_interval,
        representation="scalar",
        level_kw=_mean_level(baseline_samples, baseline_interval),
        raw={"derived_from": "readings"},
    )

    executions = tuple(
        ExecutionSample(
            sample_id=str(row.get("id", f"reading-{index}")),
            resource_id=resource_id,
            interval=Interval(
                parse_timestamp(row["at"], default_tz=tz),
                parse_timestamp(row["at"], default_tz=tz) + timedelta(minutes=5),
            ),
            kw=dec(row["kw"], field="readings.kw"),
            instruction_id=None,
            received_at=None,
        )
        for index, row in enumerate(rows)
        if parse_timestamp(row["at"], default_tz=tz) >= event_start
    )

    instruction = DispatchInstruction(
        instruction_id="legacy-dispatch",
        event_id=event.event_id,
        resource_id=resource_id,
        version=1,
        issued_at=window.start - timedelta(hours=1),
        window=window,
        direction="reduce",
        capacity_kw=dec(_get(event_raw, "capacity_kw", default=0), field="event.capacity_kw"),
        status="active",
    )

    rate = RateVersion(
        rate_version_id="legacy-rate",
        tariff_id="legacy-tariff",
        version=1,
        window=Interval(datetime(1970, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)),
        issued_at=window.start - timedelta(days=1),
        status="active",
        capacity_rate_per_kw_hour=capacity_rate,
        energy_rate_per_kwh=rate_value,
        deviation_rate_per_kwh=rate_value,
        energy_rate_basis="kw",
        currency=currency,
        direction="both",
        priority=0,
    )

    readings = tuple(
        Reading(
            reading_id=str(row.get("id", f"reading-{index}")),
            resource_id=resource_id,
            sample=Sample(
                interval=Interval(
                    parse_timestamp(row["at"], default_tz=tz),
                    parse_timestamp(row["at"], default_tz=tz) + timedelta(minutes=5),
                ),
                kw=dec(row["kw"], field="readings.kw"),
            ),
        )
        for index, row in enumerate(rows)
    )

    rules = SettlementRules(
        baseline_policy="hybrid",
        baseline_policy_on_gap="prorate",
        min_baseline_coverage=Decimal("0.5"),
        allow_stacking_instructions=True,
        rate_conflict_policy="priority",
        # The legacy contract is delivered energy x rate.  The event-level
        # engine must not silently add capacity or shortfall components to it.
        shortfall_penalty=False,
        notes=("legacy-input-adapter",),
    )
    request = SettlementRequest(
        events=(event,),
        eligibility=(eligibility,),
        baselines=(baseline,),
        instructions=(instruction,),
        executions=executions,
        readings=readings,
        rates=(rate,),
        rules=rules,
        as_of=None,
    )
    return _with_dataset_fingerprint(request)


def _fallback_sample_end(rows: list[dict], tz: Optional[str]) -> str:
    times = sorted(parse_timestamp(row["at"], default_tz=tz) for row in rows)
    if len(times) >= 2:
        step = times[-1] - times[-2]
    else:
        step = timedelta(minutes=5)
    return (times[-1] + step).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Event-level schema
# ---------------------------------------------------------------------------


def _parse_event_level(payload: dict) -> SettlementRequest:
    tz_default = payload.get("timezone")
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    rules = _parse_rules(payload.get("rules") or payload.get("settlement_rules") or {})
    as_of = parse_optional(payload.get("as_of"), tz=tz_default, field="as_of")
    if as_of is None and payload.get("revision_of") is not None:
        # A recalculation must pin its visibility cutoff; consulting the wall
        # clock here would make the output non-reproducible.
        raise ParseError("revision_of requires an explicit as_of instant")

    events = _parse_events(payload, tz_default, issues)
    resources = _parse_resources(payload)
    eligibility = _parse_eligibility(payload, tz_default, issues)
    baselines = _parse_baselines(payload, tz_default, issues, warnings)
    instructions = _parse_instructions(payload, tz_default, issues)
    executions = _parse_executions(payload, tz_default, issues)
    readings = _parse_readings(payload, tz_default, issues)
    rates = _parse_rates(payload, tz_default, issues, warnings)
    corrections = _parse_corrections(payload, tz_default, issues)
    prior_bills = _parse_prior_bills(payload, issues)

    if not events:
        raise ParseError("event-level payload requires at least one event")

    _validate_references(
        events, eligibility, baselines, instructions, executions, rates, corrections, issues
    )

    request = SettlementRequest(
        events=tuple(events),
        eligibility=tuple(eligibility),
        baselines=tuple(baselines),
        instructions=tuple(instructions),
        executions=tuple(executions),
        readings=tuple(readings),
        rates=tuple(rates),
        corrections=tuple(corrections),
        prior_bills=tuple(prior_bills),
        resources=tuple(resources.values()),
        rules=rules,
        as_of=as_of,
        warnings=tuple(warnings),
        parse_issues=tuple(issues),
    )
    return _with_dataset_fingerprint(request)


def _parse_rules(raw: dict) -> SettlementRules:
    if not isinstance(raw, dict):
        raise ParseError("rules must be an object")
    baseline = raw.get("performance_bands") or raw.get("performance_thresholds") or []
    bands = tuple(_parse_band(item) for item in baseline) if isinstance(baseline, list) else ()

    def text(name: str, default: str) -> str:
        value = raw.get(name, default)
        return str(value) if value is not None else default

    def flag(name: str, default: bool) -> bool:
        value = raw.get(name, default)
        return bool(value)

    coverage = raw.get("min_baseline_coverage", raw.get("minimum_baseline_coverage", 0.95))
    try:
        coverage_dec = dec(coverage, field="rules.min_baseline_coverage")
    except AmountError as exc:
        raise ParseError(str(exc)) from exc
    if coverage_dec > 1:
        coverage_dec = coverage_dec / Decimal(100)

    return SettlementRules(
        rule_version=text("rule_version", "grid-flex-settlement.rules/2.0"),
        baseline_policy=text("baseline_policy", "hybrid"),
        honor_baseline_revisions=flag("honor_baseline_revisions", True),
        min_baseline_coverage=coverage_dec,
        coverage_policy=text("coverage_policy", text("baseline_policy_on_gap", "prorate")),
        baseline_policy_on_gap=text("baseline_policy_on_gap", text("coverage_policy", "prorate")),
        deviation_basis=text("deviation_basis", "execution"),
        rounding=text("rounding", "half_even"),
        split_at_local_midnight=flag("split_at_local_midnight", True),
        allow_stacking_instructions=flag("allow_stacking_instructions", False),
        performance_bands=bands,
        shortfall_penalty=flag("shortfall_penalty", True),
        credit_overdelivery=flag("credit_overdelivery", False),
        late_reading_policy=text("late_reading_policy", "exclude"),
        rate_conflict_policy=text("rate_conflict_policy", "error"),
        max_settlement_rounds=max(1, _as_int(raw.get("max_settlement_rounds"), field="rules.max_settlement_rounds", default=2)),
        notes=tuple(str(item) for item in raw.get("notes", []) or []),
    )


def _parse_band(raw: Any) -> PerformanceBand:
    if not isinstance(raw, dict):
        raise ParseError("performance band must be an object")
    return PerformanceBand(
        min_ratio=dec(_require(raw, "min_ratio", "min"), field="performance_band.min_ratio"),
        max_ratio=(
            dec(raw["max_ratio"], field="performance_band.max_ratio")
            if raw.get("max_ratio") is not None
            else None
        ),
        factor=dec(_require(raw, "factor", "multiplier"), field="performance_band.factor"),
        label=str(raw.get("label", "")) or None,
    )


def _parse_events(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[Event]:
    raw_events = payload.get("events") or payload.get("event_versions") or []
    if not isinstance(raw_events, list):
        raise ParseError("events must be a list")
    events: list[Event] = []
    for index, raw in enumerate(raw_events):
        if not _record_ok(raw):
            issues.append(_issue("event", index, "record is not an object"))
            continue
        location = f"events[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            window = _parse_event_window(raw, tz, location)
            direction = str(_get(raw, "direction", "mode", default="reduce")).lower()
            if direction not in ("reduce", "increase"):
                raise RecordError(f"{location}.direction: expected reduce|increase, got {direction!r}")
            baseline_window = _get(raw, "baseline_window")
            baseline_interval = None
            if isinstance(baseline_window, dict):
                baseline_interval = _parse_window_value(baseline_window, tz=tz, field=f"{location}.baseline_window")
            elif isinstance(baseline_window, str):
                baseline_interval = _parse_window_value(
                    {"start": baseline_window, "end": _get(raw, "baseline_end")},
                    tz=tz,
                    field=f"{location}.baseline_window",
                )
            settlement_raw = _get(raw, "settlement_window")
            settlement_interval = (
                _parse_window_value(settlement_raw, tz=tz, field=f"{location}.settlement_window")
                if isinstance(settlement_raw, dict)
                else None
            )
            event = Event(
                event_id=str(_require(raw, "event_id", "id")),
                version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                window=window,
                timezone=tz,
                direction=direction,
                status=str(_get(raw, "status", default="active")),
                capacity_kw=(
                    dec(raw["capacity_kw"], field=f"{location}.capacity_kw")
                    if raw.get("capacity_kw") is not None
                    else None
                ),
                issued_at=parse_optional(_get(raw, "issued_at"), tz=tz, field=f"{location}.issued_at"),
                baseline_window=baseline_interval,
                settlement_window=settlement_interval,
                baseline_lookback_minutes=(
                    _as_int(_get(raw, "baseline_lookback_minutes"), field=f"{location}.baseline_lookback_minutes")
                    if _get(raw, "baseline_lookback_minutes") is not None
                    else None
                ),
                baseline_method=str(_get(raw, "baseline_method", default="mean")),
                rate_version_id=_get(raw, "rate_version_id", "rate_id"),
                raw=dict(raw),
            )
            events.append(event)
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("event", index, str(exc), event_id=_get(raw, "event_id", "id")))
    return events


def _parse_event_window(raw: dict, tz: str, location: str) -> Interval:
    window = raw.get("window")
    if isinstance(window, dict):
        return _parse_window_value(window, tz=tz, field=f"{location}.window")
    if window is not None and not isinstance(window, dict):
        raise RecordError(f"{location}.window must be an object")
    start = parse_timestamp(_require(raw, "start"), default_tz=tz, field=f"{location}.start")
    end_raw = _get(raw, "end")
    if end_raw is None:
        duration = _get(raw, "duration_minutes", "duration")
        if duration is None:
            raise RecordError(f"{location}: missing end or duration")
        end = start + timedelta(minutes=int(duration))
    else:
        end = parse_timestamp(end_raw, default_tz=tz, field=f"{location}.end")
    return Interval(start, end)


def _parse_resources(payload: dict) -> dict[str, dict]:
    raw = payload.get("resources") or []
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if not isinstance(raw, list):
        raise ParseError("resources must be a list or object")
    result: dict[str, dict] = {}
    for item in raw:
        if isinstance(item, dict) and item.get("resource_id") is not None:
            result[str(item["resource_id"])] = dict(item)
    return result


def _parse_eligibility(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[EligibilityWindow]:
    raw_items = payload.get("eligibility") or payload.get("eligibility_windows") or payload.get("eligibilities") or []
    if not isinstance(raw_items, list):
        raise ParseError("eligibility must be a list")
    result: list[EligibilityWindow] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("eligibility", index, "record is not an object"))
            continue
        location = f"eligibility[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            window = (
                _parse_window_value(raw["window"], tz=tz, field=f"{location}.window")
                if isinstance(raw.get("window"), dict)
                else _parse_interval(raw, tz=tz, field=location)
            )
            result.append(
                EligibilityWindow(
                    eligibility_id=str(_require(raw, "eligibility_id", "id")),
                    resource_id=str(_require(raw, "resource_id")),
                    window=window,
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    status=str(_get(raw, "status", default="active")),
                    event_id=_get(raw, "event_id"),
                    issued_at=parse_optional(_get(raw, "issued_at"), tz=tz, field=f"{location}.issued_at"),
                    raw=dict(raw),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("eligibility", index, str(exc), resource_id=_get(raw, "resource_id")))
    return result


def _parse_baselines(
    payload: dict, tz_default: Optional[str], issues: list[dict], warnings: list[dict]
) -> list[BaselineVersion]:
    raw_items = payload.get("baselines") or payload.get("baseline_versions") or []
    if isinstance(raw_items, dict):
        raw_items = [dict(value, baseline_id=key) if isinstance(value, dict) else value for key, value in raw_items.items()]
    if not isinstance(raw_items, list):
        raise ParseError("baselines must be a list")
    result: list[BaselineVersion] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("baseline", index, "record is not an object"))
            continue
        location = f"baselines[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            samples_raw = raw.get("samples", raw.get("intervals", []))
            if not isinstance(samples_raw, list):
                raise RecordError(f"{location}.samples: must be a list")
            samples = _parse_baseline_samples(samples_raw, tz=tz, location=location, default_method=str(_get(raw, "method", default="mean")))
            supersedes = _parse_supersedes(_get(raw, "supersedes"))
            status = str(_get(raw, "status", default="active"))
            method = str(_get(raw, "method", default="mean"))
            representation = str(
                _get(
                    raw,
                    "representation",
                    "shape",
                    default="scalar" if method in ("mean", "average", "scalar", "regression", "day-type") else "profile",
                )
            ).lower()
            if representation not in ("profile", "scalar"):
                raise RecordError(f"{location}.representation: expected profile|scalar, got {representation!r}")
            level_kw = _get(raw, "level_kw", "baseline_kw", "value_kw")
            baseline_window_raw = _get(raw, "window", "baseline_window")
            baseline_window = (
                _parse_window_value(baseline_window_raw, tz=tz, field=f"{location}.window")
                if isinstance(baseline_window_raw, dict)
                else None
            )
            if representation == "scalar":
                if level_kw is not None:
                    level_kw = dec(level_kw, field=f"{location}.level_kw")
                elif samples:
                    if baseline_window is None:
                        baseline_window = Interval(
                            min(sample.interval.start for sample in samples),
                            max(sample.interval.end for sample in samples),
                        )
                    level_kw = _mean_level(samples, baseline_window)
                else:
                    raise RecordError(f"{location}: scalar baseline requires level_kw or samples")
            elif baseline_window is None and samples:
                baseline_window = Interval(
                    min(sample.interval.start for sample in samples),
                    max(sample.interval.end for sample in samples),
                )
            issued_at = parse_optional(_get(raw, "issued_at", "created_at"), tz=tz, field=f"{location}.issued_at")
            if issued_at is None:
                if payload.get("as_of"):
                    issued_at = parse_timestamp(payload["as_of"], default_tz=tz, field="as_of")
                else:
                    # Deterministic tie-break: no wall-clock time is consulted.
                    issued_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
                    warnings.append(
                        _warning(
                            "baseline_missing_issued_at",
                            location,
                            "issued_at defaulted to epoch for deterministic ordering",
                        )
                    )
            effective_at = parse_optional(
                _get(raw, "effective_at", "effective_from"),
                tz=tz,
                field=f"{location}.effective_at",
            )
            result.append(
                BaselineVersion(
                    baseline_id=str(_require(raw, "baseline_id", "id")),
                    resource_id=str(_require(raw, "resource_id")),
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    method=method,
                    status=status,
                    issued_at=issued_at,
                    effective_at=effective_at,
                    samples=tuple(samples),
                    event_id=_get(raw, "event_id"),
                    supersedes=supersedes,
                    window=baseline_window,
                    representation=representation,
                    level_kw=level_kw,
                    raw=dict(raw),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("baseline", index, str(exc), baseline_id=_get(raw, "baseline_id", "id")))
    return result


def _parse_baseline_samples(
    raw_samples: list, *, tz: str, location: str, default_method: str
) -> list[Sample]:
    samples: list[Sample] = []
    default_duration = 5
    for position, raw in enumerate(raw_samples):
        if not _record_ok(raw):
            raise RecordError(f"{location}.samples[{position}]: record is not an object")
        samples.append(
            _parse_sample(
                raw,
                tz=tz,
                default_duration_minutes=default_duration,
                field=f"{location}.samples[{position}]",
            )
        )
    return samples


def _parse_supersedes(value: Any) -> Optional[tuple[str, int]]:
    if value is None:
        return None
    if isinstance(value, str):
        return (value, 0)
    if isinstance(value, dict):
        return (str(_require(value, "id", "baseline_id", "rate_version_id")), _as_int(_get(value, "version"), field="supersedes.version", default=0))
    raise RecordError("supersedes must be a string or object")


def _parse_instructions(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[DispatchInstruction]:
    raw_items = payload.get("instructions") or payload.get("dispatch") or payload.get("dispatch_instructions") or []
    if not isinstance(raw_items, list):
        raise ParseError("instructions must be a list")
    result: list[DispatchInstruction] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("instruction", index, "record is not an object"))
            continue
        location = f"instructions[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            window = (
                _parse_window_value(raw["window"], tz=tz, field=f"{location}.window")
                if isinstance(raw.get("window"), dict)
                else _parse_interval(raw, tz=tz, field=location)
            )
            result.append(
                DispatchInstruction(
                    instruction_id=str(_require(raw, "instruction_id", "id")),
                    event_id=str(_require(raw, "event_id")),
                    resource_id=str(_require(raw, "resource_id")),
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    issued_at=parse_timestamp(
                        _require(raw, "issued_at", "created_at"),
                        default_tz=tz,
                        field=f"{location}.issued_at",
                    ),
                    window=window,
                    direction=str(_get(raw, "direction", default="reduce")).lower(),
                    capacity_kw=dec(_require(raw, "capacity_kw", "kw"), field=f"{location}.capacity_kw"),
                    status=str(_get(raw, "status", default="active")),
                    supersedes=_parse_supersedes(_get(raw, "supersedes", "supersedes_instruction")),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("instruction", index, str(exc), instruction_id=_get(raw, "instruction_id", "id")))
    return result


def _parse_executions(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[ExecutionSample]:
    raw_items = payload.get("executions") or payload.get("actual") or payload.get("execution_samples") or []
    if not isinstance(raw_items, list):
        raise ParseError("executions must be a list")
    result: list[ExecutionSample] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("execution", index, "record is not an object"))
            continue
        location = f"executions[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            if isinstance(raw.get("window"), dict):
                window = _parse_window_value(raw["window"], tz=tz, field=f"{location}.window")
            elif _get(raw, "start") is not None:
                window = _parse_interval(raw, tz=tz, field=location)
            else:
                start = parse_timestamp(_require(raw, "at"), default_tz=tz, field=f"{location}.at")
                window = Interval(start, start + timedelta(minutes=int(_get(raw, "minutes", default=5))))
            result.append(
                ExecutionSample(
                    sample_id=str(_get(raw, "sample_id", "id", default=f"execution-{index}")),
                    resource_id=str(_require(raw, "resource_id")),
                    interval=window,
                    kw=dec(_require(raw, "kw", "value"), field=f"{location}.kw"),
                    instruction_id=_get(raw, "instruction_id"),
                    received_at=parse_optional(_get(raw, "received_at", "received"), tz=tz, field=f"{location}.received_at"),
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("execution", index, str(exc), resource_id=_get(raw, "resource_id")))
    return result


def _parse_readings(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[Reading]:
    raw_items = payload.get("readings") or []
    if not isinstance(raw_items, list):
        raise ParseError("readings must be a list")
    result: list[Reading] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("reading", index, "record is not an object"))
            continue
        location = f"readings[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            if isinstance(raw.get("window"), dict):
                window = _parse_window_value(raw["window"], tz=tz, field=f"{location}.window")
            elif _get(raw, "start") is not None:
                window = _parse_interval(raw, tz=tz, field=location)
            else:
                start = parse_timestamp(_require(raw, "at"), default_tz=tz, field=f"{location}.at")
                window = Interval(start, start + timedelta(minutes=int(_get(raw, "minutes", default=5))))
            resource_id = str(_require(raw, "resource_id", "meter_id"))
            result.append(
                Reading(
                    reading_id=str(_get(raw, "reading_id", "id", default=f"reading-{index}")),
                    resource_id=resource_id,
                    sample=Sample(
                        interval=window,
                        kw=dec(_require(raw, "kw", "value"), field=f"{location}.kw"),
                        sample_id=str(_get(raw, "reading_id", "id", default=f"reading-{index}")),
                        received_at=parse_optional(_get(raw, "received_at", "received"), tz=tz, field=f"{location}.received_at"),
                        version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    ),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("reading", index, str(exc), resource_id=_get(raw, "resource_id", "meter_id")))
    return result


def _parse_rates(
    payload: dict, tz_default: Optional[str], issues: list[dict], warnings: list[dict]
) -> list[RateVersion]:
    raw_items = payload.get("rates") or payload.get("rate_versions") or payload.get("tariffs") or []
    if not isinstance(raw_items, list):
        raise ParseError("rates must be a list")
    result: list[RateVersion] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("rate", index, "record is not an object"))
            continue
        location = f"rates[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            window_raw = _get(raw, "window", "effective")
            if isinstance(window_raw, dict):
                window = _parse_window_value(window_raw, tz=tz, field=f"{location}.window")
            elif _get(raw, "effective_from") or _get(raw, "start"):
                window = Interval(
                    parse_timestamp(_get(raw, "effective_from", "start"), default_tz=tz, field=f"{location}.effective_from"),
                    parse_optional(_get(raw, "effective_to", "end"), tz=tz, field=f"{location}.effective_to")
                    or datetime(2100, 1, 1, tzinfo=timezone.utc),
                )
            else:
                raise RecordError(f"{location}: rate requires an effective window")
            bands = tuple(_parse_band(item) for item in raw.get("performance_bands", []) or [])
            status = str(_get(raw, "status", default="active"))
            issued_at = parse_optional(_get(raw, "issued_at", "created_at"), tz=tz, field=f"{location}.issued_at")
            if issued_at is None:
                issued_at = parse_timestamp(
                    _get(raw, "effective_from", "start", default="1970-01-01T00:00:00Z"),
                    default_tz=tz,
                    field=f"{location}.issued_at",
                )
                warnings.append(_warning("rate_missing_issued_at", location, "issued_at defaulted from effective window"))
            energy = _get(raw, "energy_rate_per_kwh", "energy_rate", "rate", default=0)
            deviation = _get(raw, "deviation_rate_per_kwh", "deviation_rate", default=energy)
            result.append(
                RateVersion(
                    rate_version_id=str(_require(raw, "rate_version_id", "rate_id", "id")),
                    tariff_id=str(_get(raw, "tariff_id", default="default")),
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    window=window,
                    issued_at=issued_at,
                    status=status,
                    capacity_rate_per_kw_hour=dec(_get(raw, "capacity_rate_per_kw_hour", "capacity_rate", default=0), field=f"{location}.capacity_rate_per_kw_hour"),
                    energy_rate_per_kwh=dec(energy, field=f"{location}.energy_rate_per_kwh"),
                    deviation_rate_per_kwh=dec(deviation, field=f"{location}.deviation_rate_per_kwh"),
                    energy_rate_basis=str(_get(raw, "energy_rate_basis", default="kwh")),
                    currency=str(_get(raw, "currency", default="USD")),
                    direction=str(_get(raw, "direction", default="both")),
                    capacity_rate_basis=str(_get(raw, "capacity_rate_basis", default="kw_hour")),
                    capacity_rate_per_kw_event=(
                        dec(raw["capacity_rate_per_kw_event"], field=f"{location}.capacity_rate_per_kw_event")
                        if raw.get("capacity_rate_per_kw_event") is not None
                        else None
                    ),
                    supersedes=_parse_supersedes(_get(raw, "supersedes")),
                    priority=_as_int(_get(raw, "priority"), field=f"{location}.priority", default=0),
                    performance_bands=bands,
                    raw=dict(raw),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("rate", index, str(exc), rate_version_id=_get(raw, "rate_version_id", "rate_id", "id")))
    return result


def _parse_corrections(payload: dict, tz_default: Optional[str], issues: list[dict]) -> list[Correction]:
    raw_items = payload.get("corrections") or payload.get("announcements") or []
    if not isinstance(raw_items, list):
        raise ParseError("corrections must be a list")
    result: list[Correction] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("correction", index, "record is not an object"))
            continue
        location = f"corrections[{index}]"
        try:
            tz = str(_get(raw, "timezone", default=tz_default or "UTC"))
            changes = _get(raw, "changes", default={}) or {}
            if not isinstance(changes, dict):
                raise RecordError(f"{location}.changes: must be an object")
            result.append(
                Correction(
                    correction_id=str(_require(raw, "correction_id", "id")),
                    event_id=str(_require(raw, "event_id")),
                    version=_as_int(_get(raw, "version"), field=f"{location}.version"),
                    issued_at=parse_timestamp(_require(raw, "issued_at", "created_at"), default_tz=tz, field=f"{location}.issued_at"),
                    status=str(_get(raw, "status", default="active")),
                    supersedes=_parse_supersedes(_get(raw, "supersedes")),
                    changes=dict(changes),
                )
            )
        except (RecordError, TimeError, AmountError) as exc:
            issues.append(_issue("correction", index, str(exc), correction_id=_get(raw, "correction_id", "id")))
    return result


def _parse_prior_bills(payload: dict, issues: list[dict]) -> list[PriorBill]:
    raw_items = payload.get("prior_bills") or payload.get("bills") or []
    if not isinstance(raw_items, list):
        raise ParseError("prior_bills must be a list")
    result: list[PriorBill] = []
    for index, raw in enumerate(raw_items):
        if not _record_ok(raw):
            issues.append(_issue("prior_bill", index, "record is not an object"))
            continue
        try:
            amount = _get(raw, "amount_cents")
            if amount is None:
                raise RecordError("prior_bill.amount_cents: required for a locked bill")
            result.append(
                PriorBill(
                    bill_id=str(_require(raw, "bill_id", "id")),
                    event_id=str(_require(raw, "event_id")),
                    resource_id=str(_require(raw, "resource_id")),
                    revision=_as_int(_get(raw, "revision"), field="prior_bill.revision", default=1),
                    status=str(_get(raw, "status", default="locked")),
                    amount_cents=int(amount),
                    currency=str(_get(raw, "currency", default="USD")),
                    fingerprint=_get(raw, "fingerprint"),
                )
            )
        except (RecordError, AmountError, TypeError, ValueError) as exc:
            issues.append(_issue("prior_bill", index, str(exc), bill_id=_get(raw, "bill_id", "id")))
    return result


def _dataset_fingerprint(request: SettlementRequest) -> str:
    """Order-independent identity of the parsed input set.

    Two feeds with the same records but a different JSON array order must hash
    the same, otherwise "same inputs, same output" is unprovable.
    """

    def sort_key(item: Any) -> str:
        return fingerprint(render_hint(item))

    def render_hint(item: Any) -> Any:
        for renderer in (
            _event_identity,
            _eligibility_identity,
            _baseline_identity,
            _instruction_identity,
            _execution_identity,
            _reading_identity,
            _rate_identity,
            _correction_identity,
            _bill_identity,
        ):
            try:
                rendered = renderer(item)
            except (AttributeError, TypeError):
                continue
            if rendered is not None:
                return rendered
        return repr(item)

    def ordered(items: Iterable[Any], render) -> list[Any]:
        return [render(item) for item in sorted(items, key=sort_key)]

    return fingerprint(
        {
            "events": ordered(request.events, _event_identity),
            "eligibility": ordered(request.eligibility, _eligibility_identity),
            "baselines": ordered(request.baselines, _baseline_identity),
            "instructions": ordered(request.instructions, _instruction_identity),
            "executions": ordered(request.executions, _execution_identity),
            "readings": ordered(request.readings, _reading_identity),
            "rates": ordered(request.rates, _rate_identity),
            "corrections": ordered(request.corrections, _correction_identity),
            "prior_bills": ordered(request.prior_bills, _bill_identity),
            "resources": sorted(
                ({"resource_id": item.get("resource_id"), **item} for item in request.resources),
                key=lambda item: str(item.get("resource_id")),
            ),
            "rules": request.rules.rule_version,
            "as_of": request.as_of,
        }
    )


def _event_identity(item: Event) -> dict[str, Any]:
    return {
        "id": item.event_id,
        "version": item.version,
        "window": [item.window.start, item.window.end],
        "timezone": item.timezone,
        "direction": item.direction,
        "status": item.status,
        "capacity_kw": item.capacity_kw,
        "issued_at": item.issued_at,
        "baseline_window": (
            [item.baseline_window.start, item.baseline_window.end] if item.baseline_window else None
        ),
        "settlement_window": (
            [item.settlement_window.start, item.settlement_window.end] if item.settlement_window else None
        ),
        "baseline_method": item.baseline_method,
        "rate_version_id": item.rate_version_id,
    }


def _eligibility_identity(item: EligibilityWindow) -> dict[str, Any]:
    return {
        "id": item.eligibility_id,
        "resource_id": item.resource_id,
        "event_id": item.event_id,
        "version": item.version,
        "status": item.status,
        "window": [item.window.start, item.window.end],
        "issued_at": item.issued_at,
    }


def _baseline_identity(item: BaselineVersion) -> dict[str, Any]:
    return {
        "id": item.baseline_id,
        "resource_id": item.resource_id,
        "event_id": item.event_id,
        "version": item.version,
        "method": item.method,
        "status": item.status,
        "issued_at": item.issued_at,
        "effective_at": item.effective_at,
        "supersedes": item.supersedes,
        "representation": item.representation,
        "level_kw": item.level_kw,
        "window": [item.window.start, item.window.end] if item.window else None,
        "samples": [
            {"start": sample.interval.start, "end": sample.interval.end, "kw": sample.kw}
            for sample in item.samples
        ],
    }


def _instruction_identity(item: DispatchInstruction) -> dict[str, Any]:
    return {
        "id": item.instruction_id,
        "event_id": item.event_id,
        "resource_id": item.resource_id,
        "version": item.version,
        "issued_at": item.issued_at,
        "window": [item.window.start, item.window.end],
        "direction": item.direction,
        "capacity_kw": item.capacity_kw,
        "status": item.status,
        "supersedes": item.supersedes,
    }


def _execution_identity(item: ExecutionSample) -> dict[str, Any]:
    return {
        "id": item.sample_id,
        "resource_id": item.resource_id,
        "interval": [item.interval.start, item.interval.end],
        "kw": item.kw,
        "instruction_id": item.instruction_id,
        "received_at": item.received_at,
        "version": item.version,
    }


def _reading_identity(item: Reading) -> dict[str, Any]:
    return {
        "id": item.reading_id,
        "resource_id": item.resource_id,
        "interval": [item.sample.interval.start, item.sample.interval.end],
        "kw": item.sample.kw,
        "received_at": item.sample.received_at,
        "version": item.sample.version,
    }


def _rate_identity(item: RateVersion) -> dict[str, Any]:
    return {
        "id": item.rate_version_id,
        "tariff_id": item.tariff_id,
        "version": item.version,
        "window": [item.window.start, item.window.end],
        "issued_at": item.issued_at,
        "status": item.status,
        "capacity_rate_per_kw_hour": item.capacity_rate_per_kw_hour,
        "capacity_rate_per_kw_event": item.capacity_rate_per_kw_event,
        "capacity_rate_basis": item.capacity_rate_basis,
        "energy_rate_per_kwh": item.energy_rate_per_kwh,
        "energy_rate_basis": item.energy_rate_basis,
        "deviation_rate_per_kwh": item.deviation_rate_per_kwh,
        "currency": item.currency,
        "direction": item.direction,
        "supersedes": item.supersedes,
        "priority": item.priority,
        "performance_bands": [(band.min_ratio, band.max_ratio, band.factor) for band in item.performance_bands],
    }


def _correction_identity(item: Correction) -> dict[str, Any]:
    return {
        "id": item.correction_id,
        "event_id": item.event_id,
        "version": item.version,
        "issued_at": item.issued_at,
        "status": item.status,
        "supersedes": item.supersedes,
        "changes": item.changes,
    }


def _bill_identity(item: PriorBill) -> dict[str, Any]:
    return {
        "id": item.bill_id,
        "event_id": item.event_id,
        "resource_id": item.resource_id,
        "revision": item.revision,
        "status": item.status,
        "amount_cents": item.amount_cents,
        "currency": item.currency,
        "fingerprint": item.fingerprint,
    }


def _with_dataset_fingerprint(request: SettlementRequest) -> SettlementRequest:
    from dataclasses import replace

    return replace(request, payload_fingerprint=_dataset_fingerprint(request))


def _validate_references(
    events: list[Event],
    eligibility: list[EligibilityWindow],
    baselines: list[BaselineVersion],
    instructions: list[DispatchInstruction],
    executions: list[ExecutionSample],
    rates: list[RateVersion],
    corrections: list[Correction],
    issues: list[dict],
) -> None:
    event_ids = {event.event_id for event in events}
    rate_ids = {rate.rate_version_id for rate in rates}
    for index, item in enumerate(eligibility):
        if item.event_id and item.event_id not in event_ids:
            issues.append(_issue("eligibility", index, f"unknown event_id {item.event_id!r}", resource_id=item.resource_id))
    for index, item in enumerate(instructions):
        if item.event_id not in event_ids:
            issues.append(_issue("instruction", index, f"unknown event_id {item.event_id!r}", instruction_id=item.instruction_id))
    for index, event in enumerate(events):
        if event.rate_version_id and event.rate_version_id not in rate_ids:
            issues.append(_issue("event", index, f"unknown rate_version_id {event.rate_version_id!r}", event_id=event.event_id))
    for index, item in enumerate(corrections):
        if item.event_id not in event_ids:
            issues.append(_issue("correction", index, f"unknown event_id {item.event_id!r}", correction_id=item.correction_id))


def _issue(kind: str, index: int, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"record_type": kind, "index": index, "message": message, "severity": "error"}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _warning(code: str, location: str, message: str) -> dict[str, Any]:
    return {"code": code, "location": location, "message": message, "severity": "warning"}

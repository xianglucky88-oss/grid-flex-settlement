"""Version resolution and correction propagation.

Nothing in this module performs monetary arithmetic.  It decides *which
version* of each input is authoritative for a given ``as_of`` instant, and it
records the evidence (version numbers, issue instants, fingerprints) that the
engine later embeds in every line item.

Rules implemented here:

* ``as_of`` is a hard visibility cutoff.  A version issued after ``as_of`` is
  unknown to that settlement run, which is what makes an "as of" recalculation
  reproducible.
* Supersede chains and explicit ``revoked``/``withdrawn``/``cancelled`` statuses
  hide older versions.  A revoked baseline is *not* silently replaced by an
  older one, and never by in-event meter readings.
* Duplicate dispatch instructions with identical content collapse to one.  The
  same identity with conflicting content is an error, not a coin flip.
* Conflicting rate versions over the same instant are an error unless an
  explicit priority policy is configured.
* Corrections are applied in ``(version, issued_at, correction_id)`` order and
  may change an event's window, direction, capacity, rate reference, or revoke
  it entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from .canon import fingerprint
from .models import (
    BaselineVersion,
    Correction,
    DispatchInstruction,
    EligibilityWindow,
    Event,
    ExecutionSample,
    PriorBill,
    RateVersion,
    Reading,
    SettlementRequest,
    SettlementRules,
    Sample,
)
from .timeutil import Interval, merge_intervals


@dataclass(frozen=True)
class CapacityOverride:
    capacity_kw: Decimal
    correction_id: str
    version: int
    issued_at: datetime


@dataclass(frozen=True)
class ResolvedEvent:
    event: Event
    effective_window: Interval
    applied_corrections: tuple[dict[str, Any], ...] = ()
    revoked: bool = False
    revocation_reason: Optional[str] = None
    capacity_override: Optional[CapacityOverride] = None

    def capacity_for(self, instruction: DispatchInstruction) -> tuple[Decimal, Optional[CapacityOverride]]:
        """Committed capacity for a resource.

        A correction issued after the dispatch instruction revises the event's
        committed capacity and therefore supersedes the instruction's nominal
        capacity.  An earlier correction does not reach back over a later
        instruction.  The override is returned so the line can record it.
        """

        override = self.capacity_override
        if override is None:
            return instruction.capacity_kw, None
        if override.issued_at <= instruction.issued_at:
            return instruction.capacity_kw, None
        return override.capacity_kw, override

    @property
    def version_key(self) -> dict[str, Any]:
        return {
            "event_id": self.event.event_id,
            "version": self.event.version,
            "source_window": [self.event.window.start, self.event.window.end],
            "effective_window": [self.effective_window.start, self.effective_window.end],
            "fingerprint": fingerprint(_event_payload(self.event)),
            "corrections": list(self.applied_corrections),
            "capacity_override": (
                {
                    "capacity_kw": self.capacity_override.capacity_kw,
                    "correction_id": self.capacity_override.correction_id,
                    "version": self.capacity_override.version,
                    "issued_at": self.capacity_override.issued_at,
                }
                if self.capacity_override
                else None
            ),
        }


@dataclass(frozen=True)
class BaselineSelection:
    source: str  # materialized | dynamic | legacy
    version: Optional[BaselineVersion]
    samples: tuple[Sample, ...]
    window: Interval  # reference window: where the level was derived
    coverage: Decimal
    covered_seconds: int
    total_seconds: int
    gaps: tuple[Interval, ...]
    reason: Optional[str] = None
    substituted: bool = False
    level_kw: Optional[Decimal] = None
    representation: str = "profile"  # profile | scalar
    application_window: Optional[Interval] = None  # event window the level applies to
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def version_label(self) -> Optional[str]:
        if self.version is None:
            return None
        return f"{self.version.baseline_id}@v{self.version.version}"

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "source": self.source,
                "version": self.version_label,
                "representation": self.representation,
                "level_kw": self.level_kw,
                "samples": [
                    {
                        "start": sample.interval.start,
                        "end": sample.interval.end,
                        "kw": sample.kw,
                        "id": sample.sample_id,
                    }
                    for sample in self.samples
                ],
            }
        )

    def value_for(self, interval: Interval) -> Optional[Decimal]:
        """Baseline kW applicable to a settlement interval.

        A ``profile`` baseline carries samples aligned to the event window and
        is averaged over the requested interval.  A ``scalar`` baseline is a
        single level derived from the (earlier) baseline window and applied
        unchanged across the event.
        """

        if self.representation == "scalar":
            return self.level_kw
        from .timeutil import weighted_average

        covered = [sample for sample in self.samples if sample.interval.clip_to(interval) is not None]
        if not covered:
            return None
        return weighted_average([(sample.interval, sample.kw) for sample in covered], interval)

    @property
    def available(self) -> bool:
        if self.representation == "scalar":
            return self.level_kw is not None
        return bool(self.samples) and self.covered_seconds > 0


@dataclass(frozen=True)
class RateSegment:
    interval: Interval
    rate: RateVersion

    @property
    def label(self) -> str:
        return f"{self.rate.rate_version_id}@v{self.rate.version}"


@dataclass(frozen=True)
class InstructionSlot:
    instruction: DispatchInstruction
    interval: Interval
    capacity_kw: Decimal = Decimal(0)

    @property
    def label(self) -> str:
        return f"{self.instruction.instruction_id}@v{self.instruction.version}"


@dataclass(frozen=True)
class ResolutionIssues:
    errors: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()

    def merged(self, other: "ResolutionIssues") -> "ResolutionIssues":
        return ResolutionIssues(self.errors + other.errors, self.warnings + other.warnings)


def _issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "severity": "error", **extra}


def _warn(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "severity": "warning", **extra}


def _visible(issued_at: Optional[datetime], as_of: Optional[datetime]) -> bool:
    return as_of is None or issued_at is None or issued_at <= as_of


def _event_payload(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "version": event.version,
        "window": [event.window.start, event.window.end],
        "timezone": event.timezone,
        "direction": event.direction,
        "status": event.status,
        "capacity_kw": event.capacity_kw,
        "settlement_window": (
            [event.settlement_window.start, event.settlement_window.end] if event.settlement_window else None
        ),
        "baseline_window": (
            [event.baseline_window.start, event.baseline_window.end] if event.baseline_window else None
        ),
        "baseline_method": event.baseline_method,
        "rate_version_id": event.rate_version_id,
    }


# ---------------------------------------------------------------------------
# Events + corrections
# ---------------------------------------------------------------------------


def resolve_events(
    request: SettlementRequest, issues: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> dict[str, ResolvedEvent]:
    grouped: dict[str, list[Event]] = {}
    for event in request.events:
        grouped.setdefault(event.event_id, []).append(event)

    resolved: dict[str, ResolvedEvent] = {}
    for event_id in sorted(grouped):
        versions = [item for item in grouped[event_id] if _visible(item.issued_at, request.as_of)]
        if not versions:
            warnings.append(
                _warn("event_not_yet_issued", f"event {event_id!r} has no version visible at as_of", event_id=event_id)
            )
            continue
        latest = max(versions, key=lambda item: (item.version, _dt_key(item.issued_at), fingerprint(_event_payload(item))))
        resolved[event_id] = _apply_corrections(latest, request, issues, warnings)
    return resolved


def _apply_corrections(
    event: Event,
    request: SettlementRequest,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> ResolvedEvent:
    corrections = [
        item
        for item in request.corrections
        if item.event_id == event.event_id and _visible(item.issued_at, request.as_of)
    ]
    corrections.sort(key=lambda item: (item.version, _dt_key(item.issued_at), item.correction_id))

    revoked = event.status in ("revoked", "cancelled", "withdrawn")
    reason = f"event status {event.status!r}" if revoked else None
    applied: list[dict[str, Any]] = []
    current = event
    capacity_override: Optional[CapacityOverride] = None

    for correction in corrections:
        reference = correction.supersedes
        if reference is not None and reference[0] == correction.correction_id:
            if correction.version <= reference[1]:
                issues.append(
                    _issue(
                        "correction_version_not_newer",
                        f"correction {correction.correction_id} does not supersede an older version",
                        correction_id=correction.correction_id,
                    )
                )
                continue
        applied.append(
            {
                "correction_id": correction.correction_id,
                "version": correction.version,
                "issued_at": correction.issued_at,
                "status": correction.status,
                "fingerprint": fingerprint(
                    {
                        "id": correction.correction_id,
                        "event_id": correction.event_id,
                        "version": correction.version,
                        "status": correction.status,
                        "changes": correction.changes,
                    }
                ),
                "changes": correction.changes,
            }
        )
        if correction.status in ("withdrawn", "revoked", "retracted"):
            revoked = True
            reason = f"correction {correction.correction_id} withdrawn"
            continue
        if "capacity_kw" in (correction.changes or {}):
            from .decimalutil import dec

            capacity_override = CapacityOverride(
                capacity_kw=dec(correction.changes["capacity_kw"], field="correction.capacity_kw"),
                correction_id=correction.correction_id,
                version=correction.version,
                issued_at=correction.issued_at,
            )
        current = _apply_change_set(current, correction, request, warnings)
        if current.status in ("revoked", "cancelled", "withdrawn"):
            revoked = True
            reason = f"correction {correction.correction_id} set status {current.status!r}"

    effective_window = current.window
    return ResolvedEvent(
        event=current,
        effective_window=effective_window,
        applied_corrections=tuple(applied),
        revoked=revoked,
        revocation_reason=reason,
        capacity_override=capacity_override,
    )


def _apply_change_set(
    event: Event,
    correction: Correction,
    request: SettlementRequest,
    warnings: list[dict[str, Any]],
) -> Event:
    changes = correction.changes or {}
    data: dict[str, Any] = {
        "event_id": event.event_id,
        "version": event.version,
        "window": event.window,
        "timezone": event.timezone,
        "direction": event.direction,
        "status": event.status,
        "capacity_kw": event.capacity_kw,
        "issued_at": event.issued_at,
        "baseline_window": event.baseline_window,
        "settlement_window": event.settlement_window,
        "baseline_lookback_minutes": event.baseline_lookback_minutes,
        "baseline_method": event.baseline_method,
        "rate_version_id": event.rate_version_id,
        "raw": event.raw,
    }
    if "window" in changes:
        window = changes["window"]
        if not isinstance(window, dict) or "start" not in window or "end" not in window:
            warnings.append(
                _warn("correction_window_ignored", "correction window must be an object with start/end")
            )
        else:
            from .timeutil import parse_timestamp

            data["window"] = Interval(
                parse_timestamp(window["start"], default_tz=event.timezone),
                parse_timestamp(window["end"], default_tz=event.timezone),
            )
    if "settlement_window" in changes:
        window = changes["settlement_window"]
        if isinstance(window, dict) and "start" in window and "end" in window:
            from .timeutil import parse_timestamp

            data["settlement_window"] = Interval(
                parse_timestamp(window["start"], default_tz=event.timezone),
                parse_timestamp(window["end"], default_tz=event.timezone),
            )
    if "direction" in changes:
        data["direction"] = str(changes["direction"]).lower()
    if "status" in changes:
        data["status"] = str(changes["status"])
    if "capacity_kw" in changes:
        from .decimalutil import dec

        data["capacity_kw"] = dec(changes["capacity_kw"], field="correction.capacity_kw")
    if "rate_version_id" in changes or "rate_id" in changes:
        data["rate_version_id"] = changes.get("rate_version_id", changes.get("rate_id"))
    if "baseline_window" in changes and isinstance(changes["baseline_window"], dict):
        from .timeutil import parse_timestamp

        window = changes["baseline_window"]
        data["baseline_window"] = Interval(
            parse_timestamp(window["start"], default_tz=event.timezone),
            parse_timestamp(window["end"], default_tz=event.timezone),
        )
    return Event(**data)


def _dt_key(value: Optional[datetime]) -> float:
    return value.timestamp() if value is not None else float("-inf")


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def resolve_baseline(
    request: SettlementRequest,
    event: ResolvedEvent,
    resource_id: str,
    baseline_window: Interval,
    readings: Sequence[Reading],
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> BaselineSelection:
    """Select a baseline version, or derive one explicitly. Never uses in-event data."""

    rules = request.rules
    candidates = [
        item
        for item in request.baselines
        if item.resource_id == resource_id
        and item.event_id in (None, event.event.event_id)
        and _visible(item.issued_at, request.as_of)
    ]

    materialized, reason = _select_baseline_version(candidates, event, baseline_window, rules, issues, warnings)

    if rules.baseline_policy == "materialized":
        if materialized is None:
            issues.append(
                _issue(
                    "baseline_missing",
                    "materialized baseline policy requires an explicit baseline version",
                    event_id=event.event.event_id,
                    resource_id=resource_id,
                    detail=reason,
                )
            )
            return BaselineSelection(
                source="materialized",
                version=None,
                samples=(),
                window=baseline_window,
                coverage=Decimal(0),
                covered_seconds=0,
                total_seconds=baseline_window.duration_seconds,
                gaps=(baseline_window,),
                reason=reason,
                detail={"policy": "materialized"},
            )
        return _selection_from_version(materialized, baseline_window, "materialized", event, warnings)

    dynamic_samples, dynamic_reason = _derive_dynamic_baseline(readings, baseline_window, event)
    dynamic_coverage = _coverage(dynamic_samples, baseline_window)

    if rules.baseline_policy == "dynamic":
        if not dynamic_samples:
            issues.append(
                _issue(
                    "baseline_missing",
                    "dynamic baseline could not be derived from pre-event readings",
                    event_id=event.event.event_id,
                    resource_id=resource_id,
                    detail=dynamic_reason,
                )
            )
            return BaselineSelection(
                source="dynamic",
                version=None,
                samples=(),
                window=baseline_window,
                coverage=Decimal(0),
                covered_seconds=0,
                total_seconds=baseline_window.duration_seconds,
                gaps=(baseline_window,),
                reason=dynamic_reason,
                detail={"policy": "dynamic"},
            )
        return _selection_from_samples(
            dynamic_samples,
            baseline_window,
            "dynamic",
            None,
            event,
            warnings,
            reason=dynamic_reason,
            representation="scalar",
            level_kw=_level_from_samples(dynamic_samples, baseline_window),
            application_window=event.event.window,
        )

    # hybrid: prefer a usable materialized version, otherwise derive explicitly.
    if materialized is not None:
        selection = _selection_from_version(materialized, baseline_window, "materialized", event, warnings)
        if selection.coverage >= rules.min_baseline_coverage:
            return selection
        if dynamic_samples and dynamic_coverage > selection.coverage:
            warnings.append(
                _warn(
                    "baseline_dynamic_override",
                    "materialized baseline coverage is below threshold; dynamic pre-event baseline used",
                    event_id=event.event.event_id,
                    resource_id=resource_id,
                    materialized_coverage=str(selection.coverage),
                    dynamic_coverage=str(dynamic_coverage),
                )
            )
            derived = _selection_from_samples(
                dynamic_samples,
                baseline_window,
                "dynamic",
                None,
                event,
                warnings,
                reason="materialized baseline below coverage threshold",
                representation="scalar",
                level_kw=_level_from_samples(dynamic_samples, baseline_window),
                application_window=event.event.window,
            )
            derived_detail = dict(derived.detail)
            derived_detail["materialized_version"] = selection.version_label
            return BaselineSelection(
                source=derived.source,
                version=derived.version,
                samples=derived.samples,
                window=derived.window,
                coverage=derived.coverage,
                covered_seconds=derived.covered_seconds,
                total_seconds=derived.total_seconds,
                gaps=derived.gaps,
                reason=derived.reason,
                substituted=True,
                detail=derived_detail,
            )
        return selection

    if dynamic_samples:
        warnings.append(
            _warn(
                "baseline_dynamic_fallback",
                "no materialized baseline visible; baseline derived from pre-event readings",
                event_id=event.event.event_id,
                resource_id=resource_id,
                reason=reason,
            )
        )
        return _selection_from_samples(
            dynamic_samples, baseline_window, "dynamic", None, event, warnings, reason=reason
        )

    issues.append(
        _issue(
            "baseline_missing",
            "no baseline version or pre-event reading could establish a baseline",
            event_id=event.event.event_id,
            resource_id=resource_id,
            detail=reason or dynamic_reason,
        )
    )
    return BaselineSelection(
        source="none",
        version=None,
        samples=(),
        window=baseline_window,
        coverage=Decimal(0),
        covered_seconds=0,
        total_seconds=baseline_window.duration_seconds,
        gaps=(baseline_window,),
        reason=reason or dynamic_reason,
    )


def _select_baseline_version(
    candidates: Sequence[BaselineVersion],
    event: ResolvedEvent,
    baseline_window: Interval,
    rules: SettlementRules,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[Optional[BaselineVersion], Optional[str]]:
    """Pick the newest authoritative version per baseline id, then across ids."""

    grouped: dict[str, list[BaselineVersion]] = {}
    for item in candidates:
        grouped.setdefault(item.baseline_id, []).append(item)

    selected: list[BaselineVersion] = []
    for baseline_id in sorted(grouped):
        versions = sorted(
            grouped[baseline_id],
            key=lambda item: (item.version, _dt_key(item.issued_at), fingerprint(_baseline_payload(item))),
        )
        newest = versions[-1]

        # A supersede pointer from another record can hide the newest stored copy.
        superseding = [
            other
            for other in candidates
            if other.supersedes is not None
            and other.supersedes[0] == baseline_id
            and (other.supersedes[1] == 0 or other.supersedes[1] >= newest.version)
            and other.version > newest.version
            and other is not newest
        ]
        if superseding and rules.honor_baseline_revisions:
            newest = max(
                superseding,
                key=lambda item: (item.version, _dt_key(item.issued_at), fingerprint(_baseline_payload(item))),
            )
            warnings.append(
                _warn(
                    "baseline_superseded",
                    f"baseline {baseline_id} is superseded by {newest.baseline_id}@v{newest.version}",
                    baseline_id=baseline_id,
                    replacement=f"{newest.baseline_id}@v{newest.version}",
                )
            )

        if newest.status in ("revoked", "withdrawn", "void"):
            # Deliberately do not fall back to an older version: an overwritten
            # baseline is not a licence to reuse stale numbers silently.
            warnings.append(
                _warn(
                    "baseline_revoked",
                    f"newest baseline {baseline_id}@v{newest.version} is {newest.status}; not falling back to an older version",
                    baseline_id=baseline_id,
                )
            )
            continue
        if newest.status in ("draft", "proposed"):
            warnings.append(
                _warn(
                    "baseline_draft_ignored",
                    f"baseline {baseline_id}@v{newest.version} is {newest.status} and is not used",
                    baseline_id=baseline_id,
                )
            )
            continue
        selected.append(newest)

    if not selected:
        return None, "no active baseline version"

    selected.sort(
        key=lambda item: (
            item.version,
            _dt_key(item.issued_at),
            item.baseline_id,
            fingerprint(_baseline_payload(item)),
        )
    )
    best = selected[-1]
    tied = [
        item
        for item in selected
        if item.version == best.version and item.issued_at == best.issued_at and item.baseline_id != best.baseline_id
    ]
    if tied:
        issue = _issue(
            "baseline_conflict",
            "multiple distinct baselines have the same version and issue instant",
            baseline_ids=sorted({item.baseline_id for item in [best, *tied]}),
        )
        issues.append(issue)
        return None, "conflicting baseline versions"
    return best, None


def _baseline_payload(baseline: BaselineVersion) -> dict[str, Any]:
    return {
        "baseline_id": baseline.baseline_id,
        "resource_id": baseline.resource_id,
        "version": baseline.version,
        "method": baseline.method,
        "status": baseline.status,
        "issued_at": baseline.issued_at,
        "effective_at": baseline.effective_at,
        "event_id": baseline.event_id,
        "supersedes": baseline.supersedes,
        "samples": [
            {"start": sample.interval.start, "end": sample.interval.end, "kw": sample.kw} for sample in baseline.samples
        ],
    }


def _selection_from_version(
    baseline: BaselineVersion,
    window: Interval,
    source: str,
    event: ResolvedEvent,
    warnings: list[dict[str, Any]],
) -> BaselineSelection:
    sample_window = baseline.window or window
    selection = _selection_from_samples(
        baseline.samples,
        sample_window,
        source,
        baseline,
        event,
        warnings,
        reason=None,
        quality_window=window,
        level_kw=baseline.level_kw,
        representation=baseline.representation,
        application_window=event.event.window,
        raw_samples=baseline.samples,
    )
    if baseline.effective_at is not None and baseline.effective_at > event.event.window.start:
        warnings.append(
            _warn(
                "baseline_effective_after_event",
                "baseline becomes effective after the event starts",
                baseline_id=baseline.baseline_id,
                baseline_version=baseline.version,
            )
        )
    return selection


def _selection_from_samples(
    samples: Sequence[Sample],
    window: Interval,
    source: str,
    baseline: Optional[BaselineVersion],
    event: ResolvedEvent,
    warnings: list[dict[str, Any]],
    *,
    reason: Optional[str],
    quality_window: Optional[Interval] = None,
    level_kw: Optional[Decimal] = None,
    representation: str = "profile",
    application_window: Optional[Interval] = None,
    raw_samples: Optional[Sequence[Sample]] = None,
) -> BaselineSelection:
    """Build a selection from the samples that cover the reference window.

    ``samples`` are clipped to the reference window for level derivation;
    ``raw_samples`` (when given) is the original, unclipped set used to test
    profile coverage against the event window, which may extend past the
    reference window.
    """
    clipped: list[Sample] = []
    for sample in samples:
        overlap = sample.interval.clip_to(window)
        if overlap is None:
            continue
        clipped.append(
            Sample(
                interval=overlap,
                kw=sample.kw,
                sample_id=sample.sample_id,
                received_at=sample.received_at,
                version=sample.version,
            )
        )
    clipped.sort(key=lambda item: (item.interval.start, item.interval.end, item.sample_id or ""))
    if representation == "scalar" and level_kw is None and clipped:
        level_kw = _level_from_samples(clipped, window)

    # Coverage answers "how much of the period this baseline governs is
    # actually evidenced?"  A scalar level is evidenced over its reference
    # window (the derivation period).  A profile is evidenced over the event
    # window it is applied to, because gaps there mean unpriced intervals.
    assessment = quality_window or window
    if representation == "scalar" and level_kw is not None:
        if clipped:
            coverage, covered_seconds, gaps = _coverage_detail(clipped, window)
            if gaps:
                warnings.append(
                    _warn(
                        "baseline_gap",
                        "baseline samples do not cover the whole reference window",
                        event_id=event.event.event_id,
                        resource_id=baseline.resource_id if baseline else None,
                        coverage=str(coverage),
                        gaps=[gap.as_dict() for gap in gaps],
                    )
                )
        else:
            # A scalar level declared directly (no reference samples) is fully
            # evidenced by its own version; it is not a partial profile.
            coverage, covered_seconds, gaps = Decimal(1), window.duration_seconds, []
    else:
        # A profile is evaluated against the window it governs (the event
        # window).  Gaps there mean intervals with no reference to compare
        # against, which must not be silently paid.
        assessment = application_window or quality_window or window
        coverage_samples = list(raw_samples) if raw_samples is not None else clipped
        coverage, covered_seconds, gaps = _coverage_detail(coverage_samples, assessment)
        if gaps and source != "none":
            warnings.append(
                _warn(
                    "baseline_gap",
                    "baseline profile does not cover the whole event window",
                    event_id=event.event.event_id,
                    resource_id=baseline.resource_id if baseline else None,
                    coverage=str(coverage),
                    gaps=[gap.as_dict() for gap in gaps],
                )
            )
    return BaselineSelection(
        source=source,
        version=baseline,
        samples=tuple(clipped),
        window=window,
        coverage=coverage,
        covered_seconds=covered_seconds,
        total_seconds=assessment.duration_seconds,
        gaps=tuple(gaps),
        reason=reason,
        level_kw=level_kw,
        representation=representation,
        application_window=application_window,
        detail={
            "method": baseline.method if baseline else event.event.baseline_method,
            "sample_count": len(clipped),
            "quality_window": assessment.as_dict(),
        },
    )


def _level_from_samples(samples: Sequence[Sample], window: Interval) -> Optional[Decimal]:
    numerator = Decimal(0)
    seconds = 0
    for sample in samples:
        clipped = sample.interval.clip_to(window)
        if clipped is None:
            continue
        numerator += sample.kw * Decimal(clipped.duration_seconds)
        seconds += clipped.duration_seconds
    if seconds == 0:
        return None
    return numerator / Decimal(seconds)


def _derive_dynamic_baseline(
    readings: Sequence[Reading], window: Interval, event: ResolvedEvent
) -> tuple[list[Sample], str]:
    """Derive a baseline from readings strictly *before* the event starts.

    Readings that overlap the event window or its settlement window are never
    considered, even partially.  This is the safeguard against "missing
    baseline replaced by event readings".
    """

    guard = event.event.window.start
    if event.event.settlement_window is not None:
        guard = min(guard, event.event.settlement_window.start)
    samples: list[Sample] = []
    excluded = 0
    for reading in readings:
        sample = reading.sample
        if sample.interval.end > guard:
            excluded += 1
            continue
        overlap = sample.interval.clip_to(window)
        if overlap is None:
            continue
        samples.append(
            Sample(
                interval=overlap,
                kw=sample.kw,
                sample_id=reading.reading_id,
                received_at=sample.received_at,
                version=sample.version,
            )
        )
    samples.sort(key=lambda item: (item.interval.start, item.interval.end, item.sample_id or ""))
    reason = f"derived from {len(samples)} pre-event reading(s); {excluded} reading(s) excluded at/inside the event"
    return samples, reason


def _coverage(samples: Sequence[Sample], window: Interval) -> Decimal:
    return _coverage_detail(samples, window)[0]


def _coverage_detail(
    samples: Sequence[Sample], window: Interval
) -> tuple[Decimal, int, list[Interval]]:
    covered = merge_intervals(
        [sample.interval.clip_to(window) for sample in samples if sample.interval.clip_to(window) is not None]
    )
    covered_seconds = sum(item.duration_seconds for item in covered)
    total = window.duration_seconds
    if total <= 0:
        return Decimal(1), 0, []
    gaps: list[Interval] = []
    cursor = window.start
    for item in covered:
        if item.start > cursor:
            gaps.append(Interval(cursor, item.start))
        cursor = max(cursor, item.end)
    if cursor < window.end:
        gaps.append(Interval(cursor, window.end))
    return Decimal(covered_seconds) / Decimal(total), covered_seconds, gaps


# ---------------------------------------------------------------------------
# Exclusions: late and revoked readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordSelection:
    samples: tuple[Sample, ...]
    excluded: tuple[dict[str, Any], ...]
    sources: tuple[str, ...]

    @property
    def coverage_note(self) -> str:
        return f"{len(self.samples)} sample(s)"


def resolve_readings(
    readings: Sequence[Reading],
    resource_id: str,
    window: Interval,
    rules: SettlementRules,
    as_of: Optional[datetime],
    warnings: list[dict[str, Any]],
) -> RecordSelection:
    relevant: list[Reading] = []
    excluded: list[dict[str, Any]] = []
    for reading in readings:
        if reading.resource_id != resource_id:
            continue
        if reading.sample.interval.clip_to(window) is None:
            continue
        if (
            as_of is not None
            and reading.sample.received_at is not None
            and reading.sample.received_at > as_of
            and rules.late_reading_policy == "exclude"
        ):
            excluded.append(
                {
                    "reading_id": reading.reading_id,
                    "received_at": reading.sample.received_at,
                    "reason": "received_after_as_of",
                    "policy": rules.late_reading_policy,
                }
            )
            continue
        relevant.append(reading)

    # Version dedupe: for the same reading identity keep the highest version,
    # then the latest receipt.  Conflicting equal-version content is recorded.
    grouped: dict[str, list[Reading]] = {}
    for reading in relevant:
        grouped.setdefault(reading.reading_id, []).append(reading)
    selected: list[Sample] = []
    for reading_id in sorted(grouped):
        versions = sorted(
            grouped[reading_id],
            key=lambda item: (item.sample.version, _dt_key(item.sample.received_at), item.sample.kw),
        )
        best = versions[-1]
        same_version = [item for item in versions if item.sample.version == best.sample.version]
        if len({item.sample.kw for item in same_version}) > 1:
            warnings.append(
                _warn(
                    "reading_version_conflict",
                    f"reading {reading_id} has conflicting values at version {best.sample.version}",
                    reading_id=reading_id,
                )
            )
        overlap = best.sample.interval.clip_to(window)
        if overlap is None:
            continue
        selected.append(
            Sample(
                interval=overlap,
                kw=best.sample.kw,
                sample_id=best.reading_id,
                received_at=best.sample.received_at,
                version=best.sample.version,
            )
        )
    selected.sort(key=lambda item: (item.interval.start, item.interval.end, item.sample_id or ""))
    return RecordSelection(tuple(selected), tuple(excluded), tuple(sorted(grouped)))


def resolve_executions(
    executions: Sequence[ExecutionSample],
    window: Interval,
    rules: SettlementRules,
    as_of: Optional[datetime],
    warnings: list[dict[str, Any]],
) -> RecordSelection:
    excluded: list[dict[str, Any]] = []
    relevant: list[ExecutionSample] = []
    for sample in executions:
        if sample.interval.clip_to(window) is None:
            continue
        if (
            as_of is not None
            and sample.received_at is not None
            and sample.received_at > as_of
            and rules.late_reading_policy == "exclude"
        ):
            excluded.append(
                {
                    "sample_id": sample.sample_id,
                    "received_at": sample.received_at,
                    "reason": "received_after_as_of",
                    "policy": rules.late_reading_policy,
                }
            )
            continue
        relevant.append(sample)

    grouped: dict[str, list[ExecutionSample]] = {}
    for sample in relevant:
        grouped.setdefault(sample.sample_id, []).append(sample)
    selected: list[Sample] = []
    for sample_id in sorted(grouped):
        versions = sorted(
            grouped[sample_id],
            key=lambda item: (item.version, _dt_key(item.received_at), item.kw),
        )
        best = versions[-1]
        overlap = best.interval.clip_to(window)
        if overlap is None:
            continue
        selected.append(
            Sample(
                interval=overlap,
                kw=best.kw,
                sample_id=best.sample_id,
                received_at=best.received_at,
                version=best.version,
            )
        )
    selected.sort(key=lambda item: (item.interval.start, item.interval.end, item.sample_id or ""))

    # Overlapping execution samples with different values would double count.
    deduped: list[Sample] = []
    for sample in selected:
        collisions = [
            other
            for other in deduped
            if other.interval.overlaps(sample.interval) and other.kw != sample.kw
        ]
        if collisions:
            warnings.append(
                _warn(
                    "execution_overlap",
                    "overlapping execution samples disagree; higher received version wins per boundary",
                    sample_id=sample.sample_id,
                    collides_with=[item.sample_id for item in collisions],
                )
            )
        deduped.append(sample)
    return RecordSelection(tuple(selected), tuple(excluded), tuple(sorted(grouped)))


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def resolve_instructions(
    request: SettlementRequest,
    event_id: str,
    resource_id: str,
    window: Interval,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    event: Optional[ResolvedEvent] = None,
) -> tuple[tuple[InstructionSlot, ...], tuple[str, ...], tuple[dict[str, Any], ...]]:
    candidates = [
        item
        for item in request.instructions
        if item.event_id == event_id and item.resource_id == resource_id and _visible(item.issued_at, request.as_of)
    ]

    # Collapse exact duplicates first; same identity, different content is an error.
    unique: dict[str, DispatchInstruction] = {}
    duplicates: list[dict[str, Any]] = []
    for item in sorted(
        candidates,
        key=lambda value: (value.instruction_id, value.version, _dt_key(value.issued_at), fingerprint(_instruction_payload(value))),
    ):
        identity = f"{item.instruction_id}@v{item.version}"
        existing = unique.get(identity)
        if existing is None:
            unique[identity] = item
            continue
        if _instruction_payload(existing) == _instruction_payload(item):
            duplicates.append(
                {
                    "instruction_id": item.instruction_id,
                    "version": item.version,
                    "action": "collapsed_identical_duplicate",
                }
            )
        else:
            issues.append(
                _issue(
                    "instruction_conflict",
                    f"instruction {identity} has conflicting content",
                    instruction_id=item.instruction_id,
                    version=item.version,
                )
            )

    grouped: dict[str, list[DispatchInstruction]] = {}
    for item in unique.values():
        grouped.setdefault(item.instruction_id, []).append(item)

    active: list[DispatchInstruction] = []
    for instruction_id in sorted(grouped):
        versions = sorted(
            grouped[instruction_id],
            key=lambda item: (item.version, _dt_key(item.issued_at), fingerprint(_instruction_payload(item))),
        )
        newest = versions[-1]
        if newest.status in ("revoked", "cancelled", "withdrawn"):
            warnings.append(
                _warn(
                    "instruction_revoked",
                    f"instruction {instruction_id}@v{newest.version} is {newest.status}",
                    instruction_id=instruction_id,
                )
            )
            continue
        if newest.direction not in ("reduce", "increase"):
            issues.append(
                _issue(
                    "instruction_direction_invalid",
                    f"instruction {instruction_id} has unsupported direction {newest.direction!r}",
                )
            )
            continue
        active.append(newest)

    if not request.rules.allow_stacking_instructions and len(active) > 1:
        # Deterministic rule: the latest issued instruction wins; the rest are
        # recorded as suppressed rather than silently summed.
        active.sort(key=lambda item: (_dt_key(item.issued_at), item.instruction_id, item.version))
        suppressed = active[:-1]
        warnings.append(
            _warn(
                "instruction_stacking_suppressed",
                "multiple active instructions for one resource; latest issued wins",
                suppressed=[f"{item.instruction_id}@v{item.version}" for item in suppressed],
            )
        )
        active = [active[-1]]

    slots: list[InstructionSlot] = []
    for instruction in active:
        overlap = instruction.window.clip_to(window)
        if overlap is not None:
            capacity, _override = event.capacity_for(instruction) if event is not None else (instruction.capacity_kw, None)
            slots.append(
                InstructionSlot(instruction=instruction, interval=overlap, capacity_kw=capacity)
            )
    slots.sort(key=lambda item: (item.interval.start, item.interval.end, item.instruction.instruction_id))
    if not slots:
        warnings.append(
            _warn(
                "instruction_absent",
                "no active instruction covers the settlement window",
                event_id=event_id,
                resource_id=resource_id,
            )
        )
    return tuple(slots), tuple(sorted(unique)), tuple(duplicates)


def _instruction_payload(instruction: DispatchInstruction) -> dict[str, Any]:
    return {
        "instruction_id": instruction.instruction_id,
        "event_id": instruction.event_id,
        "resource_id": instruction.resource_id,
        "version": instruction.version,
        "issued_at": instruction.issued_at,
        "window": [instruction.window.start, instruction.window.end],
        "direction": instruction.direction,
        "capacity_kw": instruction.capacity_kw,
        "status": instruction.status,
        "supersedes": instruction.supersedes,
    }


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def resolve_rates(
    request: SettlementRequest,
    event: ResolvedEvent,
    window: Interval,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[tuple[RateSegment, ...], tuple[str, ...]]:
    candidates = [rate for rate in request.rates if _visible(rate.issued_at, request.as_of)]

    if event.event.rate_version_id:
        family = event.event.rate_version_id
        candidates = [rate for rate in candidates if rate.rate_version_id == family or rate.supersedes and rate.supersedes[0] == family]
        if not candidates:
            issues.append(
                _issue(
                    "rate_missing",
                    f"event references rate_version_id {family!r} but no visible version exists",
                    event_id=event.event.event_id,
                    rate_version_id=family,
                )
            )
            return (), ()

    # Collapse duplicate rate versions, honour supersedes chains, then split.
    by_identity: dict[tuple[str, int], RateVersion] = {}
    for rate in sorted(
        candidates,
        key=lambda item: (item.rate_version_id, item.version, _dt_key(item.issued_at), fingerprint(_rate_payload(item))),
    ):
        identity = (rate.rate_version_id, rate.version)
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = rate
        elif _rate_payload(existing) != _rate_payload(rate):
            issues.append(
                _issue(
                    "rate_version_conflict",
                    f"rate {identity[0]}@v{identity[1]} has conflicting content",
                    rate_version_id=identity[0],
                    version=identity[1],
                )
            )

    superseded: set[tuple[str, int]] = set()
    for identity, rate in by_identity.items():
        if rate.supersedes is None:
            continue
        superseded_id, superseded_version = rate.supersedes
        if superseded_version == 0:
            # Bare ``supersedes: "<id>"`` means "every earlier version of this id".
            for candidate_id, candidate_version in by_identity:
                if candidate_id == superseded_id and candidate_version < rate.version:
                    superseded.add((candidate_id, candidate_version))
        else:
            superseded.add((superseded_id, superseded_version))

    active = [
        rate
        for identity, rate in by_identity.items()
        if identity not in superseded
        and rate.status not in ("revoked", "withdrawn", "void", "draft", "proposed")
        and rate.window.overlaps(window)
        and _direction_compatible(rate.direction, event.event.direction)
    ]
    if not active:
        issues.append(
            _issue(
                "rate_unavailable",
                "no active rate version covers the settlement window",
                event_id=event.event.event_id,
                window=window.as_dict(),
            )
        )
        return (), ()

    # Split at every rate boundary so each segment has one clearly identified rate.
    boundaries = {window.start, window.end}
    for rate in active:
        if window.start < rate.window.start < window.end:
            boundaries.add(rate.window.start)
        if window.start < rate.window.end < window.end:
            boundaries.add(rate.window.end)
    points = sorted(boundaries)
    segments: list[RateSegment] = []
    for start, end in zip(points, points[1:]):
        if start >= end:
            continue
        segment = Interval(start, end)
        covering = [rate for rate in active if rate.window.contains(segment)]
        if not covering:
            issues.append(
                _issue(
                    "rate_gap",
                    "rate coverage has a gap",
                    event_id=event.event.event_id,
                    interval=segment.as_dict(),
                )
            )
            continue
        best = _pick_rate(covering, request.rules, issues, warnings, segment)
        if best is not None:
            segments.append(RateSegment(interval=segment, rate=best))
    return tuple(segments), tuple(sorted({rate.rate_version_id for rate in active}))


def _pick_rate(
    covering: list[RateVersion],
    rules: SettlementRules,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    segment: Interval,
) -> Optional[RateVersion]:
    if len(covering) == 1:
        return covering[0]
    top_priority = max(rate.priority for rate in covering)
    top = [rate for rate in covering if rate.priority == top_priority]
    distinct = {_rate_key(rate) for rate in top}
    if len(distinct) > 1:
        if rules.rate_conflict_policy != "priority":
            issues.append(
                _issue(
                    "rate_conflict",
                    "overlapping rate versions disagree at equal priority",
                    interval=segment.as_dict(),
                    rate_versions=[f"{rate.rate_version_id}@v{rate.version}" for rate in top],
                    policy=rules.rate_conflict_policy,
                )
            )
            return None
        warnings.append(
            _warn(
                "rate_conflict_resolved_by_priority",
                "equal-priority rate conflict resolved deterministically",
                interval=segment.as_dict(),
            )
        )
    top.sort(key=lambda rate: (_dt_key(rate.issued_at), rate.rate_version_id, rate.version))
    return top[-1]


def _rate_key(rate: RateVersion) -> tuple:
    return (
        rate.capacity_rate_per_kw_hour,
        rate.energy_rate_per_kwh,
        rate.deviation_rate_per_kwh,
        rate.currency,
    )


def _rate_payload(rate: RateVersion) -> dict[str, Any]:
    return {
        "rate_version_id": rate.rate_version_id,
        "tariff_id": rate.tariff_id,
        "version": rate.version,
        "window": [rate.window.start, rate.window.end],
        "issued_at": rate.issued_at,
        "status": rate.status,
        "capacity_rate_per_kw_hour": rate.capacity_rate_per_kw_hour,
        "capacity_rate_per_kw_event": rate.capacity_rate_per_kw_event,
        "capacity_rate_basis": rate.capacity_rate_basis,
        "energy_rate_per_kwh": rate.energy_rate_per_kwh,
        "deviation_rate_per_kwh": rate.deviation_rate_per_kwh,
        "currency": rate.currency,
        "direction": rate.direction,
        "supersedes": rate.supersedes,
        "priority": rate.priority,
        "performance_bands": [
            (band.min_ratio, band.max_ratio, band.factor) for band in rate.performance_bands
        ],
    }


def _direction_compatible(rate_direction: str, event_direction: str) -> bool:
    if rate_direction in ("both", "any", ""):
        return True
    return rate_direction == event_direction


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def resolve_eligibility(
    request: SettlementRequest,
    event_id: str,
    resource_id: str,
    window: Interval,
    warnings: list[dict[str, Any]],
) -> tuple[list[EligibilityWindow], bool]:
    """Return covering eligibility windows and whether any record was declared.

    The boolean distinguishes "this resource was never enrolled" from "it was
    enrolled, but not for this event window".  The two failures are audited
    differently: the first is missing configuration, the second is an
    out-of-scope dispatch.
    """

    declared = [
        item
        for item in request.eligibility
        if item.resource_id == resource_id
        and item.event_id in (None, event_id)
        and _visible(item.issued_at, request.as_of)
    ]
    candidates = [
        item
        for item in declared
        if item.status == "active" and item.window.overlaps(window)
    ]
    if not candidates:
        warnings.append(
            _warn(
                "eligibility_absent",
                "no active eligibility window covers the settlement window",
                event_id=event_id,
                resource_id=resource_id,
                declared=len(declared),
            )
        )
    return candidates, bool(declared)


# ---------------------------------------------------------------------------
# Prior bills
# ---------------------------------------------------------------------------


def resolve_prior_bill(
    request: SettlementRequest, event_id: str, resource_id: str
) -> tuple[Optional[PriorBill], tuple[PriorBill, ...]]:
    bills = sorted(
        [
            item
            for item in request.prior_bills
            if item.event_id == event_id and item.resource_id == resource_id
        ],
        key=lambda item: (item.revision, item.bill_id),
    )
    if not bills:
        return None, ()
    locked = [item for item in bills if item.status == "locked"]
    latest = locked[-1] if locked else bills[-1]
    return latest, tuple(bills)


def resolve_units(
    request: SettlementRequest,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Deterministic (event, resource) unit list from every resource-bearing input."""

    resources_by_event: dict[str, set[str]] = {event.event_id: set() for event in request.events if event.event_id}
    for item in request.eligibility:
        if item.event_id in resources_by_event:
            resources_by_event[item.event_id].add(item.resource_id)
    for item in request.baselines:
        if item.event_id in resources_by_event:
            resources_by_event[item.event_id].add(item.resource_id)
    for item in request.instructions:
        if item.event_id in resources_by_event:
            resources_by_event[item.event_id].add(item.resource_id)
    for item in request.executions:
        for event in request.events:
            if event.event_id in resources_by_event and event.window.overlaps(item.interval):
                resources_by_event[event.event_id].add(item.resource_id)
    for item in request.readings:
        for event in request.events:
            if event.event_id in resources_by_event and event.window.clip_to(item.sample.interval) is not None:
                resources_by_event[event.event_id].add(item.resource_id)
    for item in request.prior_bills:
        if item.event_id in resources_by_event:
            resources_by_event[item.event_id].add(item.resource_id)
    # A resource declared by the event operator is expected to settle even when
    # other records are missing, so its absence becomes an error unit rather
    # than a silent omission from the batch.
    for item in request.resources:
        resource_id = item.get("resource_id")
        if resource_id is None:
            continue
        for event in request.events:
            resources_by_event[event.event_id].add(str(resource_id))

    units: set[tuple[str, str]] = set()
    for event_id in sorted(resources_by_event):
        for resource_id in sorted(resources_by_event[event_id]):
            units.add((event_id, resource_id))
    return sorted(units)

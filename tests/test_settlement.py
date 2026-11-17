"""Event-level settlement: windows, components, provenance, and determinism."""

import json
import unittest
from decimal import Decimal

from grid_flex_settlement.canon import canonical_json
from grid_flex_settlement.decimalutil import allocate_minor, dec, format_money
from grid_flex_settlement.pipeline import run, settle_payload
from grid_flex_settlement.report import reconcile

try:  # pragma: no cover - import shim for both discovery styles
    from . import support
except ImportError:  # running with ``discover -s tests`` and tests/ on sys.path
    import support  # type: ignore[no-redef]


def _unit(result, key="e1/r1"):
    for unit in result["units"]:
        if unit["key"] == key:
            return unit
    raise AssertionError(f"unit {key} not found in {[u['key'] for u in result['units']]}")


def _component(unit, name):
    for comp in unit["components"]:
        if comp["name"] == name:
            return comp
    return None


class TestComponents(unittest.TestCase):
    def test_capacity_performance_and_deviation_are_separate(self):
        result = run(support.payload())
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        capacity = _component(unit, "capacity")
        performance = _component(unit, "performance")
        deviation = _component(unit, "deviation")
        # capacity: 50 kW * 1 h * 0.5
        self.assertEqual(capacity["amount_cents"], 2500)
        # performance: (100-60) kW * 1 h * 0.25
        self.assertEqual(performance["amount_cents"], 1000)
        # deviation: shortfall (50-40) kWh * 0.1, as a negative adjustment
        self.assertEqual(deviation["amount_cents"], -100)
        self.assertEqual(unit["final_cents"], 3400)

    def test_each_line_names_its_interval_and_versions(self):
        result = run(support.payload())
        unit = _unit(result)
        for component in unit["components"]:
            for line in component["lines"]:
                for key in (
                    "rate_version",
                    "baseline_version",
                    "instruction_version",
                    "rule_version",
                    "utc_interval",
                ):
                    self.assertTrue(line[key], f"missing {key} on {line['line_id']}")
                self.assertEqual(line["utc_interval"], ["2026-06-01T10:00:00Z", "2026-06-01T11:00:00Z"])

    def test_component_and_line_cents_reconcile(self):
        result = run(support.payload())
        unit = _unit(result)
        self.assertTrue(unit["components"])
        self.assertEqual(
            sum(component["amount_cents"] for component in unit["components"]),
            unit["final_cents"],
        )
        for component in unit["components"]:
            self.assertTrue(component["allocation_reconciles"])
            self.assertEqual(
                sum(line["amount_cents"] for line in component["lines"]),
                component["amount_cents"],
            )
        batch = settle_payload(support.payload())
        self.assertTrue(reconcile(batch)["balanced"])

    def test_negative_direction_propagates(self):
        payload = support.payload(
            events=[support.event(direction="increase")],
            instructions=[support.instruction(direction="increase")],
            rates=[support.rate(direction="increase")],
            readings=[support.reading(kw=140)],
        )
        result = run(payload)
        unit = _unit(result)
        # Increasing load above baseline is the delivered service.
        self.assertEqual(_component(unit, "performance")["amount_cents"], 1000)
        for component in unit["components"]:
            for line in component["lines"]:
                self.assertEqual(line["direction"], "increase")


class TestWindows(unittest.TestCase):
    def test_eligibility_event_and_settlement_windows_are_distinct(self):
        payload = support.payload(
            eligibility=[
                support.eligibility(
                    start="2026-06-01T00:00:00Z",
                    end="2026-06-01T10:30:00Z",
                    eligibility_id="first-half",
                )
            ],
            events=[
                support.event(
                    settlement_window={"start": "2026-06-01T10:00:00Z", "end": "2026-06-01T10:30:00Z"}
                )
            ],
            readings=[
                support.reading(start="2026-06-01T10:00:00Z", end="2026-06-01T10:30:00Z", kw=60),
            ],
            instructions=[
                support.instruction(start="2026-06-01T10:00:00Z", end="2026-06-01T10:30:00Z"),
            ],
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["windows"]["event"]["end"], "2026-06-01T11:00:00Z")
        self.assertEqual(unit["windows"]["settlement"]["end"], "2026-06-01T10:30:00Z")
        self.assertEqual(unit["windows"]["eligibility"]["end"], "2026-06-01T10:30:00Z")
        audit_windows = unit["audit"]["windows"]
        self.assertIn("relation", audit_windows)
        # Half an hour, so half the capacity and energy.
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 1250)
        self.assertEqual(_component(unit, "performance")["amount_cents"], 500)

    def test_settlement_outside_eligibility_is_an_error_not_zero(self):
        payload = support.payload(
            eligibility=[support.eligibility(start="2026-05-01T00:00:00Z", end="2026-05-02T00:00:00Z")]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertEqual(unit["final_cents"], 0)
        self.assertIn("outside_eligibility", {item["code"] for item in unit["errors"]})

    def test_partial_eligibility_is_settled_only_where_eligible(self):
        payload = support.payload(
            eligibility=[
                support.eligibility(start="2026-06-01T00:00:00Z", end="2026-06-01T10:30:00Z")
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertIn("settlement_partially_eligible", {item["code"] for item in unit["warnings"]})
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 1250)


class TestBaseline(unittest.TestCase):
    def test_missing_materialized_baseline_never_uses_event_readings(self):
        payload = support.payload(
            baselines=[],
            readings=[support.reading(start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=60)],
            rules={"baseline_policy": "materialized"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertEqual(unit["final_cents"], 0)
        self.assertIn("baseline_missing", {item["code"] for item in unit["errors"]})
        # Crucially, no line claims the event reading was a baseline.
        self.assertEqual(unit["components"], [])

    def test_revoked_newest_baseline_does_not_fall_back(self):
        payload = support.payload(
            baselines=[
                support.baseline(version=1, level_kw=100),
                support.baseline(version=2, level_kw=80, status="revoked", issued_at="2026-06-02T00:00:00Z"),
            ],
            rules={"baseline_policy": "materialized"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertEqual(unit["baseline"]["version"], None)

    def test_overwriting_baseline_does_not_silently_reuse_older_version(self):
        payload = support.payload(
            baselines=[
                support.baseline(version=1, level_kw=100),
                support.baseline(
                    version=2,
                    level_kw=90,
                    status="revoked",
                    issued_at="2026-06-02T00:00:00Z",
                ),
            ],
            rules={"baseline_policy": "materialized"},
        )
        unit = _unit(run(payload))
        self.assertEqual(unit["status"], "error")
        self.assertIsNone(unit["baseline"]["version"])
        self.assertEqual(
            [item["code"] for item in unit["errors"]].count("baseline_missing"), 1
        )

    def test_cross_id_supersede_selects_the_replacement(self):
        payload = support.payload(
            baselines=[
                support.baseline(baseline_id="b1", version=1, level_kw=100),
                support.baseline(
                    baseline_id="b2",
                    version=2,
                    level_kw=90,
                    issued_at="2026-06-02T00:00:00Z",
                    supersedes={"id": "b1", "version": 1},
                ),
            ],
            rules={"baseline_policy": "materialized"},
        )
        unit = _unit(run(payload))
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(unit["baseline"]["version"], "b2@v2")
        self.assertIn("baseline_superseded", {item["code"] for item in unit["warnings"]})

    def test_dynamic_baseline_uses_only_pre_event_readings(self):
        payload = support.payload(
            baselines=[],
            rules={"baseline_policy": "dynamic"},
            readings=[
                support.reading(reading_id="pre1", start="2026-06-01T09:00:00Z", end="2026-06-01T09:30:00Z", kw=100),
                support.reading(reading_id="pre2", start="2026-06-01T09:30:00Z", end="2026-06-01T10:00:00Z", kw=100),
                support.reading(reading_id="in1", start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=60),
            ],
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(unit["baseline"]["source"], "dynamic")
        self.assertEqual(unit["baseline"]["samples"], 2)
        # baseline 100, actual 60 -> 40 kW reduction
        self.assertEqual(unit["metrics"]["delivered_kwh"], "40")

    def test_baseline_gap_is_prorated_and_flagged(self):
        payload = support.payload(
            baselines=[
                support.baseline(
                    representation="profile",
                    samples=[
                        {"start": "2026-06-01T10:00:00Z", "end": "2026-06-01T10:30:00Z", "kw": 100},
                    ],
                )
            ],
            rules={"baseline_policy": "materialized"},
            readings=[support.reading(start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=60)],
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertIn("baseline_prorated", {item["code"] for item in unit["warnings"]})
        capacity = _component(unit, "capacity")
        flags = {flag for line in capacity["lines"] for flag in line["flags"]}
        self.assertIn("baseline_partial_coverage", flags)

    def test_baseline_gap_strict_policy_fails_unit(self):
        payload = support.payload(
            baselines=[
                support.baseline(
                    representation="profile",
                    samples=[{"start": "2026-06-01T10:00:00Z", "end": "2026-06-01T10:15:00Z", "kw": 100}],
                )
            ],
            rules={"baseline_policy": "materialized", "coverage_policy": "strict"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertIn("baseline_insufficient_coverage", {item["code"] for item in unit["errors"]})

    def test_baseline_versions_are_resolved_as_of(self):
        payload = support.payload(
            as_of="2026-06-01T12:00:00Z",
            baselines=[
                support.baseline(version=1, level_kw=100, issued_at="2026-06-01T08:00:00Z"),
                support.baseline(version=2, level_kw=90, issued_at="2026-06-02T00:00:00Z"),
            ],
            rules={"baseline_policy": "materialized"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["baseline"]["version"], "b1@v1")
        # At a later as_of the newer version is visible.
        import copy

        later = copy.deepcopy(payload)
        later["as_of"] = "2026-06-03T00:00:00Z"
        unit_later = _unit(run(later))
        self.assertEqual(unit_later["baseline"]["version"], "b1@v2")
        self.assertNotEqual(unit["final_cents"], unit_later["final_cents"])


class TestDeterminism(unittest.TestCase):
    def test_input_order_does_not_affect_output(self):
        base = support.payload(
            baselines=[
                support.baseline(version=1, level_kw=100),
                support.baseline(version=2, level_kw=90, issued_at="2026-06-02T12:00:00Z"),
            ],
            readings=[
                support.reading(reading_id="m1", start="2026-06-01T10:00:00Z", end="2026-06-01T10:30:00Z", kw=60),
                support.reading(reading_id="m2", start="2026-06-01T10:30:00Z", end="2026-06-01T11:00:00Z", kw=70),
            ],
            rates=[
                support.rate(rate_version_id="rt1", version=1),
                support.rate(rate_version_id="rt2", version=1, priority=0),
            ],
            instructions=[
                support.instruction(instruction_id="i1", issued_at="2026-06-01T08:00:00Z"),
            ],
        )
        forward = run(base)
        shuffled = {**base}
        shuffled["readings"] = list(reversed(base["readings"]))
        shuffled["baselines"] = list(reversed(base["baselines"]))
        shuffled["rates"] = list(reversed(base["rates"]))
        backward = run(shuffled)
        self.assertEqual(forward["totals"], backward["totals"])
        self.assertEqual(
            [unit["final_cents"] for unit in forward["units"]],
            [unit["final_cents"] for unit in backward["units"]],
        )
        self.assertEqual(canonical_json(forward["units"]), canonical_json(backward["units"]))

    def test_repeated_runs_are_identical(self):
        payload = support.payload()
        first = run(payload)
        second = run(payload)
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_second_recalculation_uses_pinned_as_of(self):
        payload = support.payload(
            as_of="2026-06-01T12:00:00Z",
            baselines=[
                support.baseline(version=1, level_kw=100, issued_at="2026-06-01T08:00:00Z"),
                support.baseline(version=2, level_kw=90, issued_at="2026-06-02T00:00:00Z"),
            ],
            readings=[
                support.reading(
                    reading_id="m1", kw=60, version=1, received_at="2026-06-01T11:05:00Z"
                ),
                # A correction to the same reading that only arrives on 4 June.
                # With as_of pinned to 1 June it must be invisible, so the
                # recalculation is byte-identical to the original run.
                support.reading(
                    reading_id="m1", kw=50, version=2, received_at="2026-06-04T00:00:00Z"
                ),
            ],
            rules={"baseline_policy": "materialized", "late_reading_policy": "exclude"},
        )
        first = run(payload)
        second = run(payload)
        self.assertEqual(first["totals"], second["totals"])
        unit = _unit(first)
        self.assertEqual(unit["audit"]["readings"]["excluded"][0]["reading_id"], "m1")
        self.assertEqual(unit["metrics"]["actual_kwh"], "60")
        # Without the cutoff the late revision would be used.
        import copy

        late = copy.deepcopy(payload)
        late["as_of"] = "2026-06-05T00:00:00Z"
        self.assertEqual(_unit(run(late))["metrics"]["actual_kwh"], "50")


class TestRounding(unittest.TestCase):
    def test_allocation_preserves_total_with_largest_remainder(self):
        weights = [dec("0.005"), dec("0.005"), dec("0.005")]
        allocation = allocate_minor(1, weights, ["a", "b", "c"])
        self.assertEqual(sum(allocation), 1)
        self.assertEqual(allocation, [1, 0, 0])

    def test_round_once_per_component_not_per_line(self):
        # Three intervals each at 1/3 cent would round to 0 each if lines were
        # rounded independently; the component total must survive.
        payload = support.payload(
            events=[support.event(settlement_window={"start": "2026-06-01T10:00:00Z", "end": "2026-06-01T10:03:00Z"})],
            readings=[
                support.reading(reading_id="m1", start="2026-06-01T10:00:00Z", end="2026-06-01T10:01:00Z", kw=60),
                support.reading(reading_id="m2", start="2026-06-01T10:01:00Z", end="2026-06-01T10:02:00Z", kw=60),
                support.reading(reading_id="m3", start="2026-06-01T10:02:00Z", end="2026-06-01T10:03:00Z", kw=60),
            ],
            rates=[
                support.rate(
                    capacity_rate_per_kw_hour="0.01",
                    energy_rate_per_kwh="0.0001",
                    deviation_rate_per_kwh="0",
                )
            ],
            instructions=[
                support.instruction(capacity_kw=1, start="2026-06-01T10:00:00Z", end="2026-06-01T10:03:00Z")
            ],
        )
        result = run(payload, compare_rounding=True)
        unit = _unit(result)
        for component in unit["components"]:
            self.assertTrue(component["allocation_reconciles"])
        self.assertTrue(result["reconciliation"]["balanced"])
        self.assertEqual(result["rounding_comparison"]["policy_used"], "round each component once, allocate cents to lines")


class TestDuplicatesAndConflicts(unittest.TestCase):
    def test_identical_duplicate_instructions_collapse(self):
        duplicate = support.instruction()
        payload = support.payload(instructions=[duplicate, dict(duplicate)])
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(unit["instruction"]["versions"], ["i1@v1"])
        self.assertIn("duplicate_instructions", {item["code"] for item in unit["warnings"]})

    def test_conflicting_duplicate_instructions_are_an_error(self):
        payload = support.payload(
            instructions=[
                support.instruction(capacity_kw=50),
                support.instruction(capacity_kw=80),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertIn("instruction_conflict", {item["code"] for item in unit["errors"]})

    def test_stacked_instructions_suppressed_deterministically(self):
        payload = support.payload(
            instructions=[
                support.instruction(instruction_id="i1", issued_at="2026-06-01T08:00:00Z"),
                support.instruction(instruction_id="i2", issued_at="2026-06-01T09:00:00Z", capacity_kw=80),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["instruction"]["dominant"], "i2@v1")
        self.assertEqual(unit["instruction"]["versions"], ["i2@v1"])
        self.assertIn("instruction_stacking_suppressed", {item["code"] for item in unit["warnings"]})

    def test_overlapping_rates_at_equal_priority_are_a_conflict(self):
        payload = support.payload(
            rates=[
                support.rate(rate_version_id="rt1", energy_rate_per_kwh="0.25"),
                support.rate(rate_version_id="rt2", energy_rate_per_kwh="0.35"),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertIn("rate_conflict", {item["code"] for item in unit["errors"]})

    def test_rate_priority_policy_resolves_conflict(self):
        payload = support.payload(
            rates=[
                support.rate(rate_version_id="rt1", energy_rate_per_kwh="0.25", priority=0),
                support.rate(rate_version_id="rt2", energy_rate_per_kwh="0.35", priority=5),
            ],
            rules={"rate_conflict_policy": "priority"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(_component(unit, "performance")["amount_cents"], 1400)

    def test_rate_version_supersede_chain(self):
        payload = support.payload(
            rates=[
                support.rate(rate_version_id="rt1", version=1, energy_rate_per_kwh="0.25"),
                support.rate(
                    rate_version_id="rt1",
                    version=2,
                    energy_rate_per_kwh="0.5",
                    supersedes="rt1",
                    issued_at="2026-06-01T09:00:00Z",
                ),
            ],
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(_component(unit, "performance")["amount_cents"], 2000)

    def test_rate_boundary_splits_the_bill_within_one_event(self):
        payload = support.payload(
            rates=[
                support.rate(
                    rate_version_id="rt1",
                    version=1,
                    effective_from="2026-01-01T00:00:00Z",
                    effective_to="2026-06-01T10:30:00Z",
                    energy_rate_per_kwh="0.25",
                    capacity_rate_per_kw_hour="0.5",
                ),
                support.rate(
                    rate_version_id="rt2",
                    version=1,
                    effective_from="2026-06-01T10:30:00Z",
                    energy_rate_per_kwh="0.5",
                    capacity_rate_per_kw_hour="1.0",
                ),
            ],
        )
        result = run(payload)
        unit = _unit(result)
        performance = _component(unit, "performance")
        self.assertEqual(len(performance["lines"]), 2)
        self.assertEqual(performance["amount_cents"], 500 + 1000)
        self.assertEqual(
            {line["rate_version"] for line in performance["lines"]},
            {"rt1@v1", "rt2@v1"},
        )


class TestCorrections(unittest.TestCase):
    def test_correction_changes_capacity_and_is_audited(self):
        payload = support.payload(
            corrections=[
                support.correction(changes={"capacity_kw": 100}, issued_at="2026-06-02T00:00:00Z")
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 5000)
        applied = unit["audit"]["event_version"]["corrections"]
        self.assertEqual(applied[0]["correction_id"], "c1")

    def test_correction_after_as_of_is_not_applied(self):
        payload = support.payload(
            as_of="2026-06-01T12:00:00Z",
            corrections=[support.correction(changes={"capacity_kw": 100}, issued_at="2026-06-02T00:00:00Z")],
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 2500)
        self.assertEqual(unit["audit"]["event_version"]["corrections"], [])

    def test_withdrawn_correction_revokes_event(self):
        payload = support.payload(
            corrections=[
                support.correction(correction_id="c1", version=1, issued_at="2026-06-02T00:00:00Z"),
                support.correction(
                    correction_id="c1",
                    version=2,
                    issued_at="2026-06-02T12:00:00Z",
                    status="withdrawn",
                    supersedes={"id": "c1", "version": 1},
                ),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "revoked")
        self.assertEqual(unit["final_cents"], 0)

    def test_correction_ordering_is_stable(self):
        corrections = [
            support.correction(correction_id="c1", version=2, issued_at="2026-06-02T00:00:00Z", changes={"capacity_kw": 90}),
            support.correction(correction_id="c1", version=1, issued_at="2026-06-01T20:00:00Z", changes={"capacity_kw": 70}),
            support.correction(correction_id="c1", version=3, issued_at="2026-06-03T00:00:00Z", changes={"capacity_kw": 110}),
        ]
        first = run(support.payload(corrections=corrections))
        second = run(support.payload(corrections=list(reversed(corrections))))
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(_component(_unit(first), "capacity")["amount_cents"], 5500)


class TestBills(unittest.TestCase):
    def test_locked_bill_generates_new_revision_without_rewriting_old(self):
        payload = support.payload(prior_bills=[support.prior_bill(amount_cents=1000, status="locked")])
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["bill"]["revision"], 2)
        self.assertEqual(unit["bill"]["amount_cents"], 3400)
        self.assertEqual(unit["bill"]["adjustment_cents"], 2400)
        self.assertFalse(unit["bill"]["supersedes"]["rewritten"])
        self.assertEqual(unit["bill"]["supersedes"]["amount_cents"], 1000)
        self.assertTrue(unit["audit"]["revisions"]["locked_prior_unchanged"])

    def test_first_bill_is_revision_one(self):
        result = run(support.payload())
        unit = _unit(result)
        self.assertEqual(unit["bill"]["revision"], 1)
        self.assertIsNone(unit["bill"]["supersedes"])


class TestFailureIsolation(unittest.TestCase):
    def test_one_bad_resource_does_not_hide_a_good_one(self):
        payload = support.payload(
            events=[support.event()],
            eligibility=[
                support.eligibility(resource_id="r1", eligibility_id="el1"),
                support.eligibility(resource_id="r2", eligibility_id="el2"),
            ],
            baselines=[
                support.baseline(resource_id="r1", baseline_id="b1"),
                support.baseline(resource_id="r2", baseline_id="b2", status="revoked"),
            ],
            instructions=[
                support.instruction(resource_id="r1", instruction_id="i1"),
                support.instruction(resource_id="r2", instruction_id="i2"),
            ],
            readings=[
                support.reading(resource_id="r1", reading_id="m1"),
                support.reading(resource_id="r2", reading_id="m2", kw=70),
            ],
            rules={"baseline_policy": "materialized"},
        )
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        units = {unit["key"]: unit for unit in result["units"]}
        self.assertEqual(units["e1/r1"]["status"], "settled")
        self.assertEqual(units["e1/r2"]["status"], "error")
        self.assertEqual(units["e1/r1"]["final_cents"], 3400)
        self.assertEqual(units["e1/r2"]["final_cents"], 0)
        # The batch total must only include the settled unit, not treat the
        # failure as a zero-value success.
        self.assertEqual(result["totals"]["settled_cents"], 3400)
        self.assertEqual(result["totals"]["error_units"], 1)

    def test_malformed_unit_never_aborts_the_batch(self):
        payload = support.payload(
            eligibility=[
                support.eligibility(resource_id="r1", eligibility_id="el1"),
                support.eligibility(resource_id="r2", eligibility_id="el2"),
            ],
            baselines=[
                support.baseline(resource_id="r1", baseline_id="b1"),
                support.baseline(resource_id="r2", baseline_id="b2"),
            ],
            instructions=[
                support.instruction(resource_id="r1", instruction_id="i1"),
                # r2's instruction has an unknown event; r2 still gets a unit.
                support.instruction(resource_id="r2", instruction_id="i2", event_id="e1"),
            ],
            readings=[
                support.reading(resource_id="r1", reading_id="m1"),
                support.reading(resource_id="r2", reading_id="m2", kw="not-a-number"),
            ],
        )
        # A malformed scalar is reported as an input issue, not a crash.
        result = run(payload)
        self.assertIn(result["status"], ("ok", "partial"))
        units = {unit["key"]: unit for unit in result["units"]}
        self.assertIn("e1/r1", units)


class TestDeclaredResources(unittest.TestCase):
    def test_declared_but_incomplete_resource_is_an_explicit_error(self):
        payload = support.payload(resources=[{"resource_id": "r1"}, {"resource_id": "r2"}])
        result = run(payload)
        units = {unit["key"]: unit for unit in result["units"]}
        self.assertEqual(units["e1/r1"]["status"], "settled")
        self.assertEqual(units["e1/r2"]["status"], "error")
        self.assertIn("eligibility_missing", {item["code"] for item in units["e1/r2"]["errors"]})
        self.assertEqual(result["status"], "partial")


class TestCheckpointResume(unittest.TestCase):
    def test_unchanged_units_are_reused_on_resume(self):
        payload = support.payload(
            eligibility=[
                support.eligibility(resource_id="r1", eligibility_id="el1"),
                support.eligibility(resource_id="r2", eligibility_id="el2"),
            ],
            baselines=[
                support.baseline(resource_id="r1", baseline_id="b1"),
                support.baseline(resource_id="r2", baseline_id="b2"),
            ],
            instructions=[
                support.instruction(resource_id="r1", instruction_id="i1"),
                support.instruction(resource_id="r2", instruction_id="i2"),
            ],
            readings=[
                support.reading(resource_id="r1", reading_id="m1"),
                support.reading(resource_id="r2", reading_id="m2", kw=70),
            ],
        )
        first = run(payload)
        resumed = run(payload, checkpoint=first["checkpoint"])
        for unit in resumed["units"]:
            self.assertTrue(unit["audit"].get("resumed_from_checkpoint"))
        self.assertEqual(first["totals"], resumed["totals"])

    def test_previously_errored_unit_is_retried_on_resume(self):
        bad = support.payload(baselines=[])
        first = run(bad)
        self.assertEqual(_unit(first)["status"], "error")
        # The same payload is retried rather than frozen in its failed form.
        # Repairs that do not change the *selected* unit inputs (here: a
        # baseline is added, changing the input fingerprint) must recalculate.
        repaired = support.payload()
        second = run(repaired, checkpoint=first["checkpoint"])
        self.assertEqual(_unit(second)["status"], "settled")
        self.assertFalse(_unit(second)["audit"].get("resumed_from_checkpoint"))

    def test_changed_unit_is_recalculated_on_resume(self):
        payload = support.payload()
        first = run(payload)
        changed = support.payload(instructions=[support.instruction(capacity_kw=80)])
        second = run(changed, checkpoint=first["checkpoint"])
        unit = _unit(second)
        self.assertFalse(unit["audit"].get("resumed_from_checkpoint"))
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 4000)


class TestDesignComparison(unittest.TestCase):
    def test_three_designs_are_compared_and_explained(self):
        payload = support.payload(
            baselines=[support.baseline(level_kw=100)],
            rules={"baseline_policy": "hybrid"},
        )
        result = run(payload, include_design_comparison=True)
        design = result["design"]
        self.assertEqual(set(design["designs"]), {"materialized", "dynamic", "hybrid"})
        for policy in design["designs"]:
            self.assertIn("summary", design["notes"][policy])
        self.assertEqual(design["designs"]["materialized"]["status"], "ok")
        self.assertEqual(design["designs"]["dynamic"]["status"], "error")

    def test_design_deltas_are_reported(self):
        payload = support.payload(
            baselines=[],
            readings=[
                support.reading(reading_id="pre", start="2026-06-01T09:00:00Z", end="2026-06-01T10:00:00Z", kw=100),
                support.reading(reading_id="m1"),
            ],
            rules={"baseline_policy": "hybrid"},
        )
        result = run(payload, include_design_comparison=True)
        design = result["design"]["designs"]
        self.assertEqual(design["materialized"]["status"], "error")
        self.assertEqual(design["dynamic"]["status"], "ok")
        self.assertGreater(len(result["design"]["deltas"]), 0)

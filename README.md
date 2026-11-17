# Grid Flex Settlement

Auditable, event-level demand-response settlement. From metering readings,
eligibility windows, baseline versions, dispatch instructions, actual execution
and rate effective windows, the engine produces a per-component breakdown
(capacity, performance, deviation) and a final amount for every event × resource
unit — with the input version used at every step recorded alongside the money.

## Modules

- `src/grid_flex_settlement/models.py`: frozen domain and result contracts.
- `src/grid_flex_settlement/parser.py`: strict input validation and
  normalization; legacy and event-level schemas.
- `src/grid_flex_settlement/timeutil.py`: UTC normalization, DST handling,
  interval algebra, local-midnight splitting.
- `src/grid_flex_settlement/decimalutil.py`: exact decimal money, single
  rounding point, largest-remainder cent allocation.
- `src/grid_flex_settlement/canon.py`: order-independent canonical hashing.
- `src/grid_flex_settlement/resolution.py`: version selection, correction
  propagation, duplicate detection, conflict detection.
- `src/grid_flex_settlement/engine.py`: domain calculation and per-unit
  isolation.
- `src/grid_flex_settlement/report.py`: stable output envelope and
  reconciliation.
- `src/grid_flex_settlement/pipeline.py`: orchestration, checkpoint resume,
  design comparison, rounding-order comparison.
- `src/grid_flex_settlement/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m grid_flex_settlement.cli                     # legacy fixture
PYTHONPATH=src python -m grid_flex_settlement.cli fixtures/event_level.json
PYTHONPATH=src python -m unittest discover -s tests -t . -v
```

The fixtures are local and deterministic; no network or credentials are
required.

## Model

### Three windows, modeled separately

| window | meaning | never used for |
| --- | --- | --- |
| eligibility | when the resource is enrolled and may be dispatched | establishing a baseline |
| event | the announced dispatch interval | deciding payment scope |
| settlement | the interval actually billed (defaults to the event) | enrolment |

They are intersected explicitly. The audit reports all four intervals
(including the baseline reference window) plus a `relation` block, so
"settlement ∩ eligibility" is inspectable rather than implied. A settlement
window that only partly overlaps eligibility is settled for the overlap and
flagged; a settlement window entirely outside eligibility is an `error` unit,
not a zero.

### Baseline handling

A baseline is selected, never invented:

- `materialized`: require an explicit baseline version. Missing means the unit
  errors. Event-window readings are **never** substituted for a missing or
  overwritten baseline.
- `dynamic`: derive a scalar level only from readings that end strictly before
  the event starts. Readings overlapping the event are excluded and counted.
- `hybrid`: prefer the newest visible materialized version whose coverage meets
  the threshold; otherwise fall back to an explicit dynamic derivation, with the
  fallback recorded as a distinct `source` and `substituted: true`.

A baseline is either a `profile` (samples aligned to the event window) or a
`scalar` (a single level derived from an earlier reference window and applied
across the event). Coverage is measured against the window the baseline
governs. A revoked newest version does not silently fall back to an older one.
See `compare_designs` (and `run(..., include_design_comparison=True)`) for a
side-by-side comparison of the three designs against the same payload.

### Versioning and corrections

`as_of` is a hard visibility cutoff: a version issued after it is invisible, so
an "as of" recalculation is reproducible. The resolver also handles:

- supersede chains (`supersedes`) for baselines, instructions and rates;
- identical duplicate instructions collapsed, conflicting duplicates errored,
  and stacked active instructions suppressed deterministically (latest issued
  wins) unless the rules allow stacking;
- rate version conflicts: overlapping rates at equal priority are an error,
  or resolved by explicit priority when the policy says so;
- correction announcements applied in `(version, issued_at, id)` order. A
  correction can revise the window, direction, capacity, rate reference or
  status; a withdrawn correction revokes the event. A capacity correction
  issued after an instruction overrides that instruction's capacity, and the
  override is recorded on the line and in the audit.

### Money and rounding

All quantities and money are exact `Decimal`. Each component is rounded to
cents exactly once, then the component's cents are allocated to its interval
lines with the largest-remainder method. Consequently:

```
sum(line.amount_cents per component) == component.amount_cents
sum(component.amount_cents)          == unit.final_cents
```

Every line reports its exact amount, its allocated cents, and the
`allocation_residual`, so a one-cent difference is explained rather than
absorbed. `run(..., compare_rounding=True)` also reports what the alternative
"round every line independently" policy would have produced.

### Provenance

Each line carries its UTC interval, rule version, rate version, baseline
version, instruction version, contributing reading ids and flags. Each unit's
`audit.stages` records a fingerprint after **every** stage: event resolution,
window model, baseline selection, instruction selection, rate selection,
segmentation, components, and the input dataset. Identical inputs produce an
identical `deterministic_fingerprint`; input array order does not matter.

### Failure isolation

The unit (event × resource) is the isolation boundary. One unit's missing
baseline, conflicting rate, bad instruction or malformed reading yields an
`error` unit with a non-zero error list and a zero amount; other units still
settle. The batch status is `ok`, `partial` or `error`, and totals count errored
units separately so a failure is never reported as a zero-value success.

### Revisions and locked bills

A locked prior bill is never rewritten. The engine emits a **new** draft
revision that references the prior bill, its amount and its status, and reports
the adjustment. `supersedes.rewritten` is always `false` for a locked bill.

## Output shape

`run(payload)` returns `status`, `totals`, `batch_errors`, `audit`, `units`,
`reconciliation`, `input` and `checkpoint`. Each unit reports `windows`,
`baseline`, `rate`, `instruction`, `metrics`, `components` (each with `lines`),
`final_cents`, `final_amount`, `errors`, `warnings`, `bill`, `revision` and
`audit`.

Legacy payloads (the original `readings` + `event` shape) are lifted into the
event-level model automatically and also expose the original
`settlement_amount`, `baseline_kw`, `actual_kw`, `delivered_kw` and
`explanation` keys.

## Checkpoint and resume

`run(payload, checkpoint=previous["checkpoint"])` reuses any unit whose
`input_fingerprint` is unchanged, recalculates units whose inputs changed, and
retries units that previously errored. An interrupted batch is therefore
resumable without recomputing or silently freezing a unit that was recalculated
from different inputs.

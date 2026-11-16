# Grid Flex Settlement

Calculate demand-response settlement from baseline, dispatch, meter, and rate versions.

## Modules

- `src/grid_flex_settlement/models.py`: immutable result contract.
- `src/grid_flex_settlement/parser.py`: input validation and record extraction.
- `src/grid_flex_settlement/engine.py`: domain calculation only.
- `src/grid_flex_settlement/report.py`: stable output envelope.
- `src/grid_flex_settlement/pipeline.py`: orchestration boundary.
- `src/grid_flex_settlement/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m grid_flex_settlement.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

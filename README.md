# LazyRay

Ray Dalio-style analytical layer for [market-data-hub](https://github.com/selvaz/market-data-hub):
a debt-cycle / growth-inflation regime classifier (v1, `dalio.py`) plus an
additive 5-engine country risk architecture (v2, `dalio_v2/`) — sovereign
solvency, private credit cycle, external currency constraint, funding
liquidity, political execution — and a Fase 5 cycle classifier on top.

Extracted from market-data-hub into its own repo: input (macro_panel,
country-level indicators) is read exclusively from the hub's public
`market_data_hub.reader` / `market_data_hub.catalog` API — never a direct
SQL connection to the hub's DuckDB file — and output (scores, regimes,
country classification) is written to LazyRay's **own** DuckDB file. See
market-data-hub's `docs/ECOSYSTEM_RATIONALIZATION.md` for the extraction
rationale and the ecosystem-wide pattern this follows (same shape as
LazyHMM/LazyFin's relationship to the hub).

## Setup

```bash
pip install -e .
```

Sending the Dalio v2 report to Telegram is the one thing that needs more.
`send_telegram_report.py` imports the connector only when it actually sends,
so the core, the tests and `--dry-run` all work with the install above; the
extra is what a real send requires:

```bash
pip install -e ".[telegram]"     # needs Python >= 3.11
```

Without it, `--dry-run` still resolves and prints the report it would have
sent, and asking to send says which command to run rather than failing on an
import.

Point LazyRay at its own output database (precedence: explicit arg ->
`LAZYRAY_DB` env -> `lazyray/config/settings.yaml::db_path` -> repo-local
default `lazyray.duckdb`), and at the hub's database for input (precedence:
explicit arg -> `MARKET_DATA_DB` env -> the hub's own settings, same
resolution the hub's own tools use):

```bash
export MARKET_DATA_DB=/path/to/market_data.duckdb
export LAZYRAY_DB=/path/to/lazyray.duckdb
```

## Usage

```bash
# Dalio v2 — 5-engine country risk analysis
python run_dalio_v2.py                    # refresh scores + regenerate the HTML report
python run_dalio_v2.py --csv              # also write a CSV snapshot
python run_dalio_v2.py --ref-year 2025
python run_dalio_v2.py --engines sovereign_solvency

# Unified v1 + v2 interactive dashboard
python make_dalio_report.py --calc --calc-v2 --open
```

```python
from lazyray.dalio import run_dalio
from lazyray.classify import classify_countries
from lazyray.dalio_v2.runner import run_dalio_v2

run_dalio()            # v1: z-scores, pillar scores, four-box regime, debt-cycle phase
classify_countries()   # static + data-driven country classification
run_dalio_v2()          # v2: 5 engines + Fase 5 cycle classifier
```

## Module map

```
lazyray/
├── dalio.py, classify.py     v1: composite z-score + debt-cycle/four-box regime
│                              classifier, country classification
├── dalio_v2/                 additive 5-engine country risk architecture
│   ├── scoring.py             sovereign_solvency.py  political_execution.py
│   ├── cycle_classifier.py    private_credit.py  external_constraint.py
│   ├── report.py              funding_liquidity.py
│   └── runner.py              run_dalio_v2() orchestrates all 5, writes engine_scores
├── db/
│   ├── schema.sql             LazyRay's own tables (dalio_signals, pillar_scores,
│   │                          regime_state, engine_scores, dalio_cycle_v2)
│   └── connection.py          get_conn() — LazyRay's own DB, independent of the hub's
├── lock.py                    db_write_lock() for LazyRay's own DB file
└── config/settings.yaml       dalio:/dalio_v2: thresholds and weights

run_dalio_v2.py  make_dalio_report.py
```

See [`docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md`](docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md)
for the `dalio_v2` design and the other `docs/DALIO_*.md` files for the
methodology review, data coverage audit, and vintage/point-in-time plan.

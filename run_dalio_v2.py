# -*- coding: utf-8 -*-
"""
run_dalio_v2.py — refresh the Dalio v2 engine scores and (re)generate the
HTML/CSV snapshot report.

Additive: this never touches dalio.py's dalio_signals/pillar_scores/
regime_state, nor make_dalio_report.py's output. See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md for the design (Fase 1
implements sovereign_solvency + political_execution; more engines land in
later phases and appear automatically in the report once they do).

Usage:
    python run_dalio_v2.py                          # all 5 engines, current year
    python run_dalio_v2.py --ref-year 2025
    python run_dalio_v2.py --engines sovereign_solvency
    python run_dalio_v2.py --csv                     # also write a CSV snapshot
    python run_dalio_v2.py --db /path/to/lazyray.duckdb
    python run_dalio_v2.py --hub-db /path/to/market_data.duckdb
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lazyray.config_loader import get_settings  # noqa: E402
from lazyray.dalio_v2.report import collect, generate_html_report, to_csv  # noqa: E402
from lazyray.dalio_v2.runner import run_dalio_v2  # noqa: E402
from lazyray.db.connection import get_conn  # noqa: E402


def _report_dir() -> Path:
    cfg = get_settings().get("reports", {})
    base = Path(cfg.get("dir") or "reports")
    if not base.is_absolute():
        base = Path(__file__).parent / base
    return base / "dalio_v2"


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh Dalio v2 engine scores + report")
    p.add_argument("--db", help="LazyRay's own DuckDB path (output); defaults to lazyray settings")
    p.add_argument("--hub-db", help="market-data-hub's DuckDB path (input, read-only); "
                                    "defaults to MARKET_DATA_DB / the hub's own settings")
    p.add_argument("--ref-year", type=int, help="reference year (default: current year)")
    p.add_argument("--engines", help="comma-separated engine subset (default: all implemented)")
    p.add_argument("--csv", action="store_true", help="also write a CSV snapshot")
    args = p.parse_args()

    engines = [e.strip() for e in args.engines.split(",")] if args.engines else None
    ref_year = args.ref_year or datetime.now().year
    ref_date = date(ref_year, 12, 31)

    print(f"Computing Dalio v2 engines as of {ref_date.isoformat()} "
          f"({'all implemented' if not engines else ', '.join(engines)})...")
    summary = run_dalio_v2(engines=engines, ref_year=ref_year, db_path=args.db,
                           hub_db_path=args.hub_db)
    for name, n in summary.items():
        print(f"  {name}: {n} countries scored")
    # cycle_classifier is None (not 0) when its gate was skipped -- excluded
    # here so it can't make an all-engines-empty run look non-empty (Codex
    # review: None == 0 is False, so `all()` over every value including it
    # never fired, letting a no-data run "succeed" with a blank report).
    engine_counts = [n for name, n in summary.items() if name != "cycle_classifier"]
    if all(n == 0 for n in engine_counts):
        print("No scores written (empty macro_panel?) - skipping report.", file=sys.stderr)
        return 1

    con = get_conn(args.db, read_only=True)
    try:
        out_dir = _report_dir()
        html_path = generate_html_report(con, ref_date, out_dir, engines=engines)
        print(f"Report: {html_path}")
        if args.csv:
            df = collect(con, ref_date, engines)
            csv_path = to_csv(df, out_dir / f"dalio_v2_{ref_date}.csv")
            print(f"CSV:    {csv_path}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

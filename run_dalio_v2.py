# -*- coding: utf-8 -*-
"""
run_dalio_v2.py — refresh the Dalio v2 engine scores and (re)generate the
HTML/CSV snapshot report.

Additive: this never touches dalio.py's dalio_signals/pillar_scores/
regime_state, nor make_dalio_report.py's output. See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md for the engine design and
docs/DALIO_PROD_ASSESSMENT_2026-09.md for the cadence/point-in-time fix this
CLI implements (ref_date = the run date, not a fixed year-end; --if-changed
so a daily schedule only produces output when the inputs actually moved).

Usage:
    python run_dalio_v2.py                          # all 5 engines, ref_date = today
    python run_dalio_v2.py --as-of 2026-08-01        # point-in-time historical run
    python run_dalio_v2.py --engines sovereign_solvency
    python run_dalio_v2.py --csv                     # also write a CSV snapshot
    python run_dalio_v2.py --if-changed              # skip (exit 3) if inputs unchanged
    python run_dalio_v2.py --db /path/to/lazyray.duckdb
    python run_dalio_v2.py --hub-db /path/to/market_data.duckdb

Exit codes: 0 success, 1 no scores written (empty macro_panel), 2 invalid
--as-of, 3 skipped by --if-changed (inputs unchanged since an earlier run --
deliberately distinct from 0/1 so a scheduler's "on success" hook, e.g. the
Telegram send, does not fire for a no-op day).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from lazyray.brief import build_brief
from lazyray.config_loader import get_settings
from lazyray.dalio_v2.report import collect, generate_html_report, to_csv
from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn
from lazyray.lock import db_write_lock


def _reports_root() -> Path:
    cfg = get_settings().get("reports", {})
    base = Path(cfg.get("dir") or "reports")
    if not base.is_absolute():
        base = Path(__file__).parent / base
    return base


def _report_dir() -> Path:
    return _reports_root() / "dalio_v2"


def _brief_dir() -> Path:
    return _reports_root() / "brief"


def main() -> int:
    # Windows consoles/log redirects default to cp1252; never let a report
    # character kill the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Refresh Dalio v2 engine scores + report")
    p.add_argument("--db", help="LazyRay's own DuckDB path (output); defaults to lazyray settings")
    p.add_argument("--hub-db", help="market-data-hub's DuckDB path (input, read-only); "
                                    "defaults to MARKET_DATA_DB / the hub's own settings")
    p.add_argument("--as-of", help="reference date YYYY-MM-DD (default: today). "
                                   "A date before today reads the hub's vintage "
                                   "tables (point-in-time); today or later reads "
                                   "the live view.")
    p.add_argument("--engines", help="comma-separated engine subset (default: all implemented)")
    p.add_argument("--csv", action="store_true", help="also write a CSV snapshot")
    p.add_argument("--if-changed", action="store_true",
                   help="skip the run (exit 3) if the input panel hash matches "
                        "the most recent run_meta row")
    args = p.parse_args()

    engines = [e.strip() for e in args.engines.split(",")] if args.engines else None
    if args.as_of:
        try:
            ref_date = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"Invalid --as-of {args.as_of!r}: expected YYYY-MM-DD.", file=sys.stderr)
            return 2
    else:
        ref_date = date.today()

    print(f"Computing Dalio v2 engines as of {ref_date.isoformat()} "
          f"({'all implemented' if not engines else ', '.join(engines)})...")
    summary = run_dalio_v2(engines=engines, ref_date=ref_date, db_path=args.db,
                           hub_db_path=args.hub_db, if_changed=args.if_changed)
    if summary.get("skipped"):
        print(f"inputs unchanged since {summary.get('since')}; skipped")
        return 3
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

    # run_dalio_v2() above already acquired and released db_write_lock
    # internally -- runner.py's `with db_write_lock(db_path):` spans the
    # whole function body and con.close() runs before it returns -- so the
    # lock is free by the time we get here. This block reopens the same
    # DuckDB file read_only=True to render the report/Brief, which looks
    # like a pure read needing no coordination. It is not: DuckDB allows
    # only a single writer OR multiple readers per file, never a mix, and
    # rejects the wrong combination in either direction with a raw
    # IOException at open time -- not lock.py's DBLockTimeout. The sibling
    # stress monitor (run_stress_monitor.py) opens a writer under this same
    # lock (lazyray/stress/persist.py::write_results), and in the overlap
    # scenario the monitor is waiting on the lock and gets released right
    # when this phase starts -- so an unlocked reader here collided with
    # that writer almost every time, not rarely. Taking the lock again here
    # (fresh, AFTER the write phase's `with` block above has already
    # exited -- db_write_lock is not reentrant, nesting it would deadlock
    # against ourselves) turns that collision into contention on the lock,
    # i.e. DBLockTimeout, which the stress monitor already handles with its
    # own exit 3.
    #
    # Deliberately NOT caught here: if this phase can't get the lock, this
    # run fails and the task goes red. Dalio is the producer of the Brief;
    # a swallowed exit-3-style "nothing to do" here would look green while
    # silently never sending a Brief. See docs/DALIO_PROD_ASSESSMENT_2026-09.md.
    with db_write_lock(args.db):
        con = get_conn(args.db, read_only=True)
        try:
            out_dir = _report_dir()
            html_path = generate_html_report(con, ref_date, out_dir, engines=engines)
            print(f"Report: {html_path}")
            if args.csv:
                df = collect(con, ref_date, engines)
                csv_path = to_csv(df, out_dir / f"dalio_v2_{ref_date}.csv")
                print(f"CSV:    {csv_path}")

            # The Brief (docs/DALIO_PROD_ASSESSMENT_2026-09.md): the one output a
            # human reads, built from the same ref_date's engine_scores/
            # dalio_cycle_v2/run_meta/stress_* rows this run just wrote. Never
            # blocks the exit code on a Brief-rendering problem -- the engine
            # scores and the old report above are already safely on disk by the
            # time this runs.
            brief_dir = _brief_dir()
            brief_dir.mkdir(parents=True, exist_ok=True)
            brief_path = brief_dir / f"lazyray_brief_{ref_date}.html"
            _, brief_html = build_brief(con, ref_date)
            brief_path.write_text(brief_html, encoding="utf-8")
            print(f"Brief:  {brief_path}")
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

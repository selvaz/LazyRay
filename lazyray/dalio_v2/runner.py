# -*- coding: utf-8 -*-
"""
runner.py — orchestrates the Dalio v2 engines and writes engine_scores.

Additive: never touches dalio_signals/pillar_scores/regime_state (dalio.py
keeps producing the current report unchanged). See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md §1 (non-goal).

Usage:
    from lazyray.dalio_v2.runner import run_dalio_v2
    run_dalio_v2()                      # all 5 engines, current year
    run_dalio_v2(ref_year=2026)
    run_dalio_v2(engines=["sovereign_solvency"])

Input is read from market-data-hub's public API (reader.read_macro_panel /
read_macro_panel_ext, see each engine's compute()); output (engine_scores,
dalio_cycle_v2) is written to LazyRay's own DB.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

from lazyray.dalio_v2 import (
    cycle_classifier, external_constraint, funding_liquidity, political_execution,
    private_credit, sovereign_solvency,
)
from lazyray.db.connection import get_conn
from lazyray.lock import db_write_lock

_ENGINES = {
    "sovereign_solvency": sovereign_solvency.compute,
    "political_execution": political_execution.compute,
    "private_credit": private_credit.compute,
    "external_constraint": external_constraint.compute,
    "funding_liquidity": funding_liquidity.compute,
}


def _records_with_real_nulls(df: pd.DataFrame):
    """df.itertuples() straight from a mixed None/str DataFrame is not safe
    to feed to executemany(): pandas' string-dtype inference silently turns
    a column's `None` entries into its own NA sentinel, which itertuples()
    then yields as a bare float('nan') -- and DuckDB writes THAT into a
    VARCHAR column as the literal 3-character text "nan", not SQL NULL. That
    "nan" text then survives every downstream `pd.isna(label)` check (it's a
    normal string, not missing), so it leaks into the report untouched. Scan
    every cell and coerce pandas/NumPy "missing" back to a real None."""
    return [tuple(None if pd.isna(v) else v for v in row)
            for row in df.itertuples(index=False, name=None)]


def run_dalio_v2(engines: Optional[List[str]] = None, ref_year: Optional[int] = None,
                 db_path: Optional[str] = None,
                 hub_db_path: Optional[str] = None) -> Dict[str, Optional[int]]:
    """Compute the requested engines (default: all implemented so far) for
    ref_year (default: current year) and write to engine_scores. Returns
    {engine_name: n_countries_scored}. cycle_classifier's value is None
    (not 0) when it was skipped for this ref_date rather than run and empty.

    db_path     -- LazyRay's own DuckDB file (output).
    hub_db_path -- market-data-hub's DuckDB file (input, read-only).
    """
    engines = engines or list(_ENGINES.keys())
    unknown = set(engines) - set(_ENGINES)
    if unknown:
        raise ValueError(f"Unknown engine(s): {sorted(unknown)}. Known: {sorted(_ENGINES)}")

    ref_date = date(ref_year or datetime.now().year, 12, 31)
    with db_write_lock(db_path):
        con = get_conn(db_path)
        try:
            # cycle_classifier can legitimately stay None (not 0) when it was
            # skipped for this ref_date -- see the REQUIRED_ENGINES guard below.
            summary: Dict[str, Optional[int]] = {}
            # One explicit transaction across all engines: DuckDB autocommits
            # each statement otherwise, so a failure at engine 3/5 would leave
            # engine_scores in a mixed-vintage state for this ref_date.
            con.execute("BEGIN TRANSACTION")
            try:
                for name in engines:
                    df = _ENGINES[name](con, ref_date, hub_db_path=hub_db_path)
                    # Replace this (ref_date, engine) batch wholesale: a country
                    # that dropped out of coverage since the last run must not
                    # survive as a stale row with an old score/model_version.
                    con.execute(
                        "DELETE FROM engine_scores WHERE ref_date = ? AND engine = ?",
                        [ref_date, name])
                    if df.empty:
                        summary[name] = 0
                        continue
                    con.executemany(
                        "INSERT INTO engine_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        _records_with_real_nulls(df))
                    summary[name] = len(df)
                # Fase 5: classify AFTER the engine loop, in the same
                # transaction, reading engine_scores as it now stands for
                # ref_date (which includes any OTHER engine's rows already
                # committed from an earlier run, not just the ones just
                # rewritten -- a partial-engine run still refreshes the
                # classification against the full current picture). Runs
                # unconditionally: no new CLI flag needed.
                #
                # BUT: only if every gate-relevant engine has AT LEAST ONE
                # row at this exact ref_date -- i.e. has actually been
                # computed for this date at some point, not necessarily by
                # THIS call. Without this guard, running a single engine for
                # a brand-new ref_year (e.g. the first `run_dalio_v2.py
                # --engines sovereign_solvency` of a new year) would rebuild
                # dalio_cycle_v2 with mostly-unclassifiable rows for that
                # date; since the report picks the globally latest ref_date,
                # that would hide the previous, fully-classified year's rows
                # behind a worse one. A country-level data gap is still
                # handled correctly by cycle_classifier's own per-output
                # coverage gate -- this only guards against an engine that
                # has literally never run for ref_date.
                present_engines = {r[0] for r in con.execute(
                    "SELECT DISTINCT engine FROM engine_scores WHERE ref_date = ?",
                    [ref_date]).fetchall()}
                if cycle_classifier.REQUIRED_ENGINES <= present_engines:
                    cycle_df = cycle_classifier.compute(con, ref_date)
                    con.execute("DELETE FROM dalio_cycle_v2 WHERE ref_date = ?", [ref_date])
                    if not cycle_df.empty:
                        con.executemany(
                            "INSERT INTO dalio_cycle_v2 VALUES (?,?,?,?,?,?,?,?,?)",
                            _records_with_real_nulls(cycle_df))
                    summary["cycle_classifier"] = len(cycle_df)
                else:
                    summary["cycle_classifier"] = None
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            return summary
        finally:
            con.close()

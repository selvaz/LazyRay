# -*- coding: utf-8 -*-
"""
runner.py — orchestrates the Dalio v2 engines and writes engine_scores.

Additive: never touches dalio_signals/pillar_scores/regime_state (dalio.py
keeps producing the current report unchanged). See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md §1 (non-goal).

Usage:
    from lazyray.dalio_v2.runner import run_dalio_v2
    run_dalio_v2()                      # all 5 engines, ref_date = today
    run_dalio_v2(ref_date=date(2026, 8, 1))
    run_dalio_v2(engines=["sovereign_solvency"])
    run_dalio_v2(if_changed=True)       # skip (see below) when nothing moved

ref_date is the RUN date, not Dec 31 of some year (docs/DALIO_PROD_
ASSESSMENT_2026-09.md problem #1: a fixed future ref_date meant
engine_scores held one snapshot overwritten daily and prev_label() never
found an earlier ref_date, so hysteresis was never active). Distinct ref_date
values now accumulate history; a same-day rerun still replaces only that
day's (ref_date, engine) batch (DELETE-then-INSERT, unchanged).

if_changed=True compares a SHA-256 hash of the input panel actually used
against the most recent row in run_meta: if unchanged, the run is skipped
entirely (no engines computed, nothing written) and the summary carries
"skipped": True plus "since" (the ref_date the matching hash was last seen
at) -- see run_dalio_v2.py's --if-changed CLI flag for how this maps to a
distinct process exit code.

Input is read point-in-time via lazyray.dalio_v2.panel.load_panel_ext (see
each engine's compute()); output (engine_scores, dalio_cycle_v2, run_meta)
is written to LazyRay's own DB.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Dict, List, Optional, Union

import pandas as pd

from lazyray.dalio_v2 import (
    cycle_classifier, external_constraint, funding_liquidity, political_execution,
    private_credit, sovereign_solvency,
)
from lazyray.dalio_v2.panel import load_panel_ext
from lazyray.dalio_v2.scoring import git_short_sha
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


def used_indicator_ids() -> set:
    """Every hub indicator_id at least one engine actually reads (their
    module-level id maps, fallback lists flattened). --if-changed hashes
    only these rows: the hub refreshes ~83 indicators, several of them
    daily/monthly series no engine consumes (policy rates, trade shares...),
    and hashing the whole panel would make "unchanged" almost never true."""
    ids: set = set()
    for mod in (sovereign_solvency, private_credit, external_constraint,
                funding_liquidity, political_execution):
        for mapping in (getattr(mod, "_IND", {}), getattr(mod, "_WGI", {})):
            for value in mapping.values():
                ids.update([value] if isinstance(value, str) else value)
    return ids


def _panel_hash(df: pd.DataFrame) -> str:
    """Stable SHA-256 over the sorted (date, country, indicator, value) rows
    of the input panel actually used for this run -- the basis for
    --if-changed. Deterministic across runs with identical data regardless
    of row order (the hub read is not guaranteed to return rows in the same
    order every time) and across processes (float repr, not Python's
    randomized hash())."""
    if df is None or df.empty:
        return hashlib.sha256(b"").hexdigest()
    d = df[["date", "country_iso3", "indicator_id", "value"]].copy()
    d["date"] = pd.to_datetime(d["date"]).astype(str)
    d = d.sort_values(["date", "country_iso3", "indicator_id"]).reset_index(drop=True)
    lines = [
        f"{r.date}|{r.country_iso3}|{r.indicator_id}|"
        f"{'' if pd.isna(r.value) else repr(float(r.value))}"
        for r in d.itertuples(index=False)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def run_dalio_v2(engines: Optional[List[str]] = None,
                 ref_date: "Optional[Union[date, datetime]]" = None,
                 db_path: Optional[str] = None,
                 hub_db_path: Optional[str] = None,
                 if_changed: bool = False) -> Dict[str, Optional[int]]:
    """Compute the requested engines (default: all implemented so far) as of
    ref_date (default: today) and write to engine_scores. Returns
    {engine_name: n_countries_scored}. cycle_classifier's value is None
    (not 0) when it was skipped for this ref_date rather than run and empty.

    if_changed  -- if True and the input panel's hash matches the most
                   recent run_meta row (any earlier ref_date), skip the run
                   entirely and return {"skipped": True, "since": <ref_date>}
                   instead of the per-engine summary.
    db_path     -- LazyRay's own DuckDB file (output).
    hub_db_path -- market-data-hub's DuckDB file (input, read-only).
    """
    engines = engines or list(_ENGINES.keys())
    unknown = set(engines) - set(_ENGINES)
    if unknown:
        raise ValueError(f"Unknown engine(s): {sorted(unknown)}. Known: {sorted(_ENGINES)}")

    if isinstance(ref_date, datetime):
        ref_date = ref_date.date()
    ref_date = ref_date or date.today()

    with db_write_lock(db_path):
        con = get_conn(db_path)
        try:
            panel_for_hash = load_panel_ext(ref_date, hub_db_path)
            if not panel_for_hash.empty:
                panel_for_hash = panel_for_hash[panel_for_hash["indicator_id"].isin(used_indicator_ids())]
            input_hash = _panel_hash(panel_for_hash)
            if if_changed:
                prev = con.execute(
                    "SELECT input_hash, ref_date FROM run_meta "
                    "ORDER BY ref_date DESC LIMIT 1").fetchone()
                if prev is not None and prev[0] == input_hash:
                    return {"skipped": True, "since": prev[1]}

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
                        "INSERT INTO engine_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                # a brand-new ref_date (e.g. the first `run_dalio_v2.py
                # --engines sovereign_solvency` for a given day) would rebuild
                # dalio_cycle_v2 with mostly-unclassifiable rows for that
                # date; since the report picks the globally latest ref_date,
                # that would hide the previous, fully-classified run's rows
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
                    # A required engine that had rows for this exact ref_date
                    # before this call can lose them here (its batch was just
                    # replaced wholesale with an empty one -- see the DELETE
                    # above the executemany in the loop). Any dalio_cycle_v2
                    # rows a PRIOR full run left for this same ref_date would
                    # otherwise dangle, describing engines that no longer
                    # have data (Codex review). Harmless no-op when there was
                    # nothing to clear (e.g. a brand-new ref_date).
                    con.execute("DELETE FROM dalio_cycle_v2 WHERE ref_date = ?", [ref_date])
                    summary["cycle_classifier"] = None
                con.execute(
                    "INSERT OR REPLACE INTO run_meta VALUES (?,?,?,?,?,?)",
                    [ref_date, input_hash, git_short_sha(), ",".join(sorted(engines)),
                     len(panel_for_hash), datetime.now()])
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            return summary
        finally:
            con.close()

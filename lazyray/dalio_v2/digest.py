# -*- coding: utf-8 -*-
"""
digest.py — short plain-text Telegram digest for a Dalio v2 run.

Replaces the daily "send the full 300+KB HTML with a fixed caption" flow
(docs/DALIO_PROD_ASSESSMENT_2026-09.md §2/§3.1.8): with ref_date = the run
date and engine_scores now accumulating history (INSERT per distinct day
instead of one snapshot overwritten daily), a run can be summarized against
the PREVIOUS run's numbers -- what changed, and which single component drove
each mover -- in a handful of lines a human actually reads on a phone.

build_digest(con, ref_date) is pure read: it never writes anything, and it
is safe to call on an empty/first-ever database (returns the "first run, no
diff" text).
"""
from __future__ import annotations

import json
from typing import Optional

import duckdb
import pandas as pd

_MAX_MOVERS = 5


def _differs(new: pd.Series, old: pd.Series) -> pd.Series:
    """Elementwise "did this actually change", NaN/None-safe: DuckDB's
    fetch_df() can turn an all-NULL VARCHAR column (e.g. dalio_stage for a
    batch of unclassifiable countries) into a float64 NaN column, and plain
    `!=` treats NaN != NaN as True -- which would report every
    still-unclassifiable country as a "change" on every single run. Two
    missing values on both sides are NOT a change."""
    both_missing = new.isna() & old.isna()
    return (new != old) & ~both_missing


def _prev_ref_date(con: duckdb.DuckDBPyConnection, ref_date) -> Optional[object]:
    row = con.execute(
        "SELECT max(ref_date) FROM engine_scores WHERE ref_date < ?", [ref_date]
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _engine_rows(con: duckdb.DuckDBPyConnection, ref_date) -> pd.DataFrame:
    return con.execute(
        "SELECT country_iso3, engine, score, label, relative_label, components_json "
        "FROM engine_scores WHERE ref_date = ?", [ref_date]).fetch_df()


def _audit_field(js: Optional[str], key: str):
    try:
        return (json.loads(js) or {}).get(key) if js else None
    except Exception:
        return None


def _gate_and_branch_events(joined: pd.DataFrame) -> list:
    """WP2 deliverable E: surface the two "silent state change" risks a plain
    label-diff would miss -- a country entering/leaving sovereign_solvency's
    fx_constrained gate (label itself changes, so the generic label-change
    count already counts it, but not WHY), and a country's funding_liquidity
    data branch moving between market/external/none (branch can change with
    the label unchanged, e.g. a country losing its 10Y print and falling
    back from 'market' to 'external' at a similar score)."""
    lines = []
    if "sovereign_solvency" in joined.index.get_level_values("engine"):
        sov = joined.xs("sovereign_solvency", level="engine")
        for country, row in sov.iterrows():
            old_gate = _audit_field(row["components_json_old"], "gate")
            new_gate = _audit_field(row["components_json_new"], "gate")
            if bool(old_gate) != bool(new_gate):
                verb = "entered" if new_gate else "left"
                lines.append(f"  {country}: {verb} fx_constrained")
    if "funding_liquidity" in joined.index.get_level_values("engine"):
        fnd = joined.xs("funding_liquidity", level="engine")
        for country, row in fnd.iterrows():
            old_branch = _audit_field(row["components_json_old"], "branch")
            new_branch = _audit_field(row["components_json_new"], "branch")
            if old_branch and new_branch and old_branch != new_branch:
                lines.append(f"  {country}: funding branch {old_branch} -> {new_branch}")
    return lines


def _biggest_component_delta(old_json: Optional[str], new_json: Optional[str]) -> Optional[str]:
    """Name of the component whose score moved the most between the two
    audit trails (shared components only). None if either side is missing
    or unparseable, or nothing overlaps."""
    try:
        old_c = (json.loads(old_json) or {}).get("components", {}) if old_json else {}
        new_c = (json.loads(new_json) or {}).get("components", {}) if new_json else {}
    except Exception:
        return None
    best_name, best_delta = None, -1.0
    for name, new_comp in new_c.items():
        old_comp = old_c.get(name)
        if not isinstance(old_comp, dict) or not isinstance(new_comp, dict):
            continue
        os_, ns = old_comp.get("score"), new_comp.get("score")
        if os_ is None or ns is None:
            continue
        delta = abs(float(ns) - float(os_))
        if delta > best_delta:
            best_name, best_delta = name, delta
    return best_name


def build_digest(con: duckdb.DuckDBPyConnection, ref_date) -> str:
    """Short plain-text Telegram digest (<= ~30 lines) for the engine_scores/
    dalio_cycle_v2 rows at ref_date, diffed against the previous ref_date
    that exists in engine_scores (strictly earlier -- never a same-day
    rerun). "first run, no diff" if no earlier ref_date exists at all."""
    cur = _engine_rows(con, ref_date)
    data_through_year = ref_date.year - 1
    n_countries = cur["country_iso3"].nunique() if not cur.empty else 0
    lines = [
        f"LazyRay Dalio v2 | as of {ref_date} | data through "
        f"{data_through_year} | {n_countries} countries",
    ]

    prev_date = _prev_ref_date(con, ref_date)
    if prev_date is None or cur.empty:
        lines.append("first run, no diff")
        lines.append("Limits: not validated; proxy-tier scores marked; "
                     f"levels from actuals through {data_through_year}.")
        return "\n".join(lines)

    prev = _engine_rows(con, prev_date)
    cur_idx = cur.set_index(["country_iso3", "engine"])
    prev_idx = prev.set_index(["country_iso3", "engine"])
    shared = cur_idx.index.intersection(prev_idx.index)

    # --- per-engine label-change counts ---------------------------------
    if len(shared):
        joined = cur_idx.loc[shared, ["label", "score", "relative_label", "components_json"]].join(
            prev_idx.loc[shared, ["label", "score", "relative_label", "components_json"]],
            lsuffix="_new", rsuffix="_old")
        changed = joined[_differs(joined["label_new"], joined["label_old"])]
        by_engine = changed.groupby(level="engine").size()
        for engine in sorted(cur_idx.index.get_level_values("engine").unique()):
            n = int(by_engine.get(engine, 0))
            lines.append(f"{engine}: {n} label change(s) vs {prev_date}")

        # --- top movers by |score delta| ---------------------------------
        deltas = (joined["score_new"] - joined["score_old"]).abs()
        movers = deltas.dropna().sort_values(ascending=False).head(_MAX_MOVERS)
        if len(movers):
            lines.append(f"Top movers vs {prev_date}:")
            for (country, engine) in movers.index:
                row = joined.loc[(country, engine)]
                comp = _biggest_component_delta(row["components_json_old"], row["components_json_new"])
                comp_txt = f" ({comp})" if comp else ""
                rel_old, rel_new = row.get("relative_label_old"), row.get("relative_label_new")
                rel_txt = f" [relative: {rel_old} -> {rel_new}]" \
                    if pd.notna(rel_old) and pd.notna(rel_new) and rel_old != rel_new else ""
                lines.append(
                    f"  {country}/{engine}: {row['score_old']:.1f} -> {row['score_new']:.1f}"
                    f" ({row['label_old']} -> {row['label_new']}){comp_txt}{rel_txt}")

        events = _gate_and_branch_events(joined)
        if events:
            lines.append("Gate/branch changes:")
            lines.extend(events[:_MAX_MOVERS])
    else:
        lines.append("no overlapping (country, engine) pairs vs the previous run")

    # --- cycle-stage changes ---------------------------------------------
    cyc_cur = con.execute(
        "SELECT country_iso3, dalio_stage FROM dalio_cycle_v2 WHERE ref_date = ?",
        [ref_date]).fetch_df()
    cyc_prev = con.execute(
        "SELECT country_iso3, dalio_stage FROM dalio_cycle_v2 WHERE ref_date = ?",
        [prev_date]).fetch_df()
    if not cyc_cur.empty and not cyc_prev.empty:
        m = cyc_cur.merge(cyc_prev, on="country_iso3", suffixes=("_new", "_old"))
        stage_changed = m[_differs(m["dalio_stage_new"], m["dalio_stage_old"])]
        lines.append(f"Cycle-stage changes: {len(stage_changed)}")
        for _, r in stage_changed.head(_MAX_MOVERS).iterrows():
            lines.append(f"  {r['country_iso3']}: {r['dalio_stage_old']} -> {r['dalio_stage_new']}")

    lines.append("Limits: not validated; proxy-tier scores marked; "
                 f"levels from actuals through {data_through_year}.")
    return "\n".join(lines)

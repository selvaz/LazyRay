# -*- coding: utf-8 -*-
"""
data.py — pure readers for the LazyRay Brief (docs/DALIO_PROD_ASSESSMENT_2026-09.md
proposes it as the single human-facing output; this module builds the plain
data model both text.py and html.py render from).

ONLY reads from LazyRay's own DuckDB: engine_scores (latest + previous
ref_date), dalio_cycle_v2, run_meta, stress_index, stress_components. Never
touches the hub. Every read degrades gracefully: an empty/first-ever DB, a
DB with no stress tables at all (they are created by
lazyray.stress.persist.apply_schema, not by lazyray.db.connection's own
schema.sql -- see run_stress_monitor.py), and a DB with stress rows for some
regions but not others must all produce a usable BriefData, never raise.

Reuses lazyray.dalio_v2.digest's `_differs` (NaN-safe change detection) and
`_biggest_component_delta` (name of the component that moved most between
two components_json blobs) rather than re-implementing them here -- see
docs/DALIO_PROD_ASSESSMENT_2026-09.md and that module's own docstring for
why the NaN-safety matters (DuckDB's fetch_df() can turn an all-NULL VARCHAR
column into float64 NaN, and plain != treats NaN != NaN as True).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Optional

import duckdb
import pandas as pd

from lazyray.config_loader import get_settings
from lazyray.dalio_v2.digest import _biggest_component_delta, _differs

ENGINE_ORDER = (
    "sovereign_solvency", "external_constraint", "private_credit",
    "funding_liquidity", "political_execution",
)
#: Short tokens used in the compact Telegram text (watchlist reasons etc.) --
#: deliberately terse, not full Italian words, to keep the ASCII digest short.
ENGINE_SHORT = {
    "sovereign_solvency": "sovereign", "external_constraint": "external",
    "private_credit": "private", "funding_liquidity": "funding",
    "political_execution": "political",
}
REGIONS = ("us", "ea", "em")
REGION_NAME_IT = {"us": "USA", "ea": "Area euro", "em": "Mercati emergenti"}
REGION_CODE = {"us": "USA", "ea": "EA", "em": "EM"}

_DSA_WATCHLIST_THRESHOLD = 0.70
_MOVER_THRESHOLD = 2.0
_MAX_MOVERS = 5
_STRESS_HISTORY_DAYS = 90


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return row is not None


def _country_meta() -> dict:
    """iso3 -> {"name": ..., "group": DM/EM/Frontier}, from market-data-hub's
    static countries.yaml (package data, never duplicated here)."""
    try:
        from market_data_hub.config_loader import get_countries
        return {c["iso3"]: {"name": c.get("name", c["iso3"]),
                            "group": c.get("development", "EM")}
               for c in get_countries()}
    except Exception:
        return {}


def _top_two_labels(engine: str) -> set:
    cfg = get_settings().get("dalio_v2", {}).get(engine, {})
    labels = cfg.get("bucket_labels") or []
    return set(labels[-2:]) if len(labels) >= 2 else set(labels)


def _bucket_index(engine: str, label: Optional[str]) -> Optional[int]:
    """0-based position of `label` in the engine's own bucket_labels list, or
    None for a label that is not a plain bucket member (fx_constrained is a
    post-assignment override, not itself in bucket_labels; see
    sovereign_solvency.py)."""
    if not label:
        return None
    labels = get_settings().get("dalio_v2", {}).get(engine, {}).get("bucket_labels") or []
    return labels.index(label) if label in labels else None


def _clean(v):
    """None for both a real None and DuckDB's fetch_df() NaN-for-NULL
    artifact on an object/VARCHAR column (see lazyray.dalio_v2.digest's
    module docstring for why an all-NULL column can surface as float64 NaN
    instead of None) -- callers downstream (text.py/html.py) only ever see
    a real value or None, never a stray NaN they'd have to guard against
    themselves."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def _audit(components_json: Optional[str]) -> dict:
    if not components_json:
        return {}
    try:
        return json.loads(components_json) or {}
    except Exception:
        return {}


@dataclass
class EngineCell:
    engine: str
    label: Optional[str] = None
    relative_label: Optional[str] = None
    score: Optional[float] = None
    relative_score: Optional[float] = None
    coverage_tier: Optional[str] = None
    effective_score: Optional[float] = None  # relative_score, else score


@dataclass
class CountryRow:
    iso3: str
    name: str
    group: str
    rank_score: Optional[float]
    engines: "dict[str, EngineCell]" = field(default_factory=dict)
    gate: Optional[dict] = None
    funding_branch: Optional[str] = None
    dsa: Optional[dict] = None  # {p_up, p10, p50, p90}
    cycle_stage: Optional[str] = None
    deleveraging_type: Optional[str] = None


@dataclass
class StressPoint:
    date: _date
    index_value: float
    regime: Optional[str]


@dataclass
class StressRegion:
    region: str
    has_data: bool = False
    date: Optional[_date] = None
    index_value: Optional[float] = None
    p_stress: Optional[float] = None
    regime: Optional[str] = None
    regime_hmm: Optional[str] = None
    n_segments: int = 0
    expected_segments: int = 0
    partial: bool = False
    pct: Optional[float] = None
    week_change_pp: Optional[float] = None
    month_change_pp: Optional[float] = None
    top_segments: list = field(default_factory=list)   # [(segment, pct)]
    missing_segments: list = field(default_factory=list)
    history: "list[StressPoint]" = field(default_factory=list)


@dataclass
class LabelChange:
    country: str
    engine: str
    old_label: Optional[str]
    new_label: Optional[str]
    component: Optional[str]
    old_component_score: Optional[float]
    new_component_score: Optional[float]


@dataclass
class StageChange:
    country: str
    old_stage: Optional[str]
    new_stage: Optional[str]


@dataclass
class Mover:
    country: str
    engine: str
    old_score: Optional[float]
    new_score: Optional[float]
    delta: float
    old_label: Optional[str]
    new_label: Optional[str]
    component: Optional[str]


@dataclass
class Changes:
    first_run: bool
    prev_ref_date: Optional[_date]
    label_changes: "list[LabelChange]" = field(default_factory=list)
    stage_changes: "list[StageChange]" = field(default_factory=list)
    movers: "list[Mover]" = field(default_factory=list)


@dataclass
class WatchItem:
    iso3: str
    name: str
    reasons: "list[str]"


@dataclass
class Limits:
    data_through_year: int
    stress_partial_regions: "list[str]"
    coverage_counts: dict  # {"proxy": n, "insufficient": n, "no_data": n}


@dataclass
class BriefData:
    as_of: _date
    prev_ref_date: Optional[_date]
    data_through_year: int
    model_version: str
    n_countries: int
    countries: "list[CountryRow]"
    stress: "dict[str, StressRegion]"
    changes: Changes
    watchlist: "list[WatchItem]"
    limits: Limits


def _expected_segments(region: str) -> int:
    try:
        from lazyray.stress.config import REGIONS as STRESS_REGIONS
        return len(STRESS_REGIONS[region].segments)
    except Exception:
        return 0


def _engine_rows_df(con: duckdb.DuckDBPyConnection, ref_date) -> pd.DataFrame:
    return con.execute(
        "SELECT country_iso3, engine, score, label, relative_score, relative_label, "
        "coverage_tier, components_json FROM engine_scores WHERE ref_date = ?",
        [ref_date]).fetch_df()


def _prev_ref_date(con: duckdb.DuckDBPyConnection, ref_date) -> Optional[_date]:
    row = con.execute(
        "SELECT max(ref_date) FROM engine_scores WHERE ref_date < ?", [ref_date]
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _model_version(con: duckdb.DuckDBPyConnection, ref_date, cur: pd.DataFrame) -> str:
    row = con.execute("SELECT model_version FROM run_meta WHERE ref_date = ?",
                      [ref_date]).fetchone()
    if row and row[0]:
        return str(row[0])
    for js in cur["components_json"]:
        v = _audit(js).get("model_version")
        if v:
            return str(v)
    return "unknown"


def _build_countries(cur: pd.DataFrame, cycle: pd.DataFrame, meta: dict
                     ) -> "list[CountryRow]":
    cycle_by_country = {r["country_iso3"]: r for _, r in cycle.iterrows()} \
        if not cycle.empty else {}
    rows: "list[CountryRow]" = []
    for iso3, g in cur.groupby("country_iso3"):
        m = meta.get(iso3, {"name": iso3, "group": "EM"})
        cells: "dict[str, EngineCell]" = {}
        for _, r in g.iterrows():
            score = None if pd.isna(r["score"]) else float(r["score"])
            rel_score = None if pd.isna(r["relative_score"]) else float(r["relative_score"])
            effective = rel_score if rel_score is not None else score
            cells[r["engine"]] = EngineCell(
                engine=r["engine"],
                label=None if pd.isna(r["label"]) else r["label"],
                relative_label=None if pd.isna(r["relative_label"]) else r["relative_label"],
                score=score, relative_score=rel_score,
                coverage_tier=None if pd.isna(r["coverage_tier"]) else r["coverage_tier"],
                effective_score=effective,
            )
        available = [c.effective_score for c in cells.values() if c.effective_score is not None]
        rank_score = sum(available) / len(available) if available else None

        gate = None
        funding_branch = None
        dsa = None
        sov_row = g[g["engine"] == "sovereign_solvency"]
        if not sov_row.empty:
            audit = _audit(sov_row.iloc[0]["components_json"])
            gate = audit.get("gate")
            dsa_audit = audit.get("dsa")
            if isinstance(dsa_audit, dict) and "p10" in dsa_audit:
                p_up = (audit.get("components", {}).get("debt_p_up_5y") or {}).get("raw_value")
                dsa = {"p_up": p_up, "p10": dsa_audit["p10"], "p50": dsa_audit["p50"],
                      "p90": dsa_audit["p90"]}
        fnd_row = g[g["engine"] == "funding_liquidity"]
        if not fnd_row.empty:
            funding_branch = _audit(fnd_row.iloc[0]["components_json"]).get("branch")

        cyc = cycle_by_country.get(iso3)
        cycle_stage = cyc["dalio_stage"] if cyc is not None and pd.notna(cyc.get("dalio_stage")) else None
        deleveraging_type = cyc["deleveraging_type"] \
            if cyc is not None and pd.notna(cyc.get("deleveraging_type")) else None

        rows.append(CountryRow(
            iso3=iso3, name=m["name"], group=m["group"], rank_score=rank_score,
            engines=cells, gate=gate, funding_branch=funding_branch, dsa=dsa,
            cycle_stage=cycle_stage, deleveraging_type=deleveraging_type,
        ))
    rows.sort(key=lambda r: (r.rank_score is None, -(r.rank_score or 0.0), r.iso3))
    return rows


def _stress_region(con: duckdb.DuckDBPyConnection, region: str, as_of) -> StressRegion:
    out = StressRegion(region=region, expected_segments=_expected_segments(region))
    if not _table_exists(con, "stress_index"):
        return out
    row = con.execute(
        "SELECT date, index_value, p_stress, regime, regime_hmm, n_segments "
        "FROM stress_index WHERE region = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        [region, as_of]).fetchone()
    if not row:
        return out
    dt, value, p_stress, regime, regime_hmm, n_segments = row
    out.has_data = True
    out.date, out.index_value, out.p_stress = dt, value, p_stress
    out.regime, out.regime_hmm, out.n_segments = regime, regime_hmm, int(n_segments)
    out.partial = out.n_segments < out.expected_segments

    history_full = con.execute(
        "SELECT index_value FROM stress_index WHERE region = ? AND date <= ? ORDER BY date",
        [region, dt]).fetch_df().iloc[:, 0]
    out.pct = round(100 * float((history_full <= value).mean()))

    prior = con.execute(
        "SELECT date, index_value FROM stress_index WHERE region = ? AND date < ? "
        "ORDER BY date DESC LIMIT 22", [region, dt]).fetchall()
    week_prior = prior[4][1] if len(prior) > 4 else None
    month_prior = prior[20][1] if len(prior) > 20 else None
    out.week_change_pp = (value - week_prior) * 100.0 if week_prior is not None else None
    out.month_change_pp = (value - month_prior) * 100.0 if month_prior is not None else None

    if out.partial and _table_exists(con, "stress_components"):
        present = {x[0] for x in con.execute(
            "SELECT DISTINCT segment FROM stress_components WHERE region = ? AND date = ?",
            [region, dt]).fetchall()}
        try:
            from lazyray.stress.config import REGIONS as STRESS_REGIONS
            out.missing_segments = [s.name for s in STRESS_REGIONS[region].segments
                                    if s.name not in present]
        except Exception:
            pass

    if _table_exists(con, "stress_components"):
        out.top_segments = con.execute(
            "SELECT segment, AVG(pct) p FROM stress_components WHERE region = ? AND date = ? "
            "GROUP BY segment ORDER BY p DESC LIMIT 2", [region, dt]).fetchall()

    hist_df = con.execute(
        "SELECT date, index_value, regime FROM stress_index WHERE region = ? AND date <= ? "
        "ORDER BY date DESC LIMIT ?", [region, dt, _STRESS_HISTORY_DAYS]).fetch_df()
    if not hist_df.empty:
        hist_df = hist_df.sort_values("date")
        out.history = [StressPoint(date=r["date"], index_value=float(r["index_value"]),
                                   regime=r["regime"])
                       for _, r in hist_df.iterrows()]
    return out


def _country_short_name(name: str, iso3: str) -> str:
    return f"{name} ({iso3})" if name != iso3 else iso3


def _build_changes(con: duckdb.DuckDBPyConnection, ref_date, prev_date,
                   cur: pd.DataFrame) -> Changes:
    if prev_date is None or cur.empty:
        return Changes(first_run=True, prev_ref_date=None)

    prev = _engine_rows_df(con, prev_date)
    cur_idx = cur.set_index(["country_iso3", "engine"])
    prev_idx = prev.set_index(["country_iso3", "engine"])
    shared = cur_idx.index.intersection(prev_idx.index)

    label_changes: "list[LabelChange]" = []
    movers: "list[Mover]" = []
    if len(shared):
        cols = ["label", "score", "components_json"]
        joined = cur_idx.loc[shared, cols].join(prev_idx.loc[shared, cols],
                                                lsuffix="_new", rsuffix="_old")
        changed = joined[_differs(joined["label_new"], joined["label_old"])]
        for (country, engine), row in changed.iterrows():
            comp = _biggest_component_delta(row["components_json_old"], row["components_json_new"])
            old_c = new_c = None
            if comp:
                old_audit = _audit(row["components_json_old"]).get("components", {})
                new_audit = _audit(row["components_json_new"]).get("components", {})
                old_c = (old_audit.get(comp) or {}).get("score")
                new_c = (new_audit.get(comp) or {}).get("score")
            label_changes.append(LabelChange(
                country=country, engine=engine, old_label=_clean(row["label_old"]),
                new_label=_clean(row["label_new"]), component=comp,
                old_component_score=_clean(old_c), new_component_score=_clean(new_c)))

        deltas = (joined["score_new"] - joined["score_old"]).abs()
        big = deltas[deltas >= _MOVER_THRESHOLD].dropna().sort_values(ascending=False)
        for (country, engine) in big.index[:_MAX_MOVERS]:
            row = joined.loc[(country, engine)]
            comp = _biggest_component_delta(row["components_json_old"], row["components_json_new"])
            movers.append(Mover(
                country=country, engine=engine,
                old_score=_clean(row["score_old"]), new_score=_clean(row["score_new"]),
                delta=float(row["score_new"] - row["score_old"]),
                old_label=_clean(row["label_old"]), new_label=_clean(row["label_new"]),
                component=comp))

    stage_changes: "list[StageChange]" = []
    cyc_cur = con.execute("SELECT country_iso3, dalio_stage FROM dalio_cycle_v2 WHERE ref_date = ?",
                          [ref_date]).fetch_df()
    cyc_prev = con.execute("SELECT country_iso3, dalio_stage FROM dalio_cycle_v2 WHERE ref_date = ?",
                           [prev_date]).fetch_df()
    if not cyc_cur.empty and not cyc_prev.empty:
        m = cyc_cur.merge(cyc_prev, on="country_iso3", suffixes=("_new", "_old"))
        changed_stage = m[_differs(m["dalio_stage_new"], m["dalio_stage_old"])]
        for _, r in changed_stage.iterrows():
            stage_changes.append(StageChange(
                country=r["country_iso3"], old_stage=_clean(r["dalio_stage_old"]),
                new_stage=_clean(r["dalio_stage_new"])))

    return Changes(first_run=False, prev_ref_date=prev_date, label_changes=label_changes,
                   stage_changes=stage_changes, movers=movers)


def dsa_is_material(c: "CountryRow", threshold: float = _DSA_WATCHLIST_THRESHOLD) -> bool:
    """p_up measures direction, not level: a low-debt country drifting up from
    30% of GDP is not a watch item. Require a non-low sovereign burden too."""
    if not c.dsa or c.dsa.get("p_up") is None or c.dsa["p_up"] < threshold:
        return False
    sov = c.engines.get("sovereign_solvency")
    return bool(sov and sov.label and sov.label != "low")


def _build_watchlist(countries: "list[CountryRow]") -> "list[WatchItem]":
    out: "list[WatchItem]" = []
    for c in countries:
        reasons = []
        if c.gate:
            reasons.append("fx: " + str(c.gate.get("reason") or "").replace("+", "+"))
        # political_execution is a cross-country percentile: by construction
        # 12 countries are always "weak" and 12 "impaired", so it cannot put
        # a country on the list by itself -- it is appended as context only.
        for engine in ENGINE_ORDER:
            if engine == "political_execution":
                continue
            cell = c.engines.get(engine)
            if cell and cell.label and cell.label in _top_two_labels(engine):
                reasons.append(f"{ENGINE_SHORT[engine]} {cell.label}")
        if dsa_is_material(c):
            dsa = c.dsa
            assert dsa is not None  # dsa_is_material(c) already proved c.dsa is truthy
            reasons.append(f"dsa {dsa['p_up'] * 100:.0f}%")
        pol = c.engines.get("political_execution")
        if reasons and pol and pol.label and pol.label in _top_two_labels("political_execution"):
            reasons.append(f"political {pol.label}")
        # "stress-region membership": stress is regional, not per-country --
        # no country-level filter exists to apply here (spec: "n/a").
        if reasons:
            out.append(WatchItem(iso3=c.iso3, name=c.name, reasons=reasons))
    return out


def _build_limits(cur: pd.DataFrame, stress: "dict[str, StressRegion]",
                  data_through_year: int) -> Limits:
    partial = [r.region for r in stress.values() if r.has_data and r.partial]
    counts = {"proxy": 0, "insufficient": 0, "no_data": 0}
    if not cur.empty:
        vc = cur["coverage_tier"].value_counts()
        for tier in counts:
            counts[tier] = int(vc.get(tier, 0))
    return Limits(data_through_year=data_through_year, stress_partial_regions=partial,
                 coverage_counts=counts)


def build_brief_data(con: duckdb.DuckDBPyConnection, ref_date) -> BriefData:
    """Pure read: assembles everything text.py/html.py need from LazyRay's own
    DB for `ref_date`. Never raises on an empty/first-run/no-stress-tables
    database -- see module docstring."""
    cur = _engine_rows_df(con, ref_date)
    prev_date = _prev_ref_date(con, ref_date)
    n_countries = cur["country_iso3"].nunique() if not cur.empty else 0
    data_through_year = ref_date.year - 1
    model_version = _model_version(con, ref_date, cur) if not cur.empty else "unknown"

    meta = _country_meta()
    cycle = con.execute(
        "SELECT country_iso3, dalio_stage, deleveraging_type FROM dalio_cycle_v2 "
        "WHERE ref_date = ?", [ref_date]).fetch_df() if not cur.empty else pd.DataFrame()
    countries = _build_countries(cur, cycle, meta) if not cur.empty else []

    stress = {region: _stress_region(con, region, ref_date) for region in REGIONS}
    changes = _build_changes(con, ref_date, prev_date, cur)
    watchlist = _build_watchlist(countries)
    limits = _build_limits(cur, stress, data_through_year)

    return BriefData(
        as_of=ref_date, prev_ref_date=prev_date, data_through_year=data_through_year,
        model_version=model_version, n_countries=n_countries, countries=countries,
        stress=stress, changes=changes, watchlist=watchlist, limits=limits,
    )

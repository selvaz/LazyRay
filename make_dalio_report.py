# -*- coding: utf-8 -*-
"""
make_dalio_report.py — interactive Ray Dalio dashboard (single-file HTML, English).
The unified v1+v2 report: v1's classification/charts plus the Dalio v2
5-engine country risk scores (additive, see
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md) on the SAME country
sheet, read straight from engine_scores if run_dalio_v2.py has been run
against this DB (the section degrades to a hint if it hasn't, instead of
failing). run_dalio_v2.py's own report.py remains a lighter, v2-only
alternative for a fast refresh that doesn't need v1's regime_state/
pillar_scores/country_classification to be populated.

Tabs:
  1. Overview     — four-box matrix, debt-cycle phase distribution, cross-country
                    table (+ v2 risk column if v2 has been run)
  2. Countries    — per-country sheet: classification, phase/regime, pillar scores,
                    Dalio v2 5-engine scores, historical charts (click any chart ->
                    interactive modal with tooltip), stale-data alerts
  3. Methodology  — the Ray Dalio method explained
  4. Statistics   — every statistic used + its economic meaning

Self-contained: data embedded as JSON, charts in vanilla-JS SVG, no external deps.

Usage:
    python make_dalio_report.py [--open] [--calc] [--calc-v2]
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from lazyray.config_loader import get_settings  # noqa: E402
from lazyray.db.connection import get_conn  # noqa: E402


def _report_dir() -> Path:
    cfg = get_settings().get("reports", {})
    path = Path(cfg.get("dir") or "reports")
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


REPORT_DIR = _report_dir()
# a country's indicator is flagged stale only if it lags the cross-country
# FRONTIER (the most recent year available for that indicator, across countries)
# by more than this many years. Globally-laggy series (tourism, resource rents)
# therefore do NOT flag everyone — only genuine country-specific gaps do.
STALE_GAP = 3

# indicators shown as historical charts on each country sheet (label)
CHART_INDICATORS = [
    ("public_debt_gdp",     "Public debt (% GDP)"),
    ("gdp_growth_weo",      "Real GDP growth (%)"),
    ("inflation_avg_weo",   "Inflation (%)"),
    ("bis_policy_rate",     "Policy rate (%)"),
    ("bis_credit_gap",      "Credit-to-GDP gap (pp)"),
    ("bis_dsr_private",     "Debt service ratio (%)"),
    ("current_account_gdp", "Current account (% GDP)"),
    ("fiscal_balance_gdp",  "Fiscal balance (% GDP)"),
]
# additional analysis inputs to check for staleness (mostly laggy WDI series)
STALE_CHECK = {
    "tourism_exports_share": "Tourism (% of exports)",
    "natural_resource_rents_gdp": "Natural resource rents (% GDP)",
    "fuel_exports_share": "Fuel exports (% merch. exports)",
    "fuel_imports_share": "Fuel imports (% merch. imports)",
    "remittances_gdp": "Remittances (% GDP)",
    "metals_exports_share": "Metals exports (% merch. exports)",
    "npl_ratio": "Bank NPLs (% loans)",
    "bank_capital_ratio": "Bank capital ratio",
}


def _country_names():
    try:
        from market_data_hub.config_loader import get_countries
        return {x["iso3"]: x.get("name", x["iso3"]) for x in get_countries()}
    except Exception:
        return {}


def collect(con, hub_db_path=None) -> dict:
    """
    con         -- LazyRay's own DB connection (regime_state/pillar_scores/
                   engine_scores/dalio_cycle_v2/country_classification).
    hub_db_path -- market-data-hub's DB (input, read-only, for macro_panel
                   history/freshness). Opened internally, independent of con.
    """
    from market_data_hub.db.connection import get_conn as _get_hub_conn
    hub_con = _get_hub_conn(hub_db_path, read_only=True)
    try:
        return _collect(con, hub_con)
    finally:
        hub_con.close()


def _collect(con, hub_con) -> dict:
    names = _country_names()
    reg = con.execute("SELECT * FROM regime_state").fetch_df()
    comp = con.execute(
        "SELECT country_iso3, score AS composite, debt_cycle_phase, short_cycle_pos, "
        "gi_regime FROM pillar_scores WHERE pillar='COMPOSITE'").fetch_df()
    pil = con.execute(
        "SELECT country_iso3, pillar, score FROM pillar_scores WHERE pillar<>'COMPOSITE'"
    ).fetch_df()

    ids = [i for i, _ in CHART_INDICATORS]
    hist = hub_con.execute(
        "SELECT country_iso3, indicator_id, year(date) AS y, value FROM macro_panel "
        "WHERE indicator_id IN (" + ",".join("?" * len(ids)) + ") AND value IS NOT NULL "
        "QUALIFY row_number() OVER (PARTITION BY country_iso3, indicator_id, year(date) "
        "ORDER BY date DESC)=1", ids).fetch_df()

    # data freshness: latest actual year per (country, indicator). A country is
    # flagged only if it lags the cross-country FRONTIER for that indicator by
    # > STALE_GAP years (so globally-laggy series don't flag everyone).
    labels = {**dict(CHART_INDICATORS), **STALE_CHECK}
    chk_ids = list(labels)
    fresh = hub_con.execute(
        "SELECT country_iso3, indicator_id, max(year(date)) AS ly FROM macro_panel "
        "WHERE indicator_id IN (" + ",".join("?" * len(chk_ids)) + ") AND value IS NOT NULL "
        "GROUP BY 1,2", chk_ids).fetch_df()
    cur_year = datetime.now().year
    frontier = fresh.groupby("indicator_id")["ly"].max().to_dict()   # most recent across countries
    stale_by = {}
    for _, r in fresh.iterrows():
        ly, fr = int(r["ly"]), int(frontier[r["indicator_id"]])
        if fr - ly > STALE_GAP:
            stale_by.setdefault(r["country_iso3"], []).append(
                {"label": labels[r["indicator_id"]], "year": ly, "frontier": fr})

    try:
        cls = con.execute("SELECT * FROM country_classification").fetch_df()
        cls_by = {r["country_iso3"]: r.to_dict() for _, r in cls.iterrows()}
    except Exception:
        cls_by = {}

    # Dalio v2 (5-engine architecture, additive -- see
    # docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md). Optional: the
    # engine_scores table may not exist yet or be empty if run_dalio_v2.py
    # was never run against this DB; degrade to "no v2 section" rather than
    # failing the whole v1 report.
    v2_by = {}
    try:
        # Latest ref_date PER ENGINE, not globally: a partial rerun of one
        # engine in a new year must not make the other engines' (older but
        # still latest) rows vanish from the dashboard.
        v2 = con.execute(
            "SELECT e.country_iso3, e.engine, e.score, e.label, e.coverage_tier, "
            "e.confidence, e.n_components, e.n_expected, e.components_json "
            "FROM engine_scores e JOIN (SELECT engine, max(ref_date) AS ref_date "
            "FROM engine_scores GROUP BY engine) m "
            "ON e.engine = m.engine AND e.ref_date = m.ref_date").fetch_df()
    except duckdb.CatalogException:
        v2 = None   # engine_scores doesn't exist yet: v2 never run on this DB
    if v2 is not None:
        for _, r in v2.iterrows():
            try:
                comps = json.loads(r["components_json"]).get("components", {})
            except Exception:
                comps = {}
            v2_by.setdefault(r["country_iso3"], {})[r["engine"]] = {
                "score": None if pd.isna(r["score"]) else round(float(r["score"]), 2),
                "label": None if pd.isna(r["label"]) else r["label"],
                "coverage_tier": None if pd.isna(r["coverage_tier"]) else r["coverage_tier"],
                "confidence": None if pd.isna(r["confidence"]) else r["confidence"],
                "n_components": None if pd.isna(r["n_components"]) else int(r["n_components"]),
                "n_expected": None if pd.isna(r["n_expected"]) else int(r["n_expected"]),
                "components": comps,
            }

    # Fase 5 cycle classifier (dalio_cycle_v2) -- one row per country per
    # ref_date (not per engine, so a plain global MAX(ref_date) is correct
    # here, unlike the per-engine query above). Same degrade-cleanly pattern
    # if the table doesn't exist yet on this DB.
    cycle_by = {}
    try:
        cyc = con.execute(
            "SELECT country_iso3, dalio_stage, deleveraging_type, overall_confidence, "
            "top_risk_drivers_json, caveats_json FROM dalio_cycle_v2 WHERE ref_date = "
            "(SELECT max(ref_date) FROM dalio_cycle_v2)").fetch_df()
    except duckdb.CatalogException:
        cyc = None   # dalio_cycle_v2 doesn't exist yet: Fase 5 never run on this DB
    if cyc is not None:
        for _, r in cyc.iterrows():
            try:
                drivers = json.loads(r["top_risk_drivers_json"]) if r["top_risk_drivers_json"] else []
            except Exception:
                drivers = []
            try:
                caveats = json.loads(r["caveats_json"]) if r["caveats_json"] else []
            except Exception:
                caveats = []
            cycle_by[r["country_iso3"]] = {
                "dalio_stage": None if pd.isna(r["dalio_stage"]) else r["dalio_stage"],
                "deleveraging_type": None if pd.isna(r["deleveraging_type"]) else r["deleveraging_type"],
                "overall_confidence": None if pd.isna(r["overall_confidence"]) else r["overall_confidence"],
                "top_risk_drivers": drivers,
                "caveats": caveats,
            }

    h = hub_con.execute("SELECT max(date) FROM macro_panel WHERE provider_dataset='WEO'").fetchone()[0]
    weo_horizon = pd.Timestamp(h).year if h is not None else None

    reg_by = {r["country_iso3"]: r for _, r in reg.iterrows()}
    comp_by = {r["country_iso3"]: r for _, r in comp.iterrows()}
    pil_by = {}
    for _, r in pil.iterrows():
        pil_by.setdefault(r["country_iso3"], {})[r["pillar"]] = (
            None if pd.isna(r["score"]) else round(float(r["score"]), 3))
    ser_by = {}
    for _, r in hist.iterrows():
        ser_by.setdefault(r["country_iso3"], {}).setdefault(r["indicator_id"], []).append(
            [int(r["y"]), round(float(r["value"]), 2)])
    for iso in ser_by:
        for ind in ser_by[iso]:
            ser_by[iso][ind].sort()

    def _v(x):
        return None if x is None or pd.isna(x) else round(float(x), 2)

    countries = {}
    # union of v1 (regime_state), v2 (engine_scores) and Fase 5 (dalio_cycle_v2)
    # coverage: a country scored only by v2/the classifier must still get a
    # sheet, not be silently dropped because v1's classification never ran
    for iso in sorted(set(reg_by) | set(v2_by) | set(cycle_by)):
        r = reg_by.get(iso)
        c = comp_by.get(iso, {})
        countries[iso] = {
            "name": names.get(iso, iso),
            "phase": r["debt_cycle_phase"] if r is not None else None,
            "quadrant": r["quadrant"] if r is not None else None,
            "delev": r["deleveraging_quality"] if r is not None else None,
            "nom_growth": _v(r["nom_growth"]) if r is not None else None,
            "nom_rate": _v(r["nom_rate"]) if r is not None else None,
            "credit_gap": _v(r["credit_gap"]) if r is not None else None,
            "dsr": _v(r["dsr"]) if r is not None else None,
            "debt_income_gap": _v(r["debt_income_gap"]) if r is not None else None,
            "debt_trend": _v(r["debt_trend"]) if r is not None else None,
            "composite": _v(c.get("composite")) if len(c) else None,
            "pillars": pil_by.get(iso, {}),
            "series": ser_by.get(iso, {}),
            "stale": stale_by.get(iso, []),
            "cls": {k: (None if pd.isna(v) else v) for k, v in cls_by.get(iso, {}).items()
                    if k not in ("country_iso3", "name", "computed_at")},
            # Dalio v2 5-engine scores, if run_dalio_v2.py has populated
            # engine_scores for this DB; {} if not (section simply hides).
            "v2": v2_by.get(iso, {}),
            # Fase 5 cycle classifier (dalio_stage/deleveraging_type); {} if
            # dalio_cycle_v2 hasn't been populated yet.
            "cycle": cycle_by.get(iso, {}),
        }

    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cur_year": cur_year, "weo_horizon": weo_horizon,
        "has_cycle": bool(cycle_by),
        "phase_counts": reg["debt_cycle_phase"].value_counts().to_dict(),
        "quad_counts": reg["quadrant"].value_counts(dropna=True).to_dict(),
        "countries": countries,
        "chart_indicators": CHART_INDICATORS,
        "has_v2": bool(v2_by),
    }


# ----------------------------------------------------------------- HTML/JS
_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Macro Dashboard — Ray Dalio</title>
<style>
 :root{--blue:#1d4ed8;--ink:#1a1a2e;--mut:#64748b;--bg:#f8fafc;--card:#fff;--bd:#e2e8f0}
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:var(--ink);margin:0;background:var(--bg);font-size:14px}
 header{background:linear-gradient(120deg,#1e3a8a,#1d4ed8);color:#fff;padding:18px 24px}
 header h1{margin:0;font-size:20px} header p{margin:4px 0 0;opacity:.85;font-size:12px}
 nav{display:flex;gap:2px;background:#1e293b;padding:0 12px;flex-wrap:wrap}
 nav button{background:none;border:0;color:#cbd5e1;padding:12px 18px;cursor:pointer;font-size:14px;border-bottom:3px solid transparent}
 nav button.active{color:#fff;border-bottom-color:#60a5fa;font-weight:600}
 main{max-width:1080px;margin:0 auto;padding:20px}
 .tab{display:none} .tab.active{display:block}
 h2{font-size:16px;color:var(--blue);border-bottom:1px solid var(--bd);padding-bottom:6px;margin-top:26px}
 h3{color:var(--blue);margin-top:22px;font-size:15px}
 table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0}
 th{background:#eef2ff;text-align:left;padding:7px 8px;border-bottom:2px solid var(--bd)}
 td{padding:5px 8px;border-bottom:1px solid #f1f5f9} td.n{text-align:right;font-variant-numeric:tabular-nums}
 tr.clk:hover td{background:#f8fafc;cursor:pointer}
 .kpi{display:inline-block;background:#eff6ff;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0}
 .kpi b{display:block;font-size:20px;color:var(--blue)} .kpi span{font-size:11px;color:var(--mut)}
 .qgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
 .box{border:1px solid #cbd5e1;border-radius:10px;padding:12px;min-height:80px}
 .q1{background:#ecfdf5;border-color:#a7f3d0} .q2{background:#fefce8;border-color:#fde68a}
 .q3{background:#fef2f2;border-color:#fecaca} .q4{background:#eff6ff;border-color:#bfdbfe}
 .box b{font-size:13px} .box .cc{margin-top:6px;font-size:12px;color:#334155;line-height:1.5}
 select{font-size:15px;padding:8px 12px;border:1px solid var(--bd);border-radius:8px;min-width:300px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:18px;margin-top:14px}
 .badges{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
 .badge{padding:6px 12px;border-radius:20px;font-size:12px;font-weight:600;color:#fff}
 .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:12px 0}
 .metric{background:var(--bg);border-radius:8px;padding:8px 10px}
 .metric .l{font-size:11px;color:var(--mut)} .metric .v{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums}
 .pbar{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px;flex-wrap:wrap}
 .pbar .pl{width:90px;color:#334155} .pbar .pt{flex:1;min-width:80px;background:#eef2ff;border-radius:4px;height:16px;position:relative}
 .pbar .pf{position:absolute;top:0;height:16px;border-radius:4px}
 .pbar .pl2{width:190px;flex-shrink:0;color:#334155} .pbar .pv2{width:130px;text-align:right;flex-shrink:0}
 details{margin:2px 0 10px 198px} details summary{cursor:pointer;font-size:11px;color:var(--mut)}
 .comp-table{margin:4px 0 0;max-width:100%;overflow-x:auto;display:block}
 .comp-table td,.comp-table th{font-size:11px;padding:3px 6px;color:#1a1a2e;white-space:nowrap}
 .muted.v2note{margin:-2px 0 2px 198px;font-size:10.5px}
 @media (max-width:640px){
   .pbar .pl2{width:100%;font-weight:600} .pbar .pv2{width:100%;text-align:left}
   details,.muted.v2note{margin-left:4px}
 }
 .charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:14px}
 .chart{border:1px solid var(--bd);border-radius:10px;padding:10px;cursor:pointer;transition:box-shadow .15s}
 .chart:hover{box-shadow:0 2px 10px rgba(29,78,216,.18)}
 .chart h4{margin:0 0 4px;font-size:12.5px;color:#334155} .chart .hint{font-size:10px;color:var(--mut)}
 .meth{line-height:1.65} .meth code{background:#eef2ff;padding:1px 5px;border-radius:4px;font-size:12px}
 .note{background:#fffbeb;border-left:4px solid #f59e0b;padding:10px 12px;border-radius:4px;margin:10px 0;font-size:13px}
 .stale{background:#fef2f2;border-left:4px solid #dc2626;padding:8px 12px;border-radius:4px;margin:10px 0;font-size:12.5px;color:#991b1b}
 .ok{color:#16a34a} .warn{color:#dc2626} .muted{color:var(--mut);font-size:12px}
 /* modal */
 #modal{display:none;position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:50;align-items:center;justify-content:center}
 #modal.show{display:flex}
 #modalbox{background:#fff;border-radius:12px;padding:18px;width:min(760px,92vw)}
 #modalbox h3{margin:0 0 2px} #mclose{float:right;cursor:pointer;color:var(--mut);font-size:20px;border:0;background:none}
 #mtip{font-size:13px;color:#334155;height:18px;margin-top:6px}
</style></head><body>
<header><h1>Macro Analysis Framework — Ray Dalio Dashboard</h1><p id="sub"></p></header>
<nav>
 <button class="active" onclick="tab('ov',this)">Overview</button>
 <button onclick="tab('ct',this)">Country sheets</button>
 <button onclick="tab('me',this)">Methodology</button>
 <button onclick="tab('st',this)">Statistics</button>
</nav>
<main>
 <section id="ov" class="tab active"></section>
 <section id="ct" class="tab">
   <h2>Country sheet</h2>
   <select id="csel" onchange="showCountry(this.value)"></select>
   <div id="card" class="card"></div>
 </section>
 <section id="me" class="tab meth"></section>
 <section id="st" class="tab meth"></section>
</main>
<div id="modal" onclick="if(event.target.id=='modal')closeModal()">
 <div id="modalbox"><button id="mclose" onclick="closeModal()">&times;</button>
  <h3 id="mtitle"></h3><div id="mtip"></div><div id="mchart"></div>
  <p class="muted" id="mfoot"></p></div>
</div>
<script>
const DATA = __DATA__;
const PHASE = {
 EARLY_EXPANSION:["#16a34a","Healthy expansion: positive growth, no credit excess"],
 HIGH_DEBT_STABLE:["#d97706","High but stabilized debt (plateau): elevated, not deteriorating, not yet deleveraging"],
 LATE_LONG_CYCLE:["#b91c1c","Late LONG-TERM debt cycle: very high or deteriorating sovereign debt (Dalio's US thesis)"],
 LATE_LEVERAGING:["#ca8a04","Leveraging up: (private) credit accelerating toward a bubble"],
 BUBBLE:["#ea580c","Bubble: credit-to-GDP gap > +10pp, euphoria"],
 CONTRACTION:["#dc2626","Contraction: negative growth"],
 DEPRESSION:["#7f1d1d","Depression: negative growth + debt service at its peak"],
 BEAUTIFUL_DELEVERAGING:["#0891b2","Managed deleveraging: nominal growth > nominal rate"],
 INFLATIONARY_DELEVERAGING:["#c026d3","Inflationary deleveraging: nominal test passes, but inflation/FX (not real growth) is doing the work -- Dalio's ugly-but-mechanically-'beautiful' case"],
 UGLY_DELEVERAGING:["#9333ea","Painful deleveraging: nominal rate > nominal growth"],
 INDETERMINATE:["#94a3b8","Insufficient data"]
};
const QUAD = {
 Q1:["#16a34a","Growth up / Inflation down","Risk assets: DM equity, corporate credit"],
 Q2:["#ca8a04","Growth up / Inflation up","Commodities, EM equity, inflation-linked bonds"],
 Q3:["#dc2626","Growth down / Inflation up","Stagflation: gold, inflation hedges, defensives"],
 Q4:["#2563eb","Growth down / Inflation down","Nominal long-duration bonds, cash"]
};
const PILLAR_W={growth:20,debt_cycle:20,liquidity:15,external:15,sovereign:10,banking:10,governance:5,geopolitical:5,social:5};
const ENC={strong_exporter:["#065f46","oil: strong exporter"],exporter:["#16a34a","oil: exporter"],
 neutral:["#64748b","oil: neutral"],importer:["#ca8a04","oil: importer"],strong_importer:["#b91c1c","oil: strong importer"]};
const CAVEAT={
 SGP:"GROSS debt (172%) is very high but NET debt is ~zero: Singapore issues to invest (asset-backed). Read the 'late cycle' phase alongside the strong composite.",
 ARG:"Debt/GDP is falling partly via INFLATIONARY erosion, not only growth: 'beautiful deleveraging' is context-dependent (composite very weak).",
 HKG:"Late-leveraging refers to PRIVATE credit (among the world's highest), not public debt (low).",
 NOR:"Low gross debt plus a huge sovereign wealth fund: external strength understated by debt/GDP alone."
};
const ILABEL={}; DATA.chart_indicators.forEach(ci=>ILABEL[ci[0]]=ci[1]);

// Dalio v2 -- 5-engine architecture (additive; see
// docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md). Higher score = worse,
// 0-100, unlike the v1 composite (cross-country z, unbounded, higher=better).
const V2_ENGINE_ORDER=["sovereign_solvency","funding_liquidity","private_credit","external_constraint","political_execution"];
const V2_ENGINE_NAMES={sovereign_solvency:"Sovereign Solvency",political_execution:"Political Execution",
 private_credit:"Private Credit Cycle",external_constraint:"External Currency Constraint",funding_liquidity:"Funding Liquidity"};
// short input tooltips, full definitions live in the Statistics tab
const V2_INPUT_DESC={
 debt_gdp:"General government gross debt, % of GDP",
 net_debt_gdp:"Government debt net of financial assets, % of GDP",
 interest_gdp:"Interest paid on government debt, % of GDP",
 interest_revenue:"Interest / government revenue x100",
 primary_deficit_gdp:"-primary balance, % of GDP",
 r_minus_g:"Effective interest rate on debt minus nominal GDP growth",
 debt_trend_5y:"OLS slope of debt/GDP, [year-3,year+5], pp/year",
 government_effectiveness:"WGI percentile: quality of public services & policy execution",
 rule_of_law:"WGI percentile: contract enforcement, property rights, courts",
 control_corruption:"WGI percentile: public power exercised for private gain",
 political_stability:"WGI percentile: risk of destabilization/violent change",
 regulatory_quality:"WGI percentile: soundness of policy/regulation for private sector",
 credit_gap:"Private credit/GDP minus its long-run trend (BIS or detrend proxy)",
 private_dsr:"BIS private debt-service ratio, own-history percentile",
 real_credit_growth:"YoY % change in private debt/GDP",
 real_house_price_gap:"Real house-price deviation from trend (not wired yet)",
 npl_ratio:"Non-performing loans, % of total gross loans",
 current_account_deficit_gdp:"-current account balance, % of GDP",
 net_external_liability_gdp:"-net international investment position, % of GDP",
 short_term_debt_reserves:"Short-term external debt / FX reserves x100",
 debt_service_exports:"External debt service / exports of goods & services x100",
 fx_debt_share:"Share of debt denominated in foreign currency",
 inflation:"Headline CPI / WEO inflation",
 fx_overvaluation_pct:"% deviation of REER from its own trailing trend",
 reserves_months:"FX reserves in months of import cover",
 yield_change_12m_pp:"10y govt bond yield today minus 12 months ago, pp",
};
function v2Color(score){
 if(score===null||score===undefined||isNaN(score))return '#94a3b8';
 if(score<20)return '#16a34a'; if(score<40)return '#84cc16'; if(score<60)return '#d97706';
 if(score<80)return '#ea580c'; return '#b91c1c';
}
// Mean of the AVAILABLE engines only (insufficient-coverage engines carry no
// score): a navigation aid for sorting, not a fifth signal -- the methodology
// keeps the 5 engines separate, so the n/5 count is always shown next to it.
function v2AvgRisk(c){
 const vals=V2_ENGINE_ORDER.map(e=>c.v2&&c.v2[e]?c.v2[e].score:null).filter(v=>v!==null&&v!==undefined);
 return vals.length?{avg:vals.reduce((a,b)=>a+b,0)/vals.length,n:vals.length}:null;
}

function tab(id,btn){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
 document.getElementById(id).classList.add('active');
 document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));btn.classList.add('active');}
function fmt(v,d=1){return (v===null||v===undefined||isNaN(v))?'—':Number(v).toFixed(d);}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
// Fase 5 cycle classifier: colors/names for dalio_stage + deleveraging_type
const STAGE_NAMES={early_or_mid_cycle:"Early/mid cycle",late_long_debt_cycle:"Late long debt cycle",
 private_bubble:"Private credit bubble",late_leveraging:"Late leveraging",contraction:"Contraction",crisis:"Crisis"};
const STAGE_COLORS={early_or_mid_cycle:'#16a34a',late_long_debt_cycle:'#ca8a04',private_bubble:'#dc2626',
 late_leveraging:'#ea580c',contraction:'#0891b2',crisis:'#991b1b'};
const DELEV_NAMES={none:"No deleveraging",beautiful:"Beautiful",inflationary:"Inflationary",
 repressive:"Repressive",restructuring:"Restructuring",ugly:"Ugly"};
const DELEV_COLORS={none:'#94a3b8',beautiful:'#16a34a',inflationary:'#dc2626',repressive:'#b45309',
 restructuring:'#7c3aed',ugly:'#991b1b'};

// ---- SVG line chart (vanilla). interactive=true adds hover crosshair+tooltip ----
function lineChart(series,W,H,interactive,id){
 const pl=40,pr=12,pt=12,pb=24;
 if(!series||series.length<2) return '<svg viewBox="0 0 '+W+' '+H+'"><text x="12" y="'+(H/2)+'" fill="#94a3b8" font-size="12">no data</text></svg>';
 const xs=series.map(p=>p[0]),ys=series.map(p=>p[1]);
 const x0=Math.min(...xs),x1=Math.max(...xs);let y0=Math.min(...ys),y1=Math.max(...ys);
 if(y0===y1){y0-=1;y1+=1;} const pad=(y1-y0)*0.1;y0-=pad;y1+=pad;
 const sx=v=>pl+(v-x0)/(x1-x0||1)*(W-pl-pr), sy=v=>pt+(1-(v-y0)/(y1-y0||1))*(H-pt-pb);
 let d=series.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
 let zl='';if(y0<0&&y1>0){const zy=sy(0).toFixed(1);zl='<line x1="'+pl+'" y1="'+zy+'" x2="'+(W-pr)+'" y2="'+zy+'" stroke="#cbd5e1" stroke-dasharray="3 3"/>';}
 const yl='<text x="3" y="'+(pt+6)+'" font-size="9" fill="#94a3b8">'+y1.toFixed(0)+'</text>'+
          '<text x="3" y="'+(H-pb)+'" font-size="9" fill="#94a3b8">'+y0.toFixed(0)+'</text>';
 const xl='<text x="'+pl+'" y="'+(H-7)+'" font-size="9" fill="#94a3b8">'+x0+'</text>'+
          '<text x="'+(W-pr-26)+'" y="'+(H-7)+'" font-size="9" fill="#94a3b8">'+x1+'</text>';
 const last=series[series.length-1];
 const dot='<circle cx="'+sx(last[0]).toFixed(1)+'" cy="'+sy(last[1]).toFixed(1)+'" r="3" fill="#1d4ed8"/>';
 let pts='';
 if(interactive){ // data for the hover
   pts=series.map(p=>'<circle class="hp" cx="'+sx(p[0]).toFixed(1)+'" cy="'+sy(p[1]).toFixed(1)+'" r="9" fill="transparent" data-y="'+p[0]+'" data-v="'+p[1]+'"/>').join('');
 }
 const cl=interactive?' onmousemove="hoverChart(event)" onmouseleave="document.getElementById(\'hx\').style.display=\'none\'"':'';
 const cross=interactive?'<line id="hx" style="display:none" stroke="#1d4ed8" stroke-width="1" y1="'+pt+'" y2="'+(H-pb)+'"/>':'';
 return '<svg viewBox="0 0 '+W+' '+H+'" width="100%"'+cl+'>'+zl+'<path d="'+d+'" fill="none" stroke="#1d4ed8" stroke-width="1.8"/>'+cross+dot+pts+yl+xl+'</svg>';
}

function openChart(iso,ind){
 const c=DATA.countries[iso]; if(!c)return; const s=c.series[ind];
 document.getElementById('mtitle').textContent=c.name+' — '+(ILABEL[ind]||ind);
 document.getElementById('mtip').textContent='Hover the chart to read values';
 document.getElementById('mchart').innerHTML=lineChart(s,720,320,true,'m');
 const yrs=s&&s.length?(' · '+s[0][0]+'–'+s[s.length-1][0]+' · last '+fmt(s[s.length-1][1])):'';
 document.getElementById('mfoot').textContent='Annual values (WEO incl. projections beyond '+DATA.cur_year+')'+yrs;
 document.getElementById('modal').classList.add('show');
}
function closeModal(){document.getElementById('modal').classList.remove('show');}
function hoverChart(e){
 const svg=e.currentTarget, hps=[...svg.querySelectorAll('.hp')]; if(!hps.length)return;
 const pt=svg.createSVGPoint(); pt.x=e.clientX; pt.y=e.clientY;
 const loc=pt.matrixTransform(svg.getScreenCTM().inverse());
 let best=null,bd=1e9; hps.forEach(h=>{const dx=Math.abs(+h.getAttribute('cx')-loc.x); if(dx<bd){bd=dx;best=h;}});
 if(!best)return; const hx=document.getElementById('hx');
 hx.setAttribute('x1',best.getAttribute('cx'));hx.setAttribute('x2',best.getAttribute('cx'));hx.style.display='';
 document.getElementById('mtip').textContent=best.dataset.y+': '+fmt(best.dataset.v,2);
}

function showCountry(iso){
 const c=DATA.countries[iso]; if(!c)return;
 const ph=PHASE[c.phase]||['#94a3b8',c.phase], q=QUAD[c.quadrant]||['#94a3b8','',''];
 let h='<h3 style="margin:0">'+c.name+' <span class="muted">('+iso+')</span></h3>';
 h+='<div class="badges">';
 h+='<span class="badge" style="background:'+ph[0]+'">Phase: '+(c.phase||'—')+'</span>';
 h+='<span class="badge" style="background:'+q[0]+'">Regime: '+(c.quadrant||'—')+'</span>';
 if(c.delev&&c.delev!=='NA')h+='<span class="badge" style="background:#475569">Deleveraging: '+c.delev+'</span>';
 h+='</div>';
 h+='<p class="muted">'+ph[1]+'</p>';
 if(c.quadrant)h+='<p class="muted">'+q[1]+' → '+q[2]+'</p>';
 if(CAVEAT[iso])h+='<div class="note">&#9888; '+CAVEAT[iso]+'</div>';
 // Fase 5 cycle classifier -- sits above the (buggy, nominal-r-g-driven)
 // v1 "Deleveraging:" badge above, which is exactly what this supersedes:
 // see docs/DALIO_V2_DEEP_AUDIT_2026-07.md and cycle_classifier.py's
 // docstring for the Argentina/Turkey case this exists to catch.
 const cyc=c.cycle||{};
 if(cyc.dalio_stage||cyc.deleveraging_type){
   h+='<div class="badges">';
   if(cyc.dalio_stage)h+='<span class="badge" style="background:'+(STAGE_COLORS[cyc.dalio_stage]||'#64748b')+
     '">Cycle stage (v2): '+(STAGE_NAMES[cyc.dalio_stage]||cyc.dalio_stage)+'</span>';
   if(cyc.deleveraging_type)h+='<span class="badge" style="background:'+(DELEV_COLORS[cyc.deleveraging_type]||'#64748b')+
     '">Deleveraging type (v2): '+(DELEV_NAMES[cyc.deleveraging_type]||cyc.deleveraging_type)+'</span>';
   h+='</div>';
   if(cyc.caveats&&cyc.caveats.length)h+='<div class="note">&#9888; '+cyc.caveats.map(escapeHtml).join(' ')+'</div>';
   if(cyc.top_risk_drivers&&cyc.top_risk_drivers.length){
     h+='<p class="muted" style="margin:4px 0 10px">Top risk drivers (v2): '+
       cyc.top_risk_drivers.map(d=>escapeHtml(d.component)+' ('+(V2_ENGINE_NAMES[d.engine]||d.engine)+', '+fmt(d.score,0)+'/100)').join(', ')+
       '</p>';
   }
 } else if(DATA.has_cycle){
   h+='<p class="muted">No Fase 5 cycle classification for this country yet.</p>';
 }
 if(c.stale&&c.stale.length){h+='<div class="stale">&#9888; <b>Stale data</b> — this country lags peers '+
   'on: '+c.stale.map(s=>s.label+' (latest '+s.year+' vs '+s.frontier+' elsewhere)').join('; ')+'</div>';}
 // classification
 const cl=c.cls||{};
 if(Object.keys(cl).length){
   const tags=[];
   if(cl.development)tags.push(['#1e293b',cl.development]);
   if(cl.region_group)tags.push(['#475569',cl.region_group.replace(/\(.*\)/,'').trim()]);
   if(cl.income)tags.push(['#64748b',cl.income]);
   if(cl.fx_regime)tags.push(['#0f766e','FX: '+cl.fx_regime]);
   if(cl.imf_program)tags.push(['#b45309','IMF program']);
   if(cl.energy_position&&ENC[cl.energy_position])tags.push([ENC[cl.energy_position][0],ENC[cl.energy_position][1]+(cl.net_fuel_gdp!=null?' ('+fmt(cl.net_fuel_gdp,0)+'% GDP)':'')]);
   if(cl.resource_dependence==='resource_driven')tags.push(['#92400e','resource-driven']);
   if(cl.tourism_dependence&&['significant','dominant'].includes(cl.tourism_dependence))tags.push(['#7c3aed','tourism: '+cl.tourism_dependence]);
   if(cl.remittance_dependence==='high')tags.push(['#be185d','high remittances']);
   h+='<div class="badges" style="margin-top:4px">';
   tags.forEach(t=>{h+='<span class="badge" style="background:'+t[0]+';font-weight:500">'+t[1]+'</span>';});
   h+='</div>';
 }
 const M=[['Composite',c.composite,2],['Debt/GDP trend (pp/yr)',c.debt_trend,2],
   ['Nominal growth %',c.nom_growth,1],['Nominal rate %',c.nom_rate,1],
   ['Credit gap (pp)',c.credit_gap,1],['DSR %',c.dsr,1]];
 h+='<div class="metrics">';
 M.forEach(m=>{h+='<div class="metric"><div class="l">'+m[0]+'</div><div class="v">'+fmt(m[1],m[2])+'</div></div>';});
 h+='</div>';
 h+='<div style="margin:12px 0"><b style="font-size:12px;color:#334155">Pillar scores (cross-country z × direction)</b>';
 Object.keys(PILLAR_W).forEach(p=>{const z=c.pillars[p];
   if(z===undefined||z===null)return;
   const w=Math.min(50,Math.abs(z)*25),col=z>=0?'#16a34a':'#dc2626',left=z>=0?50:50-w;
   h+='<div class="pbar"><span class="pl">'+p+'</span><span class="pt">'+
      '<span class="pf" style="left:'+left+'%;width:'+w+'%;background:'+col+'"></span>'+
      '<span style="position:absolute;left:50%;top:0;height:16px;border-left:1px solid #94a3b8"></span></span>'+
      '<span style="width:42px;text-align:right">'+fmt(z,2)+'</span></div>';});
 h+='</div>';
 const v2=c.v2||{};
 if(Object.keys(v2).length){
   h+='<div style="margin:12px 0"><b style="font-size:12px;color:#334155">Dalio v2 &mdash; country risk engines</b> '+
      '<span class="muted">(0-100, higher = worse; separate from the v1 composite above)</span>';
   V2_ENGINE_ORDER.forEach(e=>{
     const r=v2[e]; if(!r)return;
     const s=r.score, col=v2Color(s), pct=(s===null||s===undefined||isNaN(s))?0:Math.max(0,Math.min(100,s));
     const txt=(s===null||s===undefined||isNaN(s))?'n/a':fmt(s,1)+'/100';
     h+='<div class="pbar"><span class="pl pl2">'+(V2_ENGINE_NAMES[e]||e)+'</span><span class="pt">'+
        '<span class="pf" style="left:0;width:'+pct+'%;background:'+col+'"></span></span>'+
        '<span class="pv2">'+txt+(r.label?' &middot; '+r.label:'')+'</span></div>';
     h+='<div class="muted v2note">coverage: '+r.coverage_tier+
        ' &middot; confidence: '+r.confidence+' &middot; '+r.n_components+'/'+r.n_expected+' inputs</div>';
     const comps=r.components||{};
     if(Object.keys(comps).length){
       h+='<details><summary>components</summary><table class="comp-table"><thead><tr>'+
          '<th>input</th><th class=n>raw value</th><th class=n>risk score</th><th class=n>weight</th></tr></thead><tbody>';
       Object.entries(comps).forEach(([name,cc])=>{
         const raw=(cc.raw_value===null||cc.raw_value===undefined)?'n/a'
           :(typeof cc.raw_value==='number'?String(+cc.raw_value.toFixed(2)):cc.raw_value);
         const sc=(cc.score===null||cc.score===undefined)?'n/a':fmt(cc.score,1);
         const wt=(typeof cc.weight==='number')?String(+cc.weight.toFixed(2)):(cc.weight??0);
         const age=cc.obs_date?' <span class="muted">('+cc.obs_date+')</span>':'';
         const desc=(V2_INPUT_DESC[name]||'').replace(/"/g,'&quot;');
         h+='<tr><td'+(desc?' title="'+desc+'"':'')+'>'+name+age+'</td><td class=n>'+raw+
            '</td><td class=n>'+sc+'</td><td class=n>'+wt+'</td></tr>';
       });
       h+='</tbody></table></details>';
     }
   });
   h+='</div>';
 } else if(DATA.has_v2){
   h+='<p class="muted">No Dalio v2 engine scores for this country yet.</p>';
 } else {
   h+='<div class="note">Dalio v2 (5-engine country risk architecture) has not been run against this '+
      'database yet &mdash; run <code>python run_dalio_v2.py</code> and regenerate this report to see it here.</div>';
 }
 h+='<div class="charts">';
 DATA.chart_indicators.forEach(ci=>{const s=c.series[ci[0]];
   h+='<div class="chart" onclick="openChart(\''+iso+'\',\''+ci[0]+'\')"><h4>'+ci[1]+
      ' <span class="hint">▸ click to enlarge</span></h4>'+lineChart(s,300,140,false)+'</div>';});
 h+='</div>';
 document.getElementById('card').innerHTML=h;
}

function buildOverview(){
 document.getElementById('sub').textContent='Generated '+DATA.now+' · '+Object.keys(DATA.countries).length+
   ' countries · WEO forecast horizon '+DATA.weo_horizon;
 const cs=Object.entries(DATA.countries);
 const quadCell=q=>cs.filter(([k,v])=>v.quadrant===q).map(([k])=>k).sort().join(', ')||'—';
 const pc=DATA.phase_counts,qc=DATA.quad_counts;
 let h='<h2>State of the world (Dalio synthesis)</h2><div>';
 h+='<div class="kpi"><b>'+cs.length+'</b><span>countries</span></div>';
 h+='<div class="kpi"><b>'+(pc.BEAUTIFUL_DELEVERAGING||0)+'</b><span>beautiful deleveraging</span></div>';
 h+='<div class="kpi"><b>'+(pc.INFLATIONARY_DELEVERAGING||0)+'</b><span>inflationary deleveraging</span></div>';
 h+='<div class="kpi"><b>'+(pc.UGLY_DELEVERAGING||0)+'</b><span>ugly deleveraging</span></div>';
 h+='<div class="kpi"><b>'+((pc.LATE_LONG_CYCLE||0)+(pc.HIGH_DEBT_STABLE||0))+'</b><span>high sovereign debt</span></div>';
 h+='<div class="kpi"><b>'+(qc.Q3||0)+'</b><span>stagflation (Q3)</span></div></div>';
 if(DATA.has_v2){
   // plain dark-gray text, no colored badge/pill (that previously relied on
   // a CSS class that was never defined -> invisible white-on-nothing text)
   h+='<h2>Dalio v2 &mdash; engine comparison <span class="muted">(0-100, higher = worse; sorted by the mean of the AVAILABLE engines '+
      '&mdash; a navigation aid, the 5 engines are never combined into one signal; click a row for the country sheet)</span></h2>';
   h+='<table><tr><th>Country</th>'+V2_ENGINE_ORDER.map(e=>'<th>'+V2_ENGINE_NAMES[e]+'</th>').join('')+'</tr>';
   cs.slice().sort((a,b)=>((v2AvgRisk(b[1])?.avg??-1)-(v2AvgRisk(a[1])?.avg??-1))).forEach(([iso,c])=>{
     h+='<tr class="clk" onclick="gotoCountry(\''+iso+'\')"><td><b>'+iso+'</b> <span class="muted">'+c.name+'</span></td>';
     V2_ENGINE_ORDER.forEach(e=>{
       const r=c.v2&&c.v2[e];
       if(!r){h+='<td class="muted">&mdash;</td>';return;}
       const s=r.score, txt=(s===null||s===undefined||isNaN(s))?'n/a':fmt(s,1);
       const tierSuffix=(r.coverage_tier&&r.coverage_tier!=='full')?' ['+r.coverage_tier+']':'';
       h+='<td>'+txt+(r.label?' &middot; '+r.label:'')+tierSuffix+'</td>';
     });
     h+='</tr>';
   });
   h+='</table>';
 }
 h+='<h2>Growth / Inflation matrix (Bridgewater four-box)</h2>';
 h+='<div class="qgrid">';
 [['q1','Q1'],['q2','Q2'],['q3','Q3'],['q4','Q4']].forEach(([cl,q])=>{
   const Q=QUAD[q];h+='<div class="box '+cl+'"><b>'+q+' · '+Q[1]+'</b><div class="muted">'+Q[2]+'</div><div class="cc">'+quadCell(q)+'</div></div>';});
 h+='</div>';
 h+='<h2>Debt-cycle phase distribution</h2><table><tr><th>Phase</th><th>N</th><th>Meaning</th></tr>';
 Object.entries(pc).sort((a,b)=>b[1]-a[1]).forEach(([p,n])=>{
   h+='<tr><td><b style="color:'+(PHASE[p]?PHASE[p][0]:'#000')+'">'+p+'</b></td><td class=n>'+n+'</td><td class="muted">'+(PHASE[p]?PHASE[p][1]:'')+'</td></tr>';});
 h+='</table>';
 h+='<h2>Cross-country snapshot <span class="muted">(click a row for the country sheet)</span></h2>';
 const v2col=DATA.has_v2?'<th title="mean of the available v2 engines (count shown); not a combined signal">v2 risk (n/5)</th>':'';
 h+='<table><tr><th>Country</th><th>Composite</th><th>Phase</th><th>Regime</th><th>Delev.</th><th>Debt/GDP trend</th><th>nom.g</th><th>nom.r</th><th>cg</th><th>dsr</th>'+v2col+'</tr>';
 cs.sort((a,b)=>((b[1].composite??-99)-(a[1].composite??-99)));
 cs.forEach(([iso,c])=>{const q=QUAD[c.quadrant];
   const dt=c.debt_trend, dtc=(dt===null||dt===undefined)?'#000':(dt>1.5?'#b91c1c':dt>0.7?'#ca8a04':dt<0?'#16a34a':'#334155');
   const star=(c.stale&&c.stale.length)?' <span title="has stale data" style="color:#dc2626">&#9888;</span>':'';
   let v2cell='';
   if(DATA.has_v2){const a=v2AvgRisk(c);
     v2cell='<td class=n style="color:'+v2Color(a?a.avg:null)+';font-weight:600">'+
       (a===null?'—':fmt(a.avg,0)+' <span class="muted">('+a.n+'/5)</span>')+'</td>';}
   h+='<tr class="clk" onclick="gotoCountry(\''+iso+'\')"><td><b>'+iso+'</b> <span class="muted">'+c.name+'</span>'+star+'</td>'+
     '<td class=n>'+fmt(c.composite,2)+'</td><td style="color:'+(PHASE[c.phase]?PHASE[c.phase][0]:'#000')+'">'+(c.phase||'—')+'</td>'+
     '<td style="color:'+(q?q[0]:'#000')+';font-weight:600">'+(c.quadrant||'—')+'</td><td>'+(c.delev||'—')+'</td>'+
     '<td class=n style="color:'+dtc+';font-weight:600">'+fmt(dt,2)+'</td>'+
     '<td class=n>'+fmt(c.nom_growth)+'</td><td class=n>'+fmt(c.nom_rate)+'</td>'+
     '<td class=n>'+fmt(c.credit_gap)+'</td><td class=n>'+fmt(c.dsr)+'</td>'+v2cell+'</tr>';});
 h+='</table>';
 if(!DATA.has_v2)h+='<p class="muted">Dalio v2 (5-engine country risk) not yet run against this database &mdash; '+
   'run <code>python run_dalio_v2.py</code> to add the v2 risk column and per-country engine detail.</p>';
 document.getElementById('ov').innerHTML=h;
}
function gotoCountry(iso){document.querySelectorAll('nav button')[1].click();
 document.getElementById('csel').value=iso;showCountry(iso);window.scrollTo(0,0);}
function buildSelect(){
 const sel=document.getElementById('csel');
 Object.entries(DATA.countries).sort((a,b)=>a[1].name.localeCompare(b[1].name)).forEach(([iso,c])=>{
   const o=document.createElement('option');o.value=iso;o.textContent=c.name+' ('+iso+')';sel.appendChild(o);});
 showCountry(sel.value);
}
document.getElementById('me').innerHTML=__METH__;
document.getElementById('st').innerHTML=__STATS__;
buildOverview();buildSelect();
</script></body></html>"""

# methodology (English)
_METH = r"""
<h2>The Ray Dalio method in detail</h2>
<p>This system does not merely collect indicators: it places every country in a <b>debt-cycle phase</b>
and a <b>growth/inflation regime</b> — which is what Dalio actually does. The number (composite) says
<i>how</i> strong/weak; the labels say <i>what kind of world</i> the country is in.</p>

<h3>1. The three drivers of an economy</h3>
<table><tr><th>Driver</th><th>Horizon</th><th>What it measures</th></tr>
<tr><td>Productivity growth</td><td>Decades</td><td>Long-run baseline (real GDP per capita)</td></tr>
<tr><td>SHORT-term debt cycle</td><td>5–8 years</td><td>Credit + monetary policy (output gap, rates)</td></tr>
<tr><td>LONG-term debt cycle</td><td>50–100 years</td><td>Debt accumulated across many short cycles (debt/GDP, DSR)</td></tr></table>
<div class="note"><b>Dalio's #1 signal:</b> the <code>debt-to-income gap</code> — when debt keeps growing
faster than income, the country approaches the limit of the long cycle. Here it is captured by the
multi-year <b>debt/GDP trajectory</b> (slope, including WEO projections).</div>

<h3>2. Debt-cycle phases</h3>
<p>Each country is classified by a threshold tree (parameters in <code>settings.yaml → dalio</code>):</p>
<table><tr><th>Phase</th><th>Condition</th><th>Reading</th></tr>
<tr><td><b>EARLY_EXPANSION</b></td><td>growth &gt; 0, no credit excess, debt &lt; 100% GDP</td><td>healthy expansion</td></tr>
<tr><td><b>LATE_LEVERAGING</b></td><td>credit gap &gt; +5pp or debt rising &gt; +1.5pp/yr</td><td>(private) credit accelerating</td></tr>
<tr><td><b>BUBBLE</b></td><td>credit-to-GDP gap &gt; +10pp</td><td>euphoria, excess leverage</td></tr>
<tr><td><b>HIGH_DEBT_STABLE</b></td><td>debt &gt; 100% GDP but stable (not rising, contained deficit)</td><td>high but plateaued (e.g. UK)</td></tr>
<tr><td><b>LATE_LONG_CYCLE</b></td><td>debt &gt; 130%, or &gt; 100% and deteriorating (rising / large deficit)</td><td>late long-term debt cycle (Dalio's US thesis)</td></tr>
<tr><td><b>CONTRACTION</b></td><td>growth &lt; 0</td><td>recession</td></tr>
<tr><td><b>DEPRESSION</b></td><td>growth &lt; 0 + DSR at its historical peak</td><td>debt crisis</td></tr>
<tr><td><b>BEAUTIFUL_DELEVERAGING</b></td><td>debt/GDP falling, nominal growth &gt; nominal rate</td><td>managed deleveraging (the ideal)</td></tr>
<tr><td><b>INFLATIONARY_DELEVERAGING</b></td><td>debt/GDP falling, nominal growth &gt; nominal rate, but distorted (fx_debt_share, then inflation &gt;15%/yr, then IMF program)</td><td>Dalio's own third category: the nominal test passes only because inflation/FX is doing the work, not real strength</td></tr>
<tr><td><b>UGLY_DELEVERAGING</b></td><td>debt/GDP falling, nominal rate &gt; nominal growth</td><td>painful/deflationary deleveraging</td></tr></table>
<div class="note"><b>Beautiful vs ugly vs inflationary:</b> Dalio's operating rule is <code>nominal growth &gt; nominal rate</code>
(both nominal — this is also the IMF/EU-AMECO "implicit interest rate vs nominal GDP growth" convention).
If nominal growth exceeds the cost of debt, debt/GDP can fall with positive growth (beautiful); the
opposite is painful. But Dalio's own framework names a third historical case: nominal growth can clear
the bar only because inflation/currency depreciation is doing the work — "ugly inflationary deleveraging"
(Weimar/Latin American-style), distinct from a genuinely beautiful one. INFLATIONARY_DELEVERAGING flags
this using the best available signal (fx_debt_share &gt; 80%, else inflation &gt; 15%/yr, else an active
IMF program).</div>

<h3>3. The Growth / Inflation matrix (Bridgewater four-box)</h3>
<p>Two axes, each measured with the most appropriate statistic:</p>
<ul>
<li><b>Growth = output gap</b>: current real growth vs the country's own <b>potential</b> (WEO
medium-term average). A fast-growing economy <i>above</i> potential is "growth up" (Vietnam); a
slow-growing one <i>below</i> trend is "down" (Italy) — regardless of absolute level.</li>
<li><b>Inflation = direction</b>: current inflation vs the <b>previous 3-year average</b>
(reflation vs disinflation), e.g. Turkey strongly disinflating = "inflation down".</li>
</ul>
<table><tr><th>Quadrant</th><th>Environment</th><th>Favoured assets</th></tr>
<tr><td><b>Q1</b></td><td>Growth ↑ Inflation ↓</td><td>DM equity, corporate credit, risk assets</td></tr>
<tr><td><b>Q2</b></td><td>Growth ↑ Inflation ↑</td><td>Commodities, EM equity, inflation-linked</td></tr>
<tr><td><b>Q3</b></td><td>Growth ↓ Inflation ↑</td><td>Gold, inflation hedges, defensives</td></tr>
<tr><td><b>Q4</b></td><td>Growth ↓ Inflation ↓</td><td>Nominal long-duration bonds, cash</td></tr></table>

<h3>4. How to read a country sheet</h3>
<p>Each sheet mirrors a Bridgewater country template: which <b>phase</b> of the debt cycle, which
growth/inflation <b>quadrant</b>, the <b>deleveraging quality</b>, the <b>pillar scores</b>
(cross-country z × direction) and the weighted <b>composite</b>. Historical charts show debt, growth,
inflation, rates, credit gap and external/fiscal balances — click any chart to enlarge with tooltips.</p>

<h3>5. Caveats & known limits</h3>
<table><tr><th>Limit</th><th>Effect</th></tr>
<tr><td>Gross vs net debt</td><td>We use gross public debt (WEO). Asset-backed countries (Singapore net ~0; Norway sovereign fund) look more "late cycle" than they are — mitigated by the composite.</td></tr>
<tr><td>Deleveraging via inflation</td><td>Debt/GDP falling via inflation erosion (Argentina) is tagged deleveraging even when the adjustment is painful; the composite stays weak.</td></tr>
<tr><td>Private vs public credit cycle</td><td>BUBBLE/LATE_LEVERAGING use private credit (BIS); LATE_LONG_CYCLE uses sovereign debt. A country can be in private late-leveraging with low public debt (Hong Kong).</td></tr>
<tr><td>Energy classification</td><td>Trade-based; an "exporter" with no resource rents (re-export hub like Cyprus) is downgraded to neutral via a rents sanity check.</td></tr>
<tr><td>nom_rate = policy rate</td><td>Proxy for the cost of debt (the spec allows policy or 10Y); the euro area uses the ECB rate.</td></tr></table>
<p class="muted">All thresholds live in <code>lazyray/config/settings.yaml</code> under <code>dalio</code>.
Source: Dalio_Macro_Framework_Spec_v3.</p>

<h2>6. Dalio v2 &mdash; 5-engine country risk architecture</h2>
<p>The sections above (phase, quadrant, composite) are <b>v1</b>: one threshold tree plus one
weighted composite z-score. v1's own limits — a single number that can compensate a real risk
(strong governance masking weak debt dynamics) with an unrelated strength, and thresholds applied
uniformly regardless of how much underlying data actually supports them — motivated a second,
<b>additive</b> layer: five independent risk engines, each scored 0-100 (higher = worse) on its
own dimension, never combined into a single number. v1 is untouched; v2 lives in its own
<code>engine_scores</code> table. Full design: <code>docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md</code>.</p>

<h3>6.1 The five engines</h3>
<table><tr><th>Engine</th><th>Question it answers</th><th>Core inputs</th></tr>
<tr><td><b>Sovereign Solvency</b></td><td>Can the state service its debt without default, repression, or destabilizing austerity?</td><td>Debt/GDP (gross &amp; net, income-group-specific thresholds), interest/revenue, interest/GDP, primary deficit, r&minus;g, 5y debt trend</td></tr>
<tr><td><b>Political Execution</b></td><td>Can the country make the adjustment its debt situation requires, without a political crisis?</td><td>5 World Bank Worldwide Governance Indicators (government effectiveness, rule of law, corruption control, political stability, regulatory quality), cross-country percentile</td></tr>
<tr><td><b>Private Credit Cycle</b></td><td>Is private-sector credit overheating, independent of public debt?</td><td>BIS credit-to-GDP gap &amp; debt service ratio where available (~43/~32 of 64 countries); a linear-detrend proxy on total private debt elsewhere, always flagged as a proxy</td></tr>
<tr><td><b>External Currency Constraint</b></td><td>Could a fiscal problem turn into a currency/BoP crisis?</td><td>Current account, NIIP, FX-denominated debt share (IMF IIPCC, ~19 countries full quality), short-term debt/reserves, REER deviation, reserve adequacy; reserve-currency issuers get a discounted score with an explicit caveat, not a silent zero</td></tr>
<tr><td><b>Funding Liquidity</b></td><td>Can the country place the debt it needs to, without a rate/currency shock?</td><td><b>Deliberately reduced scope</b>: real Gross Financing Needs and auction data are free only for ~15-25 OECD/major economies and are NOT wired here. This engine is the coarse proxy tier only (short-term debt/reserves + 12-month bond yield change) &mdash; its coverage tier is always "proxy", never "full".</td></tr></table>

<h3>6.2 How a score is built</h3>
<p>Each raw input is mapped to 0-100 by linear interpolation between three named thresholds
(0 at/below "watch", 50 at "stress", 100 at/above "critical"), then the available components are
averaged with configured weights (<code>settings.yaml &rarr; dalio_v2</code>). Category label
(e.g. strong/stable/watch/stressed/critical) uses a dead-band around each boundary so a score
oscillating near a cut point does not flip label on every run (hysteresis); a move spanning more
than one bucket still applies immediately.</p>

<h3>6.3 Coverage tiers &mdash; read this before trusting a number</h3>
<table><tr><th>Tier</th><th>Meaning</th></tr>
<tr><td><b>full</b></td><td>&ge;80% of the engine's expected inputs are present for this country.</td></tr>
<tr><td><b>proxy</b></td><td>40-80% present, or the engine structurally only ever produces a coarser substitute for the real input (Private Credit's non-BIS countries; all of Funding Liquidity).</td></tr>
<tr><td><b>insufficient</b></td><td>&lt;40% of inputs present. The score is <code>null</code> (shown as "n/a") &mdash; a single lucky/safe available component is never allowed to read as a confident "0.0/strong" on its own.</td></tr></table>

<h3>6.4 Known limits (do not skip this)</h3>
<table><tr><th>Limit</th><th>Detail</th></tr>
<tr><td>Not vintage-aware</td><td>Reads the current known value for every indicator, not the point-in-time value as of the report date &mdash; the same look-ahead risk v1 has for its forecast-dependent slope. A vintage-aware backtest is planned (<code>DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md</code>) but not built yet.</td></tr>
<tr><td>Many thresholds are ASSUMED, not calibrated</td><td>Only a subset come from the source methodology proposal (with citations in the code); the rest are informed guesses (e.g. NIIP&nbsp;&minus;35%/GDP follows the EU Macroeconomic Imbalance Procedure alert threshold) pending real calibration.</td></tr>
<tr><td>Weights are equal/arbitrary</td><td>No sensitivity analysis has been run yet on any engine's component weights.</td></tr>
<tr><td>No historical backtest</td><td>These engines have not been validated against known crisis episodes (Greece 2010, Argentina, UK gilt 2022, etc.). Treat the scores as a structured, auditable starting point &mdash; not a validated predictive signal.</td></tr>
<tr><td>Funding Liquidity is a placeholder for most countries</td><td>Its "proxy" tier (short-term debt/reserves + yield change) is a real but coarse substitute for actual financing-need/auction data, which is only free for a minority of economies.</td></tr></table>
<p class="muted">Every engine score in the country sheet has an expandable "components" panel
showing the exact raw value, sub-score and weight behind it &mdash; use it before trusting any
single number.</p>

<h2>7. Dalio v2 &mdash; cycle classifier</h2>
<p>Sits on top of the 5 engines and produces two categorical outputs per country: <b>Cycle stage</b>
and <b>Deleveraging type</b>. It exists because the v1 "Deleveraging:" badge above (BEAUTIFUL/UGLY,
driven by nominal growth vs a nominal interest rate) can misread a country whose debt/GDP is falling
purely through <i>inflationary erosion</i> &mdash; not fiscal health &mdash; as "beautiful". Argentina and
Turkey are the textbook cases: high inflation mechanically produces a "favorable"-looking nominal
r&minus;g even during an outright debt crisis. See
<code>docs/DALIO_V2_DEEP_AUDIT_2026-07.md</code> and
<code>lazyray/dalio_v2/cycle_classifier.py</code>'s module docstring for the full story.</p>

<h3>7.1 Deleveraging type</h3>
<table><tr><th>Type</th><th>Meaning</th></tr>
<tr><td><b>beautiful</b></td><td>Debt/GDP falling with positive real growth, controlled inflation, a stable currency and easy/normal funding conditions &mdash; the healthy form.</td></tr>
<tr><td><b>inflationary</b></td><td>Debt/GDP falling but inflation or currency depreciation is high &mdash; the debt is being eroded away, not paid down. This is the category that keeps Argentina/Turkey-style cases out of "beautiful".</td></tr>
<tr><td><b>repressive</b></td><td>A weak proxy (very negative real interest rate combined with a low FX-debt share, i.e. debt mostly absorbable domestically) &mdash; no central-bank-holdings-of-debt data source exists, so this rarely fires. Documented, not hidden.</td></tr>
<tr><td><b>ugly</b></td><td>Debt/GDP falling but none of the above hold (recession, funding stress, etc.) &mdash; also where a hypothetical debt restructuring lands, since no restructuring-event data source exists either.</td></tr>
<tr><td><b>none</b></td><td>Debt/GDP is not falling at all.</td></tr>
<tr><td><b>n/a (unclassifiable)</b></td><td>One of the engines this output depends on (Sovereign Solvency, External Constraint, Funding Liquidity) has insufficient coverage for this country &mdash; the gate is all-or-nothing across those three, never a silent per-branch skip that could land on a falsely reassuring answer.</td></tr></table>

<h3>7.2 Cycle stage</h3>
<table><tr><th>Stage</th><th>Meaning</th></tr>
<tr><td><b>crisis</b></td><td>Funding Liquidity and External Constraint both at their worst labels.</td></tr>
<tr><td><b>late_long_debt_cycle</b></td><td>Sovereign Solvency stressed/critical and Funding Liquidity at watch or worse.</td></tr>
<tr><td><b>private_bubble</b></td><td>Private Credit Cycle at its "bubble" label.</td></tr>
<tr><td><b>late_leveraging</b></td><td>Private Credit Cycle one notch below bubble.</td></tr>
<tr><td><b>contraction</b></td><td>Negative real GDP growth, none of the above triggered.</td></tr>
<tr><td><b>early_or_mid_cycle</b></td><td>None of the above &mdash; the default, not a sign of strength on its own.</td></tr>
<tr><td><b>n/a (unclassifiable)</b></td><td>Same all-or-nothing gate as deleveraging type, across Sovereign Solvency, Funding Liquidity, Private Credit and External Constraint.</td></tr></table>

<h3>7.3 Why labels, not scores</h3>
<p>Every branch above tests an engine's <b>label</b> (e.g. "severe", "stressed") &mdash; never its raw
0-100 score. Only the label is stabilized by that engine's own hysteresis (a score can wobble across a
bucket boundary run to run; the label doesn't, until the move clears a margin). Testing raw scores here
would reintroduce exactly the flutter hysteresis exists to prevent; testing labels means the classifier's
stability is fully inherited from the 5 engines, with no second hysteresis mechanism needed.</p>
"""

# statistics glossary (English) — every statistic + economic meaning
_STATS = r"""
<h2>Statistics used — definitions & economic meaning</h2>
<p>Every metric in this dashboard, how it is computed, and why it matters economically.</p>

<h3>Scoring statistics</h3>
<table><tr><th>Statistic</th><th>Computation</th><th>Economic meaning</th></tr>
<tr><td><b>Cross-country z-score</b></td><td>(x − mean across countries) / std across countries, × direction, clipped to ±3, at the current year</td><td>How far a country sits above/below its peers on an indicator. "× direction" flips indicators where high is bad (debt, unemployment, HY spread) so that <b>higher z = better</b>. Clipping stops one extreme (e.g. Turkish inflation) from dominating.</td></tr>
<tr><td><b>Pillar score</b></td><td>Mean of the cross-country z-scores of the indicators in that pillar</td><td>Relative strength of a country on a theme (growth, debt cycle, liquidity, external, sovereign, banking, governance, geopolitical).</td></tr>
<tr><td><b>Composite</b></td><td>Weighted mean of pillar scores (growth 20, debt_cycle 20, liquidity 15, external 15, sovereign 10, banking 10, governance 5, geopolitical 5; weights renormalised over available pillars)</td><td>Overall <b>relative strength / risk</b> of a country vs peers. NOT a cyclical signal — the cycle is captured by the phase and the regime.</td></tr>
<tr><td><b>Direction (+1/−1/0)</b></td><td>Sign attached to each indicator</td><td>+1 = higher is healthier (reserves, growth); −1 = higher is worse (debt, unemployment, credit spreads); 0 = level/neutral.</td></tr></table>

<h3>Debt-cycle statistics</h3>
<table><tr><th>Statistic</th><th>Computation</th><th>Economic meaning</th></tr>
<tr><td><b>Debt/GDP trajectory (trend)</b></td><td>OLS slope of public debt/GDP over [year−3, year+5], in pp/year, <b>including WEO projections</b></td><td>Dalio's #1 signal: is debt growing faster than income? Forward-looking because the projected path is what matters for sustainability. &gt;+1.5 = rising fast; &lt;0 = deleveraging.</td></tr>
<tr><td><b>Debt level (% GDP)</b></td><td>General government gross debt, current year (WEO)</td><td>Where the country sits in the LONG-term debt cycle. &gt;100% elevated, &gt;130% very high.</td></tr>
<tr><td><b>Credit-to-GDP gap</b></td><td>BIS, private non-financial sector, deviation of credit/GDP from its long-run trend (HP filter, λ=400000)</td><td>The classic bubble gauge: &gt;+10pp signals a credit boom / leverage excess in the private sector.</td></tr>
<tr><td><b>Debt service ratio (DSR)</b></td><td>BIS, private non-financial sector: interest + principal as % of income. Stress read as a <b>percentile of its own history</b> (&gt;80th = peak)</td><td>Affordability of private debt. Read relative to each country's history because ~20% is normal for high-mortgage economies (NL, CH) but high for others. A DSR at its own peak precedes tops/depressions.</td></tr>
<tr><td><b>Nominal growth − nominal rate</b></td><td>(real growth + inflation) − policy rate</td><td>Whether debt dynamics are favourable: if nominal growth &gt; the cost of debt, debt/GDP can fall while growing (beautiful deleveraging); otherwise it is painful.</td></tr></table>

<h3>Growth / inflation statistics</h3>
<table><tr><th>Statistic</th><th>Computation</th><th>Economic meaning</th></tr>
<tr><td><b>Output gap (growth Δ)</b></td><td>Current real growth − potential (WEO medium-term average, [year+2, year+5])</td><td>Is the economy running above or below its own potential? The right normalisation across very different growth profiles (DM vs EM).</td></tr>
<tr><td><b>Inflation direction (inflation Δ)</b></td><td>Current inflation − average of the previous 3 years</td><td>Reflation vs disinflation — the inflation regime in motion, which drives asset returns more than the level.</td></tr></table>

<h3>Country classification statistics</h3>
<table><tr><th>Statistic</th><th>Computation</th><th>Economic meaning</th></tr>
<tr><td><b>Net fuel position (% GDP)</b></td><td>fuel_exports% × exports/GDP − fuel_imports% × imports/GDP</td><td>Net energy trade balance. &gt;+10% strong exporter (Gulf, Norway), &lt;−10% strong importer. Drives terms-of-trade sensitivity to oil.</td></tr>
<tr><td><b>Resource dependence</b></td><td>Natural resource rents (% GDP)</td><td>Exposure to commodity cycles; also the sanity check that an "exporter" is a real producer, not a re-export hub.</td></tr>
<tr><td><b>Development tier</b></td><td>From the analytical region group + income (static)</td><td>DM / EM / Frontier — the structural market category.</td></tr>
<tr><td><b>FX regime, IMF program</b></td><td>Curated static inputs</td><td>Float / managed / pegged / dollarized; whether under an IMF program — both shape policy room and crisis risk.</td></tr></table>

<h3>Data-quality statistics</h3>
<table><tr><th>Statistic</th><th>Meaning</th></tr>
<tr><td><b>Stale-data flag</b></td><td>An indicator whose latest <b>actual</b> observation is older than 3 years (e.g. tourism receipts, resource rents often lag in WDI). Shown per country so the reader knows which inputs are dated.</td></tr>
<tr><td><b>WEO forecast horizon</b></td><td>The last year of IMF WEO projections in the database; the debt trajectory and the charts use these forward-looking values.</td></tr></table>

<p class="muted">Sources: IMF WEO (DataMapper), BIS (stats.bis.org), World Bank WDI/WGI, FRED. All values
anchored to the current year for the "state" metrics; projections used only for the forward-looking
debt trajectory and the charts.</p>

<h2>Dalio v2 — engine input statistics</h2>
<p>Every input shown in a country's expandable "components" panel, grouped by engine. Each
row is scored 0-100 (higher = worse) via <code>score_threshold()</code> against the watch/stress/critical
levels in <code>config/settings.yaml::dalio_v2</code>, then combined into that engine's score with the
listed weight. See section 6 above for the full engine methodology.</p>

<h3>Sovereign Solvency</h3>
<table><tr><th>Input</th><th>Meaning</th></tr>
<tr><td><b>debt_gdp</b></td><td>General government gross debt, % of GDP. The stock of what has to be serviced.</td></tr>
<tr><td><b>net_debt_gdp</b></td><td>Government debt net of financial assets, % of GDP. Less punishing than the gross figure for countries holding large reserves/sovereign funds.</td></tr>
<tr><td><b>interest_gdp</b></td><td>Interest paid on government debt, % of GDP. The direct fiscal drag today.</td></tr>
<tr><td><b>interest_revenue</b></td><td>Interest / government revenue × 100. Interest burden relative to what the state actually collects — a tighter solvency read than interest/GDP.</td></tr>
<tr><td><b>primary_deficit_gdp</b></td><td>−primary balance, % of GDP (negative primary balance = deficit before interest). The fiscal effort needed just to stop debt/GDP from a self-reinforcing primary-deficit spiral.</td></tr>
<tr><td><b>r_minus_g</b></td><td>Effective implied interest rate on debt minus nominal GDP growth (real growth + inflation). The single most important debt-dynamics number: if r&gt;g, debt/GDP compounds upward even with a balanced primary budget.</td></tr>
<tr><td><b>debt_trend_5y</b></td><td>OLS slope of debt/GDP over [year−3, year+5] in pp/year (includes WEO projections). Forward-looking trajectory, same idea as v1's debt-cycle trend but scoped to this engine's own components.</td></tr></table>

<h3>Political Execution (World Bank WGI)</h3>
<table><tr><th>Input</th><th>Meaning</th></tr>
<tr><td><b>government_effectiveness</b></td><td>WGI percentile: quality of public services, civil service, policy formulation and implementation.</td></tr>
<tr><td><b>rule_of_law</b></td><td>WGI percentile: confidence in and abidance by the rules of society — contract enforcement, property rights, courts, police.</td></tr>
<tr><td><b>control_corruption</b></td><td>WGI percentile: extent to which public power is exercised for private gain.</td></tr>
<tr><td><b>political_stability</b></td><td>WGI percentile: likelihood of government destabilization or unconstitutional/violent change, including terrorism.</td></tr>
<tr><td><b>regulatory_quality</b></td><td>WGI percentile: ability of government to formulate and implement sound policies/regulations that permit and promote private-sector development.</td></tr></table>
<p class="muted">All five are 0-100 percentile ranks published by the World Bank; the engine flips them so a
<b>higher engine score = weaker execution</b> (opposite direction from the raw WGI percentile).</p>

<h3>Private Credit Cycle</h3>
<table><tr><th>Input</th><th>Meaning</th></tr>
<tr><td><b>credit_gap</b></td><td>Private non-financial-sector credit/GDP minus its long-run trend. BIS's own gap when available; otherwise a linear-detrend proxy off <code>private_debt_gdp</code> (flagged via <code>credit_gap_source</code> in the audit trail — a proxy value caps the engine at "proxy" coverage even if every other input is present). &gt;+10pp signals a credit boom.</td></tr>
<tr><td><b>private_dsr</b></td><td>BIS private-sector debt-service ratio, expressed as this country's own historical percentile (0-100). Read relative to its own history because "normal" DSR varies a lot by country (e.g. high-mortgage economies run structurally higher).</td></tr>
<tr><td><b>real_credit_growth</b></td><td>YoY % change in private debt/GDP. Rate of leveraging, independent of the level.</td></tr>
<tr><td><b>real_house_price_gap</b></td><td>Real house-price deviation from trend. Not wired to a free data source yet — always shown as "n/a" (structurally missing, not a bug).</td></tr>
<tr><td><b>npl_ratio</b></td><td>Non-performing loans, % of total gross loans. Realized credit losses already on banks' books.</td></tr></table>

<h3>External Currency Constraint</h3>
<table><tr><th>Input</th><th>Meaning</th></tr>
<tr><td><b>current_account_deficit_gdp</b></td><td>−current account balance, % of GDP (positive = deficit). New external financing needed each year.</td></tr>
<tr><td><b>net_external_liability_gdp</b></td><td>−net international investment position, % of GDP (positive = net external debtor). The accumulated stock the country owes the rest of the world.</td></tr>
<tr><td><b>short_term_debt_reserves</b></td><td>Short-term external debt / FX reserves × 100 (Greenspan-Guidotti-style ratio). &gt;100% means reserves don't cover debt rolling over within a year — classic sudden-stop vulnerability gauge.</td></tr>
<tr><td><b>debt_service_exports</b></td><td>External debt service (principal+interest) / exports of goods and services × 100. Ability to pay external obligations out of hard-currency earnings.</td></tr>
<tr><td><b>fx_debt_share</b></td><td>Share of public/external debt denominated in foreign currency. The "original sin" exposure — FX depreciation directly inflates local-currency debt burden.</td></tr>
<tr><td><b>inflation</b></td><td>Headline CPI/WEO inflation. High inflation both signals and causes currency weakness.</td></tr>
<tr><td><b>fx_overvaluation_pct</b></td><td>% deviation of the real effective exchange rate (REER) from its own trailing linear trend. Positive = currency looks rich vs its own recent history, a depreciation-risk flag; not applied to explicit reserve-currency issuers (USA/JPN/GBR/CHE — see <code>is_reserve_currency</code> in the audit trail).</td></tr>
<tr><td><b>reserves_months</b></td><td>FX reserves in months of import cover. Buffer against a sudden stop in external financing.</td></tr></table>

<h3>Funding Liquidity <span class="muted">(proxy-tier placeholder — see §6.4)</span></h3>
<table><tr><th>Input</th><th>Meaning</th></tr>
<tr><td><b>short_term_debt_reserves</b></td><td>Same ratio as External Constraint's — reused here as the best free proxy for near-term rollover/financing pressure (real GFN/auction-calendar data isn't free for most countries).</td></tr>
<tr><td><b>yield_change_12m_pp</b></td><td>10y government bond yield today minus 12 months ago, in percentage points. A sharp rise is the market pricing in funding stress before it shows up anywhere else.</td></tr></table>
"""


def _js_str(x) -> str:
    """json.dumps for embedding inside a <script> block: '</' is escaped to
    '<\\/' (identical once the JS string is parsed) so a stray '</script>'
    in any data/label can never terminate the script element and blank the
    whole page."""
    return json.dumps(x, ensure_ascii=False).replace("</", "<\\/")


def render_html(d: dict) -> str:
    # __DATA__ is substituted LAST so a literal "__METH__"/"__STATS__" inside
    # the (DB-derived) data can never be picked up by a later replace
    return (_TEMPLATE
            .replace("__METH__", _js_str(_METH))
            .replace("__STATS__", _js_str(_STATS))
            .replace("__DATA__", _js_str(d)))


def main() -> int:
    p = argparse.ArgumentParser(description="Ray Dalio dashboard")
    p.add_argument("--db", help="LazyRay's own DuckDB path (output)")
    p.add_argument("--hub-db", help="market-data-hub's DuckDB path (input, read-only); "
                                    "defaults to MARKET_DATA_DB / the hub's own settings")
    p.add_argument("--open", action="store_true")
    p.add_argument("--calc", action="store_true", help="recompute (run_dalio) first")
    p.add_argument("--calc-v2", action="store_true",
                   help="also recompute the Dalio v2 5-engine scores first (additive, see "
                        "docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md)")
    args = p.parse_args()

    if args.calc:
        from lazyray.classify import classify_countries
        from lazyray.dalio import run_dalio
        s = run_dalio(args.db, hub_db_path=args.hub_db)
        classify_countries(args.db, hub_db_path=args.hub_db)
        print(f"Recomputed: {s['countries']} countries, phases {s['phases']}")

    if args.calc_v2:
        from lazyray.dalio_v2.runner import run_dalio_v2
        v2s = run_dalio_v2(db_path=args.db, hub_db_path=args.hub_db)
        print(f"Recomputed Dalio v2: {v2s}")

    REPORT_DIR.mkdir(exist_ok=True)
    con = get_conn(args.db, read_only=True)
    try:
        d = collect(con, hub_db_path=args.hub_db)
    finally:
        con.close()

    stamp = datetime.now().strftime("%Y%m%d")
    out = REPORT_DIR / f"dalio_report_{stamp}.html"
    out.write_text(render_html(d), encoding="utf-8")
    print(f"Dashboard: {out}")
    print(f"Countries: {len(d['countries'])} | phases: {d['phase_counts']}")
    if args.open:
        import webbrowser
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

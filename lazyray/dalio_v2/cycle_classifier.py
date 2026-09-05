# -*- coding: utf-8 -*-
"""
cycle_classifier.py — Dalio v2 Fase 5: the final classifier that sits ON TOP
of the 5 engines. See docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 5
and docs/DALIO_V2_DEEP_AUDIT_2026-07.md for why this exists.

Motivating bug: sovereign_solvency's r_minus_g compares a NOMINAL interest
rate to NOMINAL growth (real growth + inflation). In a high-inflation economy
(Argentina ~30%, Turkey ~28%) this mechanically produces a deeply negative,
"favorable"-looking r-g -- reading as near-zero sovereign risk even though the
falling debt/GDP is pure inflationary erosion, not fiscal health. This was
already predicted in docs/DALIO_METHODOLOGY_REVIEW_2026-07.md §2.2 ("Argentina
finisce in BEAUTIFUL_DELEVERAGING... per erosione inflazionistica") and is
exactly what docs/DALIO_CHATGPT_5ENGINE_PROPOSAL_2026-07.md §10's 5-way
deleveraging taxonomy exists to catch.

Design (see the two docs above for the full rationale):
  - classify_deleveraging()/classify_dalio_stage() are pure functions over a
    plain dict of already-resolved inputs -- no DB access, fully unit
    testable in isolation.
  - classify_dalio_stage() tests each engine's own HYSTERESIS-STABLE LABEL,
    never a raw score: only the label is stabilized by bucket_with_hysteresis
    in each engine; porting the proposal's raw-score cutoffs (funding_score
    >= 80, etc.) would reintroduce the exact flutter hysteresis exists to
    prevent. No second hysteresis mechanism is needed here.
  - Every threshold and label set is read from
    settings.yaml::dalio_v2.cycle_classifier -- never hardcoded here. Only
    the six-category taxonomy structure (which branches exist, in what
    order) is code, same division as the 5 engines (structure in Python,
    calibration in yaml).
  - Coverage gating is per-OUTPUT, all-or-nothing across the engines that
    output's branches reference -- never a soft per-branch skip-and-fall-
    through, which could land a data-starved country on "early_or_mid_cycle"
    / "none" purely because inputs were missing (reads as "fine" when the
    truth is "unknown").
  - Missing-data safety in classify_deleveraging(): a missing operand in an
    AND clause counts as False; in an OR clause it also counts as False. A
    missing input can only ever push the result toward "ugly", never
    manufacture "beautiful" or "repressive" out of nothing, nor suppress
    "inflationary".
  - "repressive" uses a proxy (real_rate_proxy_pct = r_effective_pct -
    inflation_pct, combined with a low fx_debt_share_pct as a stand-in for
    "debt is mostly domestic-currency, absorbable by the local system") --
    there is no central-bank-holdings-of-debt data source, so this is
    honestly weak and will rarely fire. Documented, not hidden.
  - "restructuring" has no data source (no restructuring-event flag exists
    anywhere in this repo) and is folded into "ugly" -- same treatment as
    private_credit.py's always-missing real_house_price_gap.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
import pandas as pd

from lazyray.config_loader import get_settings
from lazyray.dalio_v2.scoring import git_short_sha

_COLUMNS = ["country_iso3", "ref_date", "dalio_stage", "deleveraging_type",
           "overall_confidence", "top_risk_drivers_json", "caveats_json",
           "audit_json", "computed_at"]

# Engines each output's branches read from; used both to gate (all must be
# non-'insufficient') and to pick the worst confidence to report.
_DELEVERAGING_GATE_ENGINES = ("sovereign_solvency", "external_constraint", "funding_liquidity")
_STAGE_GATE_ENGINES = ("sovereign_solvency", "funding_liquidity", "private_credit", "external_constraint")

# Union of the above -- every engine either output's branches can reference.
# Exposed so runner.py can check whether it's even worth refreshing
# dalio_cycle_v2 for a ref_date (see REQUIRED_ENGINES usage there): if one of
# these has literally never been computed for ref_date, compute() would
# write a mostly-unclassifiable batch that overwrites a previously-complete
# classification, since the report picks the latest ref_date globally.
REQUIRED_ENGINES = frozenset(_DELEVERAGING_GATE_ENGINES) | frozenset(_STAGE_GATE_ENGINES)
_ALL_ENGINES = ("sovereign_solvency", "political_execution", "private_credit",
                "external_constraint", "funding_liquidity")

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def classify_deleveraging(inputs: dict, cfg: Optional[dict] = None) -> Optional[str]:
    """none / beautiful / inflationary / repressive / ugly (restructuring is
    folded into ugly, see module docstring). None if debt_trend_actuals_5y
    itself is unknown -- everything else defaults toward "ugly", never a
    safer label, when an input is missing."""
    cfg = cfg or {}
    th = cfg.get("thresholds", {})
    beautiful_funding_labels = set(cfg.get("beautiful_funding_labels", ["easy", "normal"]))

    debt_trend = inputs.get("debt_trend_actuals_5y")
    if debt_trend is None:
        return None
    falling_th = th.get("debt_trend_falling_threshold_pp", -0.2)
    if debt_trend > falling_th:
        return "none"

    growth = inputs.get("real_growth_pct")
    inflation = inputs.get("inflation_pct")
    fx_dep = inputs.get("fx_depreciation_12m_pct")
    funding_label = inputs.get("funding_label")

    growth_th = th.get("real_growth_positive_threshold_pct", 0.0)
    infl_high = th.get("inflation_high_threshold_pct", 10.0)
    fx_high = th.get("fx_depreciation_high_threshold_pct", 15.0)

    is_beautiful = (
        growth is not None and growth > growth_th
        and inflation is not None and inflation < infl_high
        and fx_dep is not None and fx_dep < fx_high
        and funding_label in beautiful_funding_labels)
    if is_beautiful:
        return "beautiful"

    is_inflationary = (
        (inflation is not None and inflation >= infl_high)
        or (fx_dep is not None and fx_dep >= fx_high))
    if is_inflationary:
        return "inflationary"

    real_rate_proxy = inputs.get("real_rate_proxy_pct")
    fx_debt_share = inputs.get("fx_debt_share_pct")
    repressive_rate_th = th.get("repressive_real_rate_threshold_pct", -2.0)
    repressive_fx_share_max = th.get("repressive_fx_debt_share_max_pct", 20.0)
    is_repressive = (
        real_rate_proxy is not None and real_rate_proxy <= repressive_rate_th
        and fx_debt_share is not None and fx_debt_share <= repressive_fx_share_max)
    if is_repressive:
        return "repressive"

    return "ugly"


def classify_dalio_stage(inputs: dict, cfg: Optional[dict] = None) -> Optional[str]:
    """early_or_mid_cycle / late_long_debt_cycle / private_bubble /
    late_leveraging / contraction / crisis. Every branch tests engine LABELS
    (hysteresis-stable), never raw scores -- see module docstring."""
    cfg = cfg or {}
    sl = cfg.get("stage_labels", {})
    th = cfg.get("thresholds", {})

    sovereign_label = inputs.get("sovereign_label")
    funding_label = inputs.get("funding_label")
    private_label = inputs.get("private_label")
    external_label = inputs.get("external_label")
    real_growth = inputs.get("real_growth_pct")

    if (funding_label in set(sl.get("crisis_funding_labels", ["severe"]))
            and external_label in set(sl.get("crisis_external_labels", ["high", "severe"]))):
        return "crisis"

    if (sovereign_label in set(sl.get("late_long_debt_cycle_sovereign_labels", ["stressed", "critical"]))
            and funding_label in set(sl.get("late_long_debt_cycle_funding_labels", ["watch", "stress", "severe"]))):
        return "late_long_debt_cycle"

    if private_label in set(sl.get("private_bubble_labels", ["bubble"])):
        return "private_bubble"

    if private_label in set(sl.get("late_leveraging_labels", ["high"])):
        return "late_leveraging"

    # real_growth_pct is a single field inside sovereign_solvency's audit
    # (not itself coverage-tier-gated -- the engine can be 'full' on its
    # other 6 components with this one missing), so an otherwise-passing
    # engine coverage gate does not guarantee it is present. If none of the
    # label-based branches above fired and growth is unknown, we cannot rule
    # out contraction -- return None (unclassifiable) rather than the "safe"
    # default, which would misrepresent "we don't know" as "this is fine".
    if real_growth is None:
        return None
    contraction_th = th.get("contraction_real_growth_threshold_pct", 0.0)
    if real_growth < contraction_th:
        return "contraction"

    return "early_or_mid_cycle"


def _engine_rows(con: duckdb.DuckDBPyConnection, ref_date) -> dict:
    """{country_iso3: {engine: row_dict}} for every engine_scores row at
    ref_date. row_dict has score/label/coverage_tier/confidence/
    components_json (already json.loads'd, {} on parse failure)."""
    df = con.execute(
        "SELECT country_iso3, engine, score, label, coverage_tier, confidence, "
        "components_json FROM engine_scores WHERE ref_date = ?", [ref_date]).fetch_df()
    by_country: dict = {}
    for _, r in df.iterrows():
        try:
            audit = json.loads(r["components_json"]) if r["components_json"] else {}
        except Exception:
            audit = {}
        by_country.setdefault(r["country_iso3"], {})[r["engine"]] = {
            "score": None if pd.isna(r["score"]) else float(r["score"]),
            "label": None if pd.isna(r["label"]) else r["label"],
            "coverage_tier": None if pd.isna(r["coverage_tier"]) else r["coverage_tier"],
            "confidence": None if pd.isna(r["confidence"]) else r["confidence"],
            "audit": audit,
        }
    return by_country


def _worst_confidence(rows: dict, engines) -> str:
    worst = "high"
    for e in engines:
        conf = rows.get(e, {}).get("confidence")
        rank = _CONFIDENCE_RANK.get(conf, 0)   # missing engine ranks below 'low'
        if rank <= _CONFIDENCE_RANK[worst]:
            worst = conf if conf in _CONFIDENCE_RANK else "low"
    return worst


def _gate(rows: dict, engines) -> Optional[list]:
    """None if every engine's coverage_tier is present and != 'insufficient';
    otherwise the list of engine names that failed the gate (missing row or
    insufficient tier), for the audit trail."""
    failed = [e for e in engines
             if rows.get(e, {}).get("coverage_tier") in (None, "insufficient")]
    return failed or None


def _top_risk_drivers(rows: dict, top_n: int) -> list:
    drivers = []
    for engine, row in rows.items():
        if row.get("coverage_tier") == "insufficient":
            continue
        for name, c in (row.get("audit", {}).get("components") or {}).items():
            score = c.get("score")
            weight = c.get("weight") or 0
            if score is not None and weight > 0:
                drivers.append({"engine": engine, "component": name, "score": score,
                                "raw_value": c.get("raw_value"), "weight": weight})
    drivers.sort(key=lambda d: -d["score"])
    return drivers[:top_n]


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None
           ) -> pd.DataFrame:
    """dalio_stage + deleveraging_type for every country with at least one
    engine_scores row at ref_date. Returns a DataFrame ready to write to
    dalio_cycle_v2."""
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("cycle_classifier", {})
    top_n = cfg.get("top_risk_drivers_n", 3)

    by_country = _engine_rows(con, ref_date)
    if not by_country:
        return pd.DataFrame(columns=_COLUMNS)

    sha = git_short_sha()
    now = datetime.now(timezone.utc)
    # dalio_stage/deleveraging_type are genuinely Optional[str] (both rows
    # below can carry either a real stage or None); country/ref_date stay Any
    # since _engine_rows()/compute()'s own signatures leave them untyped.
    rows_out: list[tuple[Any, Any, Optional[str], Optional[str], str, str, str, str, datetime]] = []
    for country, rows in by_country.items():
        # funding_liquidity's branch=none/coverage_tier='no_data' (WP2,
        # docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.7: 14 countries with
        # neither the market nor the external funding data branch) gets its
        # OWN named stage instead of falling through the generic gate below
        # into a bare, unexplained None -- "we genuinely have no funding
        # data for this country" is a different fact from "some gate engine
        # had insufficient coverage this run".
        if rows.get("funding_liquidity", {}).get("coverage_tier") == "no_data":
            audit_json = {
                "model_version": sha,
                "engines_used": {e: rows.get(e, {}).get("coverage_tier") or "missing"
                                 for e in _ALL_ENGINES},
                "unclassifiable_reason": {"dalio_stage": ["funding_liquidity:no_data"],
                                          "deleveraging_type": ["funding_liquidity:no_data"]},
            }
            rows_out.append((
                country, ref_date, "unclassified_no_funding_data", None, "low",
                json.dumps(_top_risk_drivers(rows, top_n)),
                json.dumps(["funding_liquidity: no funding data for this country "
                           "(branch=none) -- dalio_stage is never a bare None for this reason"]),
                json.dumps(audit_json), now,
            ))
            continue

        sov = rows.get("sovereign_solvency", {})
        ext = rows.get("external_constraint", {})

        sov_audit = sov.get("audit", {})
        ext_audit = ext.get("audit", {})
        ext_components = ext_audit.get("components", {}) or {}

        real_growth_pct = sov_audit.get("real_growth_pct")
        r_effective_pct = sov_audit.get("r_effective_pct")
        debt_trend_actuals_5y = sov_audit.get("debt_trend_actuals_5y")
        inflation_pct = (ext_components.get("inflation") or {}).get("raw_value")
        fx_debt_share_pct = (ext_components.get("fx_debt_share") or {}).get("raw_value")
        fx_depreciation_12m_pct = ext_audit.get("fx_depreciation_12m_pct")
        real_rate_proxy_pct = (r_effective_pct - inflation_pct) \
            if r_effective_pct is not None and inflation_pct is not None else None

        inputs = {
            "debt_trend_actuals_5y": debt_trend_actuals_5y,
            "real_growth_pct": real_growth_pct,
            "inflation_pct": inflation_pct,
            "fx_depreciation_12m_pct": fx_depreciation_12m_pct,
            "real_rate_proxy_pct": real_rate_proxy_pct,
            "fx_debt_share_pct": fx_debt_share_pct,
            "sovereign_label": sov.get("label"),
            "funding_label": rows.get("funding_liquidity", {}).get("label"),
            "private_label": rows.get("private_credit", {}).get("label"),
            "external_label": ext.get("label"),
        }

        deleveraging_missing = _gate(rows, _DELEVERAGING_GATE_ENGINES)
        stage_missing = _gate(rows, _STAGE_GATE_ENGINES)
        deleveraging_type = None if deleveraging_missing else classify_deleveraging(inputs, cfg)
        dalio_stage = None if stage_missing else classify_dalio_stage(inputs, cfg)
        # classify_dalio_stage can itself return None (real_growth_pct
        # missing) even though every gate engine's coverage_tier passed --
        # growth is one field inside sovereign_solvency's audit, not
        # separately tier-gated. Track that distinctly from a full engine-
        # coverage failure so the audit trail always explains a None.
        stage_reason = stage_missing
        if dalio_stage is None and not stage_missing:
            stage_reason = ["sovereign_solvency:real_growth_pct"]

        overall_confidence = _worst_confidence(
            rows, set(_DELEVERAGING_GATE_ENGINES) | set(_STAGE_GATE_ENGINES))

        caveats = []
        if deleveraging_missing:
            caveats.append(f"deleveraging_type unclassifiable: insufficient coverage in "
                           f"{', '.join(sorted(deleveraging_missing))}")
        if stage_reason:
            caveats.append(f"dalio_stage unclassifiable: {', '.join(sorted(stage_reason))} "
                           f"missing/insufficient")
        if deleveraging_type == "repressive":
            caveats.append("'repressive' is a weak proxy (real rate vs FX debt share) -- "
                           "no central-bank-holdings-of-debt data source exists.")

        audit_json = {
            "model_version": sha,
            "engines_used": {e: rows.get(e, {}).get("coverage_tier") or "missing"
                             for e in _ALL_ENGINES},
            "unclassifiable_reason": {
                "dalio_stage": stage_reason,
                "deleveraging_type": deleveraging_missing,
            },
        }

        rows_out.append((
            country, ref_date, dalio_stage, deleveraging_type, overall_confidence,
            json.dumps(_top_risk_drivers(rows, top_n)), json.dumps(caveats),
            json.dumps(audit_json), now,
        ))

    return pd.DataFrame(rows_out, columns=_COLUMNS)

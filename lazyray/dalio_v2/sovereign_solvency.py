# -*- coding: utf-8 -*-
"""
sovereign_solvency.py — Dalio v2 Engine 1: Sovereign Solvency.

Can the state service its debt without default, extreme financial repression,
high inflation, persistent monetization, or politically destabilizing
austerity? See docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 1 for
the full design (7 components, r-g formula, income-group thresholds).

Reads the live v_macro_panel_ext (current known values, not vintage-aware
yet) — components_json marks vintage_safe=False. Fase A of
DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md wires asof= point-in-time reads for
historical backtesting in a later pass; this module stays unchanged, only
its data source becomes swappable then.

debt_trend_5y reuses dalio.py's _slope()/_first_avail()/_latest() so the two
systems' debt trajectories are defined identically (same window, same
forecast inclusion), not two subtly different formulas.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import pandas as pd
from market_data_hub.config_loader import get_countries
from market_data_hub.reader import read_macro_panel_ext

from lazyray.config_loader import get_settings
from lazyray.dalio import _slope
from lazyray.dalio_v2.scoring import (
    bucket_with_hysteresis, confidence_for, coverage_tier, fresh_first_avail,
    git_short_sha, notna, prev_label, score_threshold, suppress_insufficient,
    weighted_average,
)

ENGINE = "sovereign_solvency"

_IND = {
    "debt_gdp": "public_debt_gdp",
    "net_debt_gdp": "govt_net_debt_gdp",
    "interest_gdp": "interest_on_debt_gdp",
    "revenue_gdp": "government_revenue_gdp",
    "primary_balance_gdp": "primary_balance_gdp",
    "growth": ["gdp_growth_weo", "real_gdp_growth"],
    "inflation": ["inflation_avg_weo", "inflation_cpi"],
    "r_effective": "implied_interest_rate",
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at"]


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None,
           hub_db_path: Optional[str] = None) -> pd.DataFrame:
    """Sovereign Solvency scores for every country in the panel as of
    ref_date. Returns a DataFrame ready to write to engine_scores.

    con         -- LazyRay's own DB connection (hysteresis lookups via
                   prev_label(), which reads this engine's own past output).
    hub_db_path -- market-data-hub's DB (input, read-only via
                   reader.read_macro_panel_ext).
    """
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("sovereign_solvency", {})
    th = cfg.get("thresholds", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels",
                            ["strong", "stable", "watch", "stressed", "critical"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)
    max_age = settings.get("staleness_max_age_years", 4)

    dev = {c["iso3"]: c.get("development", "EM") for c in get_countries()}

    panel = read_macro_panel_ext(db_path=hub_db_path)
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)
    panel["date"] = pd.to_datetime(panel["date"])
    ref_ts = pd.Timestamp(ref_date)

    sha = git_short_sha()
    now = datetime.now(timezone.utc)
    rows = []
    for country, cdf_full in panel.groupby("country_iso3"):
        cdf = cdf_full[cdf_full["date"] <= ref_ts]
        if cdf.empty:
            continue
        by_ind = {i: g[["date", "value"]] for i, g in cdf.groupby("indicator_id")}

        # fresh_first_avail (not dalio._first_avail): a stale primary series
        # must not shadow a fresh fallback (gdp_growth_weo vs real_gdp_growth)
        debt, debt_dt = fresh_first_avail(by_ind, _IND["debt_gdp"], ref_ts, max_age)
        net_debt, net_debt_dt = fresh_first_avail(by_ind, _IND["net_debt_gdp"], ref_ts, max_age)
        interest_gdp, interest_dt = fresh_first_avail(by_ind, _IND["interest_gdp"], ref_ts, max_age)
        revenue_gdp, _ = fresh_first_avail(by_ind, _IND["revenue_gdp"], ref_ts, max_age)
        primary_balance, primary_dt = fresh_first_avail(by_ind, _IND["primary_balance_gdp"], ref_ts, max_age)
        growth, _ = fresh_first_avail(by_ind, _IND["growth"], ref_ts, max_age)
        infl, _ = fresh_first_avail(by_ind, _IND["inflation"], ref_ts, max_age)
        r_eff, r_eff_dt = fresh_first_avail(by_ind, _IND["r_effective"], ref_ts, max_age)

        debt_full = cdf_full[cdf_full["indicator_id"] == _IND["debt_gdp"]][["date", "value"]]
        debt_trend = _slope(debt_full, ref_ts.year - 3, ref_ts.year + 5)
        # Companion actuals-only slope (plan Fase 1 / methodology-review
        # P1.2): the trajectory above deliberately includes WEO forecasts,
        # so the audit trail must show how much of it is forecast-driven.
        debt_hist = cdf[cdf["indicator_id"] == _IND["debt_gdp"]][["date", "value"]]
        debt_trend_actuals = _slope(debt_hist, ref_ts.year - 5, ref_ts.year)

        g_nom = (((1 + growth / 100.0) * (1 + infl / 100.0)) - 1) * 100.0 \
            if notna(growth) and notna(infl) else float("nan")
        r_minus_g = (r_eff - g_nom) if notna(r_eff) and notna(g_nom) else float("nan")
        interest_revenue = (interest_gdp / revenue_gdp * 100.0) \
            if notna(interest_gdp) and notna(revenue_gdp) and revenue_gdp != 0 \
            else float("nan")
        primary_deficit = -primary_balance if notna(primary_balance) else float("nan")

        grp = "dm" if dev.get(country, "EM") == "DM" else "em"
        debt_th = th.get(f"debt_gdp_{grp}", [90, 110, 130])
        net_debt_th = th.get(f"net_debt_gdp_{grp}", [90, 110, 130])

        raw_values = {
            "debt_gdp": debt, "net_debt_gdp": net_debt, "interest_revenue": interest_revenue,
            "interest_gdp": interest_gdp, "primary_deficit_gdp": primary_deficit,
            "r_minus_g": r_minus_g, "debt_trend_5y": debt_trend,
        }
        # observation date of each component's primary input (None for the
        # windowed trend); derived components inherit their numerator's date
        obs_dates = {
            "debt_gdp": debt_dt, "net_debt_gdp": net_debt_dt,
            "interest_revenue": interest_dt, "interest_gdp": interest_dt,
            "primary_deficit_gdp": primary_dt, "r_minus_g": r_eff_dt,
            "debt_trend_5y": None,
        }
        components = {
            "debt_gdp": score_threshold(debt, *debt_th),
            "net_debt_gdp": score_threshold(net_debt, *net_debt_th),
            "interest_revenue": score_threshold(interest_revenue, *th.get("interest_revenue", [10, 15, 25])),
            "interest_gdp": score_threshold(interest_gdp, *th.get("interest_gdp", [3, 5, 7])),
            "primary_deficit_gdp": score_threshold(primary_deficit, *th.get("primary_deficit_gdp", [2, 4, 6])),
            "r_minus_g": score_threshold(r_minus_g, *th.get("r_minus_g", [1, 3, 5])),
            "debt_trend_5y": score_threshold(debt_trend, *th.get("debt_trend_5y", [0.7, 1.5, 3.0])),
        }
        score, n_avail, n_exp = weighted_average(components, weights)
        tier = coverage_tier(n_avail, n_exp)
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date), "asof": None,
            "income_group": dev.get(country, "EM"),
            # forecast-dependence flag: how the scored (forecast-inclusive)
            # trajectory compares with a slope over realized actuals only
            "debt_trend_actuals_5y": None if pd.isna(debt_trend_actuals)
                else round(float(debt_trend_actuals), 4),
            "debt_trend_forecast_dependent": (
                None if pd.isna(debt_trend) or pd.isna(debt_trend_actuals)
                else bool(abs(debt_trend - debt_trend_actuals) > 1.0)),
            # Persisted (not just used internally for r_minus_g/g_nom) so the
            # Fase 5 cycle classifier can read real growth without
            # re-deriving it: see market_data_hub/dalio_v2/cycle_classifier.py.
            "real_growth_pct": round(float(growth), 4) if notna(growth) else None,
            "r_effective_pct": round(float(r_eff), 4) if notna(r_eff) else None,
            "components": {
                k: {"raw_value": None if pd.isna(raw_values[k]) else round(float(raw_values[k]), 4),
                    "score": components[k], "weight": weights.get(k, 0),
                    "obs_date": obs_dates.get(k)}
                for k in components
            },
            "missing_components": [k for k, v in components.items() if v is None],
            "coverage_tier": tier, "vintage_safe": False,
        }
        rows.append((country, ref_date, ENGINE,
                    None if score is None else round(score, 2), label, tier, conf,
                    n_avail, n_exp, json.dumps(audit), now))

    return pd.DataFrame(rows, columns=_COLUMNS)

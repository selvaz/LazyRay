# -*- coding: utf-8 -*-
"""
external_constraint.py — Dalio v2 Engine 4: External Currency Constraint.

Measures whether a country has an external (currency/balance-of-payments)
constraint that could turn a fiscal problem into a currency crisis. See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 3.

Data reality (verified 2026-07-09, see the plan doc §2.2): a full
sector-of-holder external-debt dataset does not exist free at 60+ country
breadth. This engine therefore mixes:
  - fx_debt_share (IMF IIPCC view, already in v_macro_panel_ext) — full
    quality, but only ~19 major economies;
  - short_term_debt_reserves / debt_service_exports (World Bank WDI,
    already in macro_panel.yaml — no new connector needed, see note below)
    — broader (~120+ countries) but external debt only, no sector/holder
    detail;
  - current_account_gdp, iip_net_position, fx_reserves_months_imports,
    reer_broad — already in macro_panel, DM+EM broad coverage.

NOTE (2026-07-09): this module originally read two new "World Bank IDS"
indicators added in the same session, whose api_source_id could not be
live-verified (network-blocked sandbox). On review, that was unnecessary —
macro_panel.yaml already carried external_debt_gni and debt_service_exports
(WDI, api_source_id 2, already live/verified) under the SAME World Bank
codes, and short_term_debt_reserves (WDI, DT.DOD.DSTC.IR.ZS) is literally
the "short-term external debt/reserves" ratio the source proposal asks for
— a better fit than the %-of-total-external-debt ratio the removed IDS
indicator used. The IDS block was deleted from macro_panel.yaml and this
module now reads the pre-existing WDI indicators instead; no new connector,
no unverified source id.

THRESHOLDS: current_account_deficit_gdp, debt_service_exports,
short_term_debt_reserves and reserves_months come from the source proposal
(§12.4 — short_term_debt_reserves uses the proposal's own 50/100/150
watch/stress/critical). The remaining four (net_external_liability_gdp,
fx_debt_share, inflation, fx_overvaluation_pct) have no proposal thresholds
and are ASSUMED — see config/settings.yaml's dalio_v2.external_constraint
comment. Revisit once Fase 6 (historical backtest) gives real calibration
evidence.

Reserve-currency caveat (proposal §19.3/§8.5): for USA, JPN, GBR, CHE and
euro-area members, the raw score is discounted (reserve_currency_discount)
since a reserve-currency issuer can sustain external imbalances longer —
but this does NOT mean zero risk, only a different (monetary debasement,
not classic BoP crisis) risk channel; the discount is applied, never
zeroed, and the caveat is recorded in components_json so it is never
silently invisible.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from market_data_hub.config_loader import get_countries
from market_data_hub.reader import read_macro_panel_ext

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.scoring import (
    bucket_with_hysteresis, confidence_for, coverage_tier, fresh_latest,
    git_short_sha, notna, prev_label, round_or_none, score_threshold,
    suppress_insufficient, weighted_average,
)

ENGINE = "external_constraint"

_IND = {
    "current_account": "current_account_gdp",
    "niip": "iip_net_position",
    "gdp_usd": "gdp_current_usd",
    "short_term_debt_reserves": "short_term_debt_reserves",
    "debt_service_exports": "debt_service_exports",
    "fx_debt_share": "fx_debt_share",
    "inflation": ["inflation_avg_weo", "inflation_cpi"],
    "reer": "reer_broad",
    "reserves_months": "fx_reserves_months_imports",
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at"]

_EXPLICIT_RESERVE_CURRENCY = {"USA", "JPN", "GBR", "CHE"}
_MIN_TREND_POINTS = 24   # reer_broad is monthly; require ~2y of history


def _pct_deviation_from_trend(s: Optional[pd.DataFrame], window_years: int = 10
                              ) -> Optional[float]:
    """% deviation of the latest value from a linear (OLS) trend over the
    trailing `window_years` of (possibly monthly) history. Used for REER
    over/under-valuation, analogous to private_credit's detrend gap but in
    % terms since REER is an index level (~100 baseline), not a ratio."""
    if s is None or s.empty:
        return None
    d = s.sort_values("date").copy()
    cutoff = d["date"].max() - pd.DateOffset(years=window_years)
    d = d[d["date"] >= cutoff].dropna(subset=["value"])
    if len(d) < _MIN_TREND_POINTS:
        return None
    x = np.arange(len(d))
    slope, intercept = np.polyfit(x, d["value"].values, 1)
    trend_latest = slope * x[-1] + intercept
    if not trend_latest:
        return None
    return float((d["value"].values[-1] - trend_latest) / trend_latest * 100.0)


def _fx_depreciation_12m_pct(s: Optional[pd.DataFrame]) -> Optional[float]:
    """Realized % depreciation of the currency over the trailing 12 months,
    from the same REER series `_pct_deviation_from_trend` uses for the
    (separate, 10y-trend) overvaluation read. Sign-flipped so positive =
    depreciation (worse), consistent with every other component's
    higher-is-worse orientation -- REER itself rises when the currency
    STRENGTHENS. Feeds the Fase 5 cycle classifier only (see
    cycle_classifier.py); not one of this engine's own scored components,
    so it can never silently recalibrate external_constraint's own score.
    Same 12-18 month prior-print tolerance as funding_liquidity's
    _yoy_level_change: a gappy series must not silently report a multi-year
    change as if it were a 12-month one."""
    if s is None or s.empty:
        return None
    d = s.sort_values("date").dropna(subset=["value"])
    if len(d) < 2:
        return None
    latest_date, latest_val = d["date"].iloc[-1], d["value"].iloc[-1]
    target = latest_date - pd.DateOffset(months=12)
    prior = d[(d["date"] <= target) & (d["date"] >= latest_date - pd.DateOffset(months=18))]
    if prior.empty:
        return None
    prior_val = prior["value"].iloc[-1]
    if pd.isna(prior_val) or not prior_val:
        return None
    return float(-(latest_val - prior_val) / prior_val * 100.0)


def _reserve_currency_iso3s() -> set:
    euro_members = {c["iso3"] for c in get_countries() if c.get("euro")}
    return _EXPLICIT_RESERVE_CURRENCY | euro_members


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None,
           hub_db_path: Optional[str] = None) -> pd.DataFrame:
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("external_constraint", {})
    th = cfg.get("thresholds", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels", ["low", "moderate", "elevated", "high", "severe"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)
    max_age = settings.get("staleness_max_age_years", 4)
    reserve_discount = cfg.get("reserve_currency_discount", 0.6)

    reserve_currencies = _reserve_currency_iso3s()

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

        current_account, ca_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["current_account"])), ref_ts, max_age)
        niip, niip_dt = fresh_latest(_latest(_first_avail(by_ind, _IND["niip"])), ref_ts, max_age)
        gdp_usd, _ = fresh_latest(_latest(_first_avail(by_ind, _IND["gdp_usd"])), ref_ts, max_age)
        short_term_reserves, strd_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["short_term_debt_reserves"])), ref_ts, max_age)
        debt_service_exports, dse_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["debt_service_exports"])), ref_ts, max_age)
        fx_debt_share, fxd_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["fx_debt_share"])), ref_ts, max_age)
        inflation, infl_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["inflation"])), ref_ts, max_age)
        reserves_months, resm_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["reserves_months"])), ref_ts, max_age)

        # REER history must come from the ref_date-filtered frame like every
        # other component: it is actual monthly BIS data (no forecasts), and
        # anchoring the trend on the unfiltered series would leak post-ref_date
        # observations into a historical run. Same staleness gate as the other
        # derived metrics: a series that stopped updating years ago must not
        # keep producing a "current" overvaluation.
        reer_hist = cdf[cdf["indicator_id"] == _IND["reer"]][["date", "value"]]
        latest_reer, _ = fresh_latest(_latest(reer_hist), ref_ts, max_age)
        fx_overvaluation = _pct_deviation_from_trend(reer_hist) \
            if latest_reer is not None else None
        # Fase 5 cycle-classifier input only (see cycle_classifier.py) -- a
        # realized 12m depreciation, distinct from fx_overvaluation_pct's 10y
        # trend deviation. Not one of this engine's scored components.
        fx_depreciation_12m = _fx_depreciation_12m_pct(reer_hist) \
            if latest_reer is not None else None

        current_account_deficit = -current_account if notna(current_account) else None
        niip_gdp = (niip / gdp_usd * 100.0) \
            if notna(niip) and notna(gdp_usd) and gdp_usd else None
        net_external_liability = -niip_gdp if niip_gdp is not None else None

        raw_values = {
            "current_account_deficit_gdp": current_account_deficit,
            "net_external_liability_gdp": net_external_liability,
            "short_term_debt_reserves": None if pd.isna(short_term_reserves) else short_term_reserves,
            "debt_service_exports": None if pd.isna(debt_service_exports) else debt_service_exports,
            "fx_debt_share": None if pd.isna(fx_debt_share) else fx_debt_share,
            "inflation": None if pd.isna(inflation) else inflation,
            "fx_overvaluation_pct": fx_overvaluation,
            "reserves_months": None if pd.isna(reserves_months) else reserves_months,
        }
        obs_dates = {
            "current_account_deficit_gdp": ca_dt, "net_external_liability_gdp": niip_dt,
            "short_term_debt_reserves": strd_dt, "debt_service_exports": dse_dt,
            "fx_debt_share": fxd_dt, "inflation": infl_dt,
            "fx_overvaluation_pct": None, "reserves_months": resm_dt,
        }
        # unpacked to exactly 3 names (not splatted from a variable-length
        # list) so a misconfigured 4-element threshold list in settings.yaml
        # can't silently collide with the explicit orientation= kwarg below
        _resm_w, _resm_s, _resm_c = th.get("reserves_months", [4, 3, 2])
        _reserves_months_th = (_resm_w, _resm_s, _resm_c)

        components = {
            "current_account_deficit_gdp": None if current_account_deficit is None else
                score_threshold(current_account_deficit, *th.get("current_account_deficit_gdp", [3, 5, 8])),
            "net_external_liability_gdp": None if net_external_liability is None else
                score_threshold(net_external_liability, *th.get("net_external_liability_gdp", [35, 50, 70])),
            "short_term_debt_reserves": None if raw_values["short_term_debt_reserves"] is None else
                score_threshold(raw_values["short_term_debt_reserves"], *th.get("short_term_debt_reserves", [50, 100, 150])),
            "debt_service_exports": None if raw_values["debt_service_exports"] is None else
                score_threshold(raw_values["debt_service_exports"], *th.get("debt_service_exports", [15, 25, 40])),
            "fx_debt_share": None if raw_values["fx_debt_share"] is None else
                score_threshold(raw_values["fx_debt_share"], *th.get("fx_debt_share", [30, 50, 70])),
            "inflation": None if raw_values["inflation"] is None else
                score_threshold(raw_values["inflation"], *th.get("inflation", [5, 10, 20])),
            "fx_overvaluation_pct": None if fx_overvaluation is None else
                score_threshold(fx_overvaluation, *th.get("fx_overvaluation_pct", [10, 20, 30])),
            "reserves_months": None if raw_values["reserves_months"] is None else
                score_threshold(raw_values["reserves_months"], *_reserves_months_th,
                                orientation=-1),
        }
        score, n_avail, n_exp = weighted_average(components, weights)

        is_reserve_currency = country in reserve_currencies
        caveats = []
        if is_reserve_currency and score is not None:
            score = round(score * reserve_discount, 2)
            caveats.append(
                "Reserve currency issuer: external constraint score discounted "
                f"x{reserve_discount} (less immediate BoP risk); monetary "
                "debasement/inflation risk is elevated instead, not captured here.")

        # fx_debt_share (the single best-quality input, IIPCC) covers only
        # ~19 countries; without it the remaining inputs are all broader-but-
        # coarser proxies, so this can never read as 'full' coverage even if
        # every proxy component is present.
        tier = coverage_tier(n_avail, n_exp)
        if components.get("fx_debt_share") is None and tier == "full":
            tier = "proxy"
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date), "asof": None,
            "is_reserve_currency": is_reserve_currency, "caveats": caveats,
            # Fase 5 cycle-classifier input, not a scored component (see
            # module docstring above and cycle_classifier.py).
            "fx_depreciation_12m_pct": (None if fx_depreciation_12m is None
                                        else round(float(fx_depreciation_12m), 4)),
            "components": {
                k: {"raw_value": round_or_none(raw_values.get(k)),
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

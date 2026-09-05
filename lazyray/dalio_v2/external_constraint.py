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
evidence. funding_liquidity.py's external branch reuses debt_service_exports'
numbers straight from here (see that module) rather than duplicating them.

Reserve-currency caveat (proposal §19.3/§8.5): for USA, JPN, GBR, CHE and
euro-area members, the raw score is discounted (reserve_currency_discount)
since a reserve-currency issuer can sustain external imbalances longer —
but this does NOT mean zero risk, only a different (monetary debasement,
not classic BoP crisis) risk channel; the discount is applied, never
zeroed, and the caveat is recorded in components_json so it is never
silently invisible. sovereign_solvency.py's fx-constraint gate
(docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.6) reuses
_reserve_currency_iso3s() from here, the SAME reserve-currency definition,
rather than a second one.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from market_data_hub.config_loader import get_countries

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path
from lazyray.dalio_v2.scoring import (
    actual_cutoff, bucket_with_hysteresis, clip_to_cutoff, confidence_for,
    coverage_tier, fresh_first_avail, fresh_latest, git_short_sha, notna,
    own_history_pct, percentile_group, prev_label, relative_score_from_pct_group,
    round_or_none, score_threshold, suppress_insufficient, weighted_average,
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
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

_EXPLICIT_RESERVE_CURRENCY = {"USA", "JPN", "GBR", "CHE"}
_MIN_TREND_POINTS = 24   # reer_broad is monthly; require ~2y of history
_OWN_HISTORY_MIN_OBS = 15


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

    dev = {c["iso3"]: c.get("development", "EM") for c in get_countries()}
    reserve_currencies = _reserve_currency_iso3s()

    panel = load_panel_ext(ref_date, hub_db_path)
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)
    panel["date"] = pd.to_datetime(panel["date"])
    ref_ts = pd.Timestamp(ref_date)
    cutoff = actual_cutoff(ref_date)
    vintage_safe = used_asof_path(ref_date)

    sha = git_short_sha()
    now = datetime.now(timezone.utc)
    records = []
    raw_by_component: "dict[str, dict]" = {}
    for country, cdf_full in panel.groupby("country_iso3"):
        cdf = cdf_full[cdf_full["date"] <= ref_ts]
        if cdf.empty:
            continue
        by_ind = {i: g[["date", "value"]] for i, g in cdf.groupby("indicator_id")}

        # clip_to_cutoff() on every candidate series here: all WEO/WDI/IIP/
        # IIPCC annual LEVEL reads -- see scoring.actual_cutoff(). Clipping
        # the series BEFORE _latest() (not rejecting the result afterward)
        # is what lets a country with both a 2025 actual and a 2026 forecast
        # row correctly fall back to the 2025 actual. REER-derived reads
        # below (fx_overvaluation/fx_depreciation_12m) deliberately do NOT
        # clip: reer_broad is BIS monthly with no forecast rows, gated on
        # ref_ts + max_age only, same as private_credit's BIS reads.
        s_ca = clip_to_cutoff(_first_avail(by_ind, _IND["current_account"]), cutoff)
        current_account, ca_dt = fresh_latest(_latest(s_ca), ref_ts, max_age)
        niip, niip_dt = fresh_latest(
            _latest(clip_to_cutoff(_first_avail(by_ind, _IND["niip"]), cutoff)), ref_ts, max_age)
        gdp_usd, _ = fresh_latest(
            _latest(clip_to_cutoff(_first_avail(by_ind, _IND["gdp_usd"]), cutoff)), ref_ts, max_age)
        s_strd = clip_to_cutoff(_first_avail(by_ind, _IND["short_term_debt_reserves"]), cutoff)
        short_term_reserves, strd_dt = fresh_latest(_latest(s_strd), ref_ts, max_age)
        s_dse = clip_to_cutoff(_first_avail(by_ind, _IND["debt_service_exports"]), cutoff)
        debt_service_exports, dse_dt = fresh_latest(_latest(s_dse), ref_ts, max_age)
        s_fxd = clip_to_cutoff(_first_avail(by_ind, _IND["fx_debt_share"]), cutoff)
        fx_debt_share, fxd_dt = fresh_latest(_latest(s_fxd), ref_ts, max_age)
        # fresh_first_avail (not _first_avail+fresh_latest): "inflation" is a
        # 2-candidate fallback list (WEO then CPI) -- a stale WEO print must
        # not shadow a fresh CPI one (Codex review, same class of bug already
        # guarded against in sovereign_solvency.py).
        s_infl = clip_to_cutoff(_first_avail(by_ind, _IND["inflation"]), cutoff)
        inflation, infl_dt = fresh_first_avail(by_ind, _IND["inflation"], ref_ts, max_age, cutoff)
        s_resm = clip_to_cutoff(_first_avail(by_ind, _IND["reserves_months"]), cutoff)
        reserves_months, resm_dt = fresh_latest(_latest(s_resm), ref_ts, max_age)

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
        data_through = max((d for d in obs_dates.values() if d), default=None)

        # own-history percentile: current_account_deficit_gdp (sign-flipped
        # current_account), short_term_debt_reserves, debt_service_exports,
        # fx_debt_share, inflation and reserves_months (orientation=-1, lower
        # is worse) all map onto a single indicator's own series;
        # net_external_liability_gdp (niip/gdp ratio) and fx_overvaluation_pct
        # (already itself a rolling-trend residual) are derived, see module.
        pct_own_history = {
            "current_account_deficit_gdp": own_history_pct(
                s_ca, current_account, ca_dt, -1, _OWN_HISTORY_MIN_OBS),
            "net_external_liability_gdp": None,
            "short_term_debt_reserves": own_history_pct(
                s_strd, raw_values["short_term_debt_reserves"], strd_dt, 1, _OWN_HISTORY_MIN_OBS),
            "debt_service_exports": own_history_pct(
                s_dse, raw_values["debt_service_exports"], dse_dt, 1, _OWN_HISTORY_MIN_OBS),
            "fx_debt_share": own_history_pct(
                s_fxd, raw_values["fx_debt_share"], fxd_dt, 1, _OWN_HISTORY_MIN_OBS),
            "inflation": own_history_pct(s_infl, raw_values["inflation"], infl_dt, 1, _OWN_HISTORY_MIN_OBS),
            "fx_overvaluation_pct": None,
            "reserves_months": own_history_pct(
                s_resm, raw_values["reserves_months"], resm_dt, -1, _OWN_HISTORY_MIN_OBS),
        }

        # raw_by_component feeds ONLY percentile_group (peer comparison),
        # which assumes higher-raw = worse (see scoring.percentile_group's
        # docstring) -- flip the one component whose raw scale runs the
        # other way. reserves_months is scored with orientation=-1 above
        # (fewer months of import cover is worse), so its raw sign must be
        # negated here too, same treatment as political_execution's WGI
        # flip. raw_values itself (the audit trail / Brief-facing dict)
        # stays untouched -- the true, unflipped months-of-reserves figure.
        for comp, v in raw_values.items():
            peer_v = -v if (comp == "reserves_months" and v is not None) else v
            raw_by_component.setdefault(comp, {})[country] = peer_v

        records.append(dict(
            country=country, score=score, label=label, tier=tier, conf=conf,
            n_avail=n_avail, n_exp=n_exp, raw_values=raw_values, obs_dates=obs_dates,
            components=components, pct_own_history=pct_own_history, data_through=data_through,
            is_reserve_currency=is_reserve_currency, caveats=caveats,
            fx_depreciation_12m=fx_depreciation_12m,
        ))

    if not records:
        return pd.DataFrame(columns=_COLUMNS)

    pct_group_by_component = {comp: percentile_group(vals, dev) for comp, vals in raw_by_component.items()}

    rows = []
    for r in records:
        country = r["country"]
        pct_group = {comp: pct_group_by_component[comp].get(country) for comp in r["raw_values"]}
        relative_score = relative_score_from_pct_group(pct_group)
        relative_label = bucket_with_hysteresis(relative_score, bucket_thresholds, bucket_labels, None, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date),
            "asof": str(ref_date) if vintage_safe else None,
            "data_through": r["data_through"],
            "is_reserve_currency": r["is_reserve_currency"], "caveats": r["caveats"],
            # Fase 5 cycle-classifier input, not a scored component (see
            # module docstring above and cycle_classifier.py).
            "fx_depreciation_12m_pct": (None if r["fx_depreciation_12m"] is None
                                        else round(float(r["fx_depreciation_12m"]), 4)),
            "components": {
                k: {"raw_value": round_or_none(r["raw_values"].get(k)),
                    "score": r["components"][k], "weight": weights.get(k, 0),
                    "obs_date": r["obs_dates"].get(k),
                    "pct_own_history": round_or_none(r["pct_own_history"].get(k)),
                    "pct_group": round_or_none(pct_group.get(k))}
                for k in r["components"]
            },
            "missing_components": [k for k, v in r["components"].items() if v is None],
            "coverage_tier": r["tier"], "vintage_safe": vintage_safe,
        }
        rows.append((country, ref_date, ENGINE,
                    None if r["score"] is None else round(r["score"], 2), r["label"], r["tier"], r["conf"],
                    r["n_avail"], r["n_exp"], json.dumps(audit), now,
                    round_or_none(relative_score), relative_label))

    return pd.DataFrame(rows, columns=_COLUMNS)

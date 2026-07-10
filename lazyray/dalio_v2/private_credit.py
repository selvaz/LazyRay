# -*- coding: utf-8 -*-
"""
private_credit.py — Dalio v2 Engine 3: Private Credit Cycle.

Separates the private-credit cycle from sovereign solvency: a country can
have low public debt and a private credit bubble, or high public debt and a
weak private credit cycle — these must not be conflated. See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 2.

credit_gap uses BIS bis_credit_gap where available (~43/64 countries per the
2026-07-08 coverage audit); for the other ~21 it falls back to a linear-
detrend proxy on private_debt_gdp (IMF GDD, 64/64) — deviation of the latest
value from a 10y OLS trend, in the same pp-of-GDP units as the BIS gap, but
structurally noisier (no HP filter, no seasonal handling). Whenever the
proxy branch is used, coverage_tier is capped so this is never presented as
equivalent to the real BIS series (see the plan's coverage_tier discipline).

private_dsr has NO proxy: BIS is the only source, so it is simply missing
(None) for the ~32/64 countries without it, dropped from the weighted
average like any other missing component.

npl_ratio thresholds ([3, 6, 10]) are NOT from the source proposal (it gives
none for this indicator) — assumed IMF-typical stress benchmarks, flagged
here and in components_json's "assumed" thresholds are not otherwise marked
per-component; this docstring is the flag.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from market_data_hub.reader import read_macro_panel_ext

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.scoring import (
    bucket_with_hysteresis, confidence_for, coverage_tier, fresh_latest,
    git_short_sha, prev_label, round_or_none, score_threshold,
    suppress_insufficient, weighted_average,
)

ENGINE = "private_credit"

_IND = {
    "credit_gap_bis": "bis_credit_gap",
    "dsr": "bis_dsr_private",
    "private_debt": "private_debt_gdp",
    "npl": "npl_ratio",
    "real_growth": ["gdp_growth_weo", "real_gdp_growth"],
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at"]

_MIN_TREND_POINTS = 5


def _linear_detrend_gap(s: Optional[pd.DataFrame], window_years: int = 10) -> Optional[float]:
    """Proxy credit-to-GDP gap: latest value minus a linear (OLS) trend fit
    over the trailing `window_years`, in the series' own units (pp of GDP).
    A crude stand-in for the BIS one-sided HP-filter gap — noisier, no
    seasonal/cycle handling, but usable where BIS coverage is absent."""
    if s is None or s.empty:
        return None
    d = s.sort_values("date").copy()
    d["y"] = pd.to_datetime(d["date"]).dt.year
    d = d.dropna(subset=["value"]).tail(window_years)
    if len(d) < _MIN_TREND_POINTS:
        return None
    x = d["y"].values - d["y"].values.min()
    slope, intercept = np.polyfit(x, d["value"].values, 1)
    trend_latest = slope * x[-1] + intercept
    return float(d["value"].values[-1] - trend_latest)


def _yoy_pct_change(s: Optional[pd.DataFrame], max_gap_days: int = 550) -> Optional[float]:
    """YoY % change of the latest two annual observations. Returns None when
    the two observations are more than `max_gap_days` apart (~18 months): a
    gappy series would otherwise report a multi-year change against
    thresholds calibrated for 12 months."""
    if s is None or len(s) < 2:
        return None
    d = s.sort_values("date")
    prev, cur = d["value"].iloc[-2], d["value"].iloc[-1]
    gap = pd.Timestamp(d["date"].iloc[-1]) - pd.Timestamp(d["date"].iloc[-2])
    if pd.isna(prev) or pd.isna(cur) or prev == 0 or gap.days > max_gap_days:
        return None
    return float((cur / prev - 1.0) * 100.0)


def _own_history_percentile(s: Optional[pd.DataFrame], min_obs: int = 8) -> Optional[float]:
    """Percentile (0-100) of the latest observation within the series' own
    history: the share of observations <= the latest. Unlike a min-max range
    position, one outlier year cannot permanently rescale it — which is what
    the 75/90/95 DSR thresholds (proposal §12.3, true percentiles) assume."""
    if s is None or s.empty:
        return None
    v = s.sort_values("date")["value"].dropna()
    if len(v) < min_obs:
        return None
    return float((v <= v.iloc[-1]).mean() * 100.0)


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None,
           hub_db_path: Optional[str] = None) -> pd.DataFrame:
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("private_credit", {})
    th = cfg.get("thresholds", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels", ["low", "moderate", "elevated", "high", "bubble"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)
    max_age = settings.get("staleness_max_age_years", 4)

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

        # the private-debt series' latest print gates everything DERIVED from
        # that series (detrend-proxy gap, ratio growth): a series that stopped
        # updating years ago would otherwise keep producing numeric values
        # scored as the current condition
        s_debt = by_ind.get(_IND["private_debt"])
        latest_debt, debt_dt = fresh_latest(_latest(s_debt), ref_ts, max_age)

        credit_gap_bis, gap_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["credit_gap_bis"])), ref_ts, max_age)
        used_proxy = credit_gap_bis is None
        if used_proxy:
            credit_gap = _linear_detrend_gap(s_debt) if latest_debt is not None else None
            gap_dt = debt_dt if credit_gap is not None else None
        else:
            credit_gap = credit_gap_bis

        s_dsr = _first_avail(by_ind, _IND["dsr"])
        dsr_pct = _own_history_percentile(s_dsr)
        latest_dsr, dsr_dt = fresh_latest(_latest(s_dsr), ref_ts, max_age)
        if latest_dsr is None:              # latest DSR print itself is stale
            dsr_pct = None

        # Real credit growth ~ YoY % change of the credit/GDP ratio + real GDP
        # growth: ratio growth nets out nominal GDP (growth + inflation), so
        # adding real growth back recovers inflation-adjusted credit growth --
        # the quantity the [5, 8, 12] thresholds (proposal §12.3) are
        # calibrated for. The bare ratio change is neither real nor credit
        # growth (a boom matched by GDP scores 0; a GDP collapse reads as one).
        # Gated on the debt series' latest print: a stale 2017->2018 ratio
        # change must not combine with a fresh GDP print into a "current"
        # number.
        ratio_growth = _yoy_pct_change(s_debt) if latest_debt is not None else None
        real_gdp, growth_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["real_growth"])), ref_ts, max_age)
        real_credit_growth = (ratio_growth + real_gdp) \
            if ratio_growth is not None and real_gdp is not None else None
        npl, npl_dt = fresh_latest(_latest(_first_avail(by_ind, _IND["npl"])), ref_ts, max_age)

        raw_values = {
            "credit_gap": credit_gap, "private_dsr": dsr_pct,
            "real_credit_growth": real_credit_growth, "real_house_price_gap": None,
            "npl_ratio": npl,
        }
        obs_dates = {
            "credit_gap": gap_dt, "private_dsr": dsr_dt,
            "real_credit_growth": growth_dt, "real_house_price_gap": None,
            "npl_ratio": npl_dt,
        }
        components = {
            "credit_gap": None if credit_gap is None else
                score_threshold(credit_gap, *th.get("credit_gap", [2, 5, 10])),
            "private_dsr": None if dsr_pct is None else
                score_threshold(dsr_pct, *th.get("private_dsr_pct", [75, 90, 95])),
            "real_credit_growth": None if real_credit_growth is None else
                score_threshold(real_credit_growth, *th.get("real_credit_growth", [5, 8, 12])),
            "real_house_price_gap": None,   # not wired yet, always missing
            "npl_ratio": None if npl is None else
                score_threshold(npl, *th.get("npl_ratio", [3, 6, 10])),
        }
        score, n_avail, n_exp = weighted_average(components, weights)
        # a proxy credit_gap (no BIS series at all for this country) can never
        # count as 'full' coverage even if every other component is present --
        # the single most important input is structurally weaker here.
        tier = coverage_tier(n_avail, n_exp)
        if used_proxy and credit_gap is not None and tier == "full":
            tier = "proxy"
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date), "asof": None,
            "credit_gap_source": "proxy(private_debt_gdp linear detrend)" if used_proxy else "bis",
            "components": {
                k: {"raw_value": round_or_none(raw_values[k]),
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

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
from market_data_hub.config_loader import get_countries

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path
from lazyray.dalio_v2.scoring import (
    actual_cutoff, bucket_with_hysteresis, clip_to_cutoff, confidence_for,
    coverage_tier, fresh_first_avail, fresh_latest, git_short_sha,
    own_history_pct, percentile_group, prev_label, relative_score_from_pct_group,
    round_or_none, score_threshold, suppress_insufficient, weighted_average,
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
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

_MIN_TREND_POINTS = 5
_OWN_HISTORY_MIN_OBS = 15


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

    dev = {c["iso3"]: c.get("development", "EM") for c in get_countries()}

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

        # the private-debt series' latest print gates everything DERIVED from
        # that series (detrend-proxy gap, ratio growth): a series that stopped
        # updating years ago would otherwise keep producing numeric values
        # scored as the current condition. private_debt_gdp (IMF GDD, annual)
        # is a LEVEL read, so the whole series is clipped to the cutoff up
        # front (scoring.actual_cutoff()/clip_to_cutoff()) -- the proxy trend
        # helpers below (_linear_detrend_gap/_yoy_pct_change) then never see
        # a post-cutoff point either, not just the "latest" gate.
        s_debt = clip_to_cutoff(by_ind.get(_IND["private_debt"]), cutoff)
        latest_debt, debt_dt = fresh_latest(_latest(s_debt), ref_ts, max_age)

        # BIS credit_gap/DSR are quarterly ACTUALS with no forecast rows at
        # all (unlike WEO) -- deliberately NOT cutoff-gated, only ref_ts +
        # max_age, so a genuinely fresher BIS print is never discarded.
        s_gap_bis = _first_avail(by_ind, _IND["credit_gap_bis"])
        credit_gap_bis, gap_dt = fresh_latest(_latest(s_gap_bis), ref_ts, max_age)
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
        # fresh_first_avail (not _first_avail+fresh_latest): "real_growth" is
        # a 2-candidate fallback list (WEO then real) -- a stale WEO print
        # must not shadow a fresh real_gdp_growth one (Codex review, same
        # class of bug already guarded against in sovereign_solvency.py).
        real_gdp, growth_dt = fresh_first_avail(by_ind, _IND["real_growth"], ref_ts, max_age, cutoff)
        real_credit_growth = (ratio_growth + real_gdp) \
            if ratio_growth is not None and real_gdp is not None else None
        npl, npl_dt = fresh_latest(
            _latest(clip_to_cutoff(_first_avail(by_ind, _IND["npl"]), cutoff)), ref_ts, max_age)

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
        data_through = max((d for d in obs_dates.values() if d), default=None)

        # own-history percentile: only credit_gap (when the BIS series -- not
        # the proxy -- backs it) and npl_ratio map onto a direct single-
        # indicator series; private_dsr's raw_value is ALREADY an own-history
        # percentile (see _own_history_percentile above, a percentile of a
        # percentile is not meaningful); real_credit_growth/real_house_price_gap
        # are derived/never-wired, see module docstring.
        pct_own_history = {
            "credit_gap": (None if used_proxy else
                          own_history_pct(s_gap_bis, credit_gap, gap_dt, 1, _OWN_HISTORY_MIN_OBS)),
            "private_dsr": None,
            "real_credit_growth": None,
            "real_house_price_gap": None,
            "npl_ratio": own_history_pct(clip_to_cutoff(by_ind.get(_IND["npl"]), cutoff),
                                         npl, npl_dt, 1, _OWN_HISTORY_MIN_OBS),
        }

        for comp, v in raw_values.items():
            raw_by_component.setdefault(comp, {})[country] = v

        records.append(dict(
            country=country, score=score, label=label, tier=tier, conf=conf,
            n_avail=n_avail, n_exp=n_exp, raw_values=raw_values, obs_dates=obs_dates,
            components=components, pct_own_history=pct_own_history,
            data_through=data_through, used_proxy=used_proxy,
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
            "credit_gap_source": "proxy(private_debt_gdp linear detrend)" if r["used_proxy"] else "bis",
            "components": {
                k: {"raw_value": round_or_none(r["raw_values"][k]),
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

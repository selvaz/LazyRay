# -*- coding: utf-8 -*-
"""
funding_liquidity.py — Dalio v2 Engine 2: Funding Liquidity, split by data
branch (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.2.4/3.1.7): the old single
"ramo B" proxy mixed two nearly-disjoint country populations (30 countries
with only a 10Y yield, 18 with only short-term-debt/reserves, 14 with
neither) under one "easy/stress" scale as if they were comparable. They are
not, so this module now reports which branch produced a score:

  - "market"   (OECD-32ish): bond_yield_10y is fresh -- yield_change_12m_pp
    (existing), term_spread_pp (10Y minus bis_policy_rate: an inverted/
    flattening curve is the classic funding-stress signal, so this is scored
    orientation=-1, lower is worse), reer_change_12m_pct (12-month REER
    move; ASSUMED to score on magnitude alone -- see module note below).
  - "external" (EM-20ish): short_term_debt_reserves is fresh -- the same
    series as external_constraint.py, plus debt_service_exports (existing
    indicator; thresholds REUSED from external_constraint.py's own config,
    not duplicated -- see module docstring there).
  - A country with BOTH keeps "market" (recorded in the audit trail).
  - A country with NEITHER gets branch "none": score/label None,
    coverage_tier "no_data" (suppress_insufficient() treats it exactly like
    "insufficient"; the cycle classifier reads it as "unclassified, not
    None-by-accident" -- see cycle_classifier.py).

reer_change_12m_pct sign convention: currency appreciation tightens funding
conditions for an EM borrower with FX-denominated debt but EASES them for a
net exporter competing on price -- the direction that matters depends on the
country's own balance sheet, which this engine does not model. Per the WP2
spec, when unsure the magnitude alone (abs % change) is scored: a big REER
swing either way is treated as a funding-conditions shock.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import pandas as pd
from market_data_hub.config_loader import get_countries

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path
from lazyray.dalio_v2.scoring import (
    actual_cutoff, bucket_with_hysteresis, clip_to_cutoff, confidence_for,
    fresh_latest, git_short_sha, own_history_pct, percentile_group,
    prev_label, relative_score_from_pct_group, round_or_none, score_threshold,
    suppress_insufficient, weighted_average,
)

ENGINE = "funding_liquidity"

_IND = {
    "short_term_debt_reserves": "short_term_debt_reserves",
    "debt_service_exports": "debt_service_exports",
    "bond_yield_10y": "bond_yield_10y",
    "bis_policy_rate": "bis_policy_rate",
    "reer": "reer_broad",
}

_MARKET_COMPONENTS = ("yield_change_12m_pp", "term_spread_pp", "reer_change_12m_pct")
_EXTERNAL_COMPONENTS = ("short_term_debt_reserves", "debt_service_exports")

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

_OWN_HISTORY_MIN_OBS = 15


def _yoy_level_change(s: Optional[pd.DataFrame]) -> Optional[float]:
    """Latest value minus the value closest to 12 months earlier (level
    change, e.g. percentage points for a yield series). The prior print must
    fall within 12-18 months of the latest: on a gappy series the "12m"
    change would otherwise silently span years, scored against thresholds
    calibrated for 12 months."""
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
    if pd.isna(prior_val):
        return None
    return float(latest_val - prior_val)


def _yoy_pct_change_abs(s: Optional[pd.DataFrame]) -> Optional[float]:
    """abs(% change) over the trailing 12 months (see module docstring for
    why magnitude, not signed direction). Same 12-18 month tolerance as
    _yoy_level_change."""
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
    return float(abs(latest_val - prior_val) / abs(prior_val) * 100.0)


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None,
           hub_db_path: Optional[str] = None) -> pd.DataFrame:
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("funding_liquidity", {})
    th = cfg.get("thresholds", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels", ["easy", "normal", "watch", "stress", "severe"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)
    max_age = settings.get("staleness_max_age_years", 4)
    # debt_service_exports thresholds REUSED from external_constraint.py, not
    # duplicated in settings.yaml (see module docstring / that module's own).
    ext_th = (settings.get("external_constraint") or {}).get("thresholds", {})

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

        # bond_yield_10y/bis_policy_rate/reer_broad are monthly actuals with
        # no forecast rows -- deliberately NOT cutoff-gated, only ref_ts +
        # max_age, same as external_constraint's REER reads.
        s_yield = by_ind.get(_IND["bond_yield_10y"])
        latest_yield, yield_dt = fresh_latest(_latest(s_yield), ref_ts, max_age)
        # short_term_debt_reserves is WDI annual (LEVEL, clipped to cutoff
        # BEFORE picking latest -- see scoring.actual_cutoff()/clip_to_cutoff()).
        s_strd = clip_to_cutoff(_first_avail(by_ind, _IND["short_term_debt_reserves"]), cutoff)
        short_term_reserves, strd_dt = fresh_latest(_latest(s_strd), ref_ts, max_age)

        # branch selection: market first (a country with both keeps market,
        # recorded in the audit trail -- see module docstring).
        if latest_yield is not None:
            branch = "market"
        elif short_term_reserves is not None:
            branch = "external"
        else:
            branch = "none"

        raw_values: "dict[str, Optional[float]]" = {}
        obs_dates: "dict[str, Optional[str]]" = {}
        pct_own_history: "dict[str, Optional[float]]" = {}
        active_components = ()

        if branch == "market":
            active_components = _MARKET_COMPONENTS
            yield_change = _yoy_level_change(s_yield) if latest_yield is not None else None
            s_policy = by_ind.get(_IND["bis_policy_rate"])
            latest_policy, policy_dt = fresh_latest(_latest(s_policy), ref_ts, max_age)
            term_spread = (latest_yield - latest_policy) \
                if latest_yield is not None and latest_policy is not None else None
            s_reer = by_ind.get(_IND["reer"])
            latest_reer, reer_dt = fresh_latest(_latest(s_reer), ref_ts, max_age)
            reer_change = _yoy_pct_change_abs(s_reer) if latest_reer is not None else None

            raw_values = {"yield_change_12m_pp": yield_change, "term_spread_pp": term_spread,
                         "reer_change_12m_pct": reer_change}
            obs_dates = {"yield_change_12m_pp": yield_dt,
                        "term_spread_pp": (policy_dt if term_spread is not None else None),
                        "reer_change_12m_pct": (reer_dt if reer_change is not None else None)}
            # all three are 12m-change/spread derivations of a single series
            # (or a 2-series subtraction) -- no direct "own history of the
            # change itself" series is assembled, see module docstring's
            # scope note; own-history percentile stays None for this branch.
            pct_own_history = {k: None for k in active_components}
            components = {
                "yield_change_12m_pp": None if yield_change is None else
                    score_threshold(yield_change, *th.get("yield_change_12m_pp", [1.0, 2.0, 3.5])),
                "term_spread_pp": None if term_spread is None else
                    score_threshold(term_spread, *th.get("term_spread_pp", [1.0, 0.0, -1.0]), orientation=-1),
                "reer_change_12m_pct": None if reer_change is None else
                    score_threshold(reer_change, *th.get("reer_change_12m_pct", [10.0, 15.0, 25.0])),
            }
        elif branch == "external":
            active_components = _EXTERNAL_COMPONENTS
            s_dse = clip_to_cutoff(_first_avail(by_ind, _IND["debt_service_exports"]), cutoff)
            debt_service_exports, dse_dt = fresh_latest(_latest(s_dse), ref_ts, max_age)

            raw_values = {"short_term_debt_reserves": short_term_reserves,
                         "debt_service_exports": debt_service_exports}
            obs_dates = {"short_term_debt_reserves": strd_dt, "debt_service_exports": dse_dt}
            pct_own_history = {
                "short_term_debt_reserves": own_history_pct(
                    s_strd, short_term_reserves, strd_dt, 1, _OWN_HISTORY_MIN_OBS),
                "debt_service_exports": own_history_pct(
                    s_dse, debt_service_exports, dse_dt, 1, _OWN_HISTORY_MIN_OBS),
            }
            components = {
                "short_term_debt_reserves": score_threshold(
                    short_term_reserves, *th.get("short_term_debt_reserves", [50, 100, 150])),
                "debt_service_exports": None if debt_service_exports is None else
                    score_threshold(debt_service_exports, *ext_th.get("debt_service_exports", [15, 25, 40])),
            }
        else:
            components = {}

        for comp, v in raw_values.items():
            raw_by_component.setdefault(comp, {})[country] = v

        if branch == "none":
            score, n_avail, n_exp = None, 0, 0
            tier = "no_data"
        else:
            score, n_avail, n_exp = weighted_average(components, weights)
            tier = "proxy" if n_avail > 0 else "insufficient"
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = None if branch == "none" else \
            bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)
        data_through = max((d for d in obs_dates.values() if d), default=None)

        records.append(dict(
            country=country, score=score, label=label, tier=tier, conf=conf,
            n_avail=n_avail, n_exp=n_exp, raw_values=raw_values, obs_dates=obs_dates,
            components=components, pct_own_history=pct_own_history, data_through=data_through,
            branch=branch, has_both=(latest_yield is not None and short_term_reserves is not None),
            active_components=active_components,
        ))

    if not records:
        return pd.DataFrame(columns=_COLUMNS)

    pct_group_by_component = {comp: percentile_group(vals, dev) for comp, vals in raw_by_component.items()}

    rows = []
    for r in records:
        country = r["country"]
        pct_group = {comp: pct_group_by_component[comp].get(country) for comp in r["active_components"]}
        relative_score = relative_score_from_pct_group(pct_group)
        relative_label = None if r["branch"] == "none" else \
            bucket_with_hysteresis(relative_score, bucket_thresholds, bucket_labels, None, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date),
            "asof": str(ref_date) if vintage_safe else None,
            "data_through": r["data_through"],
            "branch": r["branch"], "has_both_branches_data": r["has_both"],
            "scope": ("no funding data" if r["branch"] == "none" else
                     f"{r['branch']} branch (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.7)"),
            "components": {
                k: {"raw_value": round_or_none(r["raw_values"].get(k)),
                    "score": r["components"].get(k), "weight": weights.get(k, 0),
                    "obs_date": r["obs_dates"].get(k),
                    "pct_own_history": round_or_none(r["pct_own_history"].get(k)),
                    "pct_group": round_or_none(pct_group.get(k))}
                for k in r["active_components"]
            },
            "missing_components": [k for k in r["active_components"] if r["components"].get(k) is None],
            "coverage_tier": r["tier"], "vintage_safe": vintage_safe,
        }
        rows.append((country, ref_date, ENGINE,
                    None if r["score"] is None else round(r["score"], 2), r["label"], r["tier"], r["conf"],
                    r["n_avail"], r["n_exp"], json.dumps(audit), now,
                    round_or_none(relative_score), relative_label))

    return pd.DataFrame(rows, columns=_COLUMNS)

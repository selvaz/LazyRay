# -*- coding: utf-8 -*-
"""
funding_liquidity.py — Dalio v2 Engine 2: Funding Liquidity (reduced scope).

The source proposal calls this "the most important engine to add" and
specifies it around Gross Financing Needs, auction bid-to-cover/tail,
maturity-wall and foreign-holder-flow data. The 2026-07-09 data-feasibility
audit (docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md §2.2/Fase 4) found
that data free and at real quality ONLY for roughly 15-25 OECD/major
economies (IMF Fiscal Monitor's GFN is PDF-only for a curated ~30-country
set; auction bid-to-cover/tail has no free cross-country aggregator; OECD's
SDMX API covers maturity structure but only ~34-38 OECD members).

This module deliberately implements ONLY the plan's "ramo B" — the coarse
proxy available for the FULL panel:
  - short_term_debt_reserves: World Bank WDI short_term_debt_reserves (same
    series as external_constraint.py; NOTE 2026-07-09: this used to be a
    newly-added "World Bank IDS" indicator with an unverified source id —
    on review that was an unnecessary duplicate, short_term_debt_reserves
    already existed, already verified/live, and is literally the "short-term
    external debt/reserves" ratio the source proposal specifies, not the
    %-of-total-external-debt ratio the removed duplicate used) as a
    rollover-risk proxy — external debt only, no domestic-debt maturity
    wall, no auction data.
  - yield_change_12m_pp: 12-month change in the 10Y bond yield (FRED
    bond_yield_10y, ~32/64 coverage) as a funding-cost-shock proxy.

coverage_tier is ALWAYS capped at 'proxy' (or 'insufficient'), regardless of
how many of these 2 inputs are present — this is a structural limit of the
proxy tier itself, not a per-country data gap that more inputs could fix.
The OECD-based "ramo A" (real GFN/maturity/auction data for major
economies) is explicitly deferred, not built here — see the plan doc.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import pandas as pd
from market_data_hub.reader import read_macro_panel_ext

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest
from lazyray.dalio_v2.scoring import (
    bucket_with_hysteresis, confidence_for, fresh_latest, git_short_sha,
    prev_label, round_or_none, score_threshold, suppress_insufficient,
    weighted_average,
)

ENGINE = "funding_liquidity"

_IND = {
    "short_term_debt_reserves": "short_term_debt_reserves",
    "bond_yield_10y": "bond_yield_10y",
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at"]


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

        short_term_reserves, strd_dt = fresh_latest(
            _latest(_first_avail(by_ind, _IND["short_term_debt_reserves"])), ref_ts, max_age)
        yield_series = by_ind.get(_IND["bond_yield_10y"])
        # the 12m change is only as current as the LATEST print: a series
        # that stopped updating years ago still yields a numeric change
        # (its last two prints stay 12-18 months apart forever), so gate on
        # the latest observation's freshness, not just record its date
        latest_yield, yield_dt = fresh_latest(_latest(yield_series), ref_ts, max_age)
        yield_change = _yoy_level_change(yield_series) if latest_yield is not None else None

        raw_values = {
            "short_term_debt_reserves": None if pd.isna(short_term_reserves) else short_term_reserves,
            "yield_change_12m_pp": yield_change,
        }
        obs_dates = {"short_term_debt_reserves": strd_dt, "yield_change_12m_pp": yield_dt}
        components = {
            "short_term_debt_reserves": None if raw_values["short_term_debt_reserves"] is None else
                score_threshold(raw_values["short_term_debt_reserves"], *th.get("short_term_debt_reserves", [50, 100, 150])),
            "yield_change_12m_pp": None if yield_change is None else
                score_threshold(yield_change, *th.get("yield_change_12m_pp", [1.0, 2.0, 3.5])),
        }
        score, n_avail, n_exp = weighted_average(components, weights)
        # structural cap: this engine only ever implements the reduced-scope
        # proxy tier ("ramo B"), never real GFN/auction data -- see docstring.
        tier = "proxy" if n_avail > 0 else "insufficient"
        score = suppress_insufficient(score, tier)   # defensive; tier already implies score is None here
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date), "asof": None,
            "scope": "proxy tier only (ramo B) -- real GFN/auction/maturity-wall "
                    "data not wired, see module docstring",
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

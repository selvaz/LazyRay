# -*- coding: utf-8 -*-
"""
political_execution.py — Dalio v2 Engine 5: Political Execution.

Can the country make the fiscal/structural adjustment its debt situation
requires, without a political crisis? See
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 1.

Built entirely on the 5 WGI indicators already wired into macro_panel
(governance pillar) — no new connector needed. voice_accountability is
intentionally excluded (not in the source proposal's §9.3 formula).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import pandas as pd
from market_data_hub.config_loader import get_countries

from lazyray.config_loader import get_settings
from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path
from lazyray.dalio_v2.scoring import (
    actual_cutoff, bucket_with_hysteresis, confidence_for, coverage_tier,
    git_short_sha, own_history_pct, percentile_group, percentile_rank,
    prev_label, relative_score_from_pct_group, round_or_none,
    suppress_insufficient, weighted_average,
)

ENGINE = "political_execution"

_WGI = {
    "government_effectiveness": "wgi_government_effectiveness",
    "rule_of_law": "wgi_rule_of_law",
    "control_corruption": "wgi_control_corruption",
    "political_stability": "wgi_political_stability",
    "regulatory_quality": "wgi_regulatory_quality",
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

_OWN_HISTORY_MIN_OBS = 15


def compute(con: duckdb.DuckDBPyConnection, ref_date, cfg: Optional[dict] = None,
           hub_db_path: Optional[str] = None) -> pd.DataFrame:
    """Political Execution scores for every country with WGI coverage as of
    ref_date. Returns a DataFrame ready to write to engine_scores."""
    settings = get_settings().get("dalio_v2", {})
    cfg = cfg or settings.get("political_execution", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels",
                            ["strong", "adequate", "watch", "weak", "impaired"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)

    dev = {c["iso3"]: c.get("development", "EM") for c in get_countries()}

    ids = list(_WGI.values())
    ref_ts = pd.Timestamp(ref_date)
    vintage_safe = used_asof_path(ref_date)
    # WGI is annual (World Bank), no forecast rows -- but it is still a
    # LEVEL read (see scoring.actual_cutoff()) and this engine is one of the
    # 5 the "levels must come from actuals" fix applies to, so a row dated
    # after the cutoff is excluded on top of the existing max_age floor.
    level_cutoff = actual_cutoff(ref_date)
    panel = load_panel_ext(ref_date, hub_db_path)
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)
    panel = panel[panel["indicator_id"].isin(ids) & panel["value"].notna()]
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel[panel["date"] <= ref_ts]
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)

    latest = (panel.sort_values("date")
                    .groupby(["country_iso3", "indicator_id"]).tail(1))
    # staleness guard: a WGI print older than max_age before ref_date must
    # not enter the cross-country percentile as the country's current state
    max_age = settings.get("staleness_max_age_years", 4)
    stale_floor = pd.Timestamp(ref_date) - pd.Timedelta(days=max_age * 365.25)
    latest = latest[(latest["date"] >= stale_floor) & (latest["date"] <= level_cutoff)]
    if latest.empty:
        return pd.DataFrame(columns=_COLUMNS)
    wide = latest.pivot(index="country_iso3", columns="indicator_id", values="value")
    obs_wide = latest.pivot(index="country_iso3", columns="indicator_id", values="date")

    # cross-country percentile per indicator (100 = best governance), then
    # inverted so a HIGHER engine score = worse, consistent with the other
    # engines (Sovereign Solvency etc. are all "high score = high risk").
    risk = pd.DataFrame(index=wide.index)
    for key, ind in _WGI.items():
        risk[key] = 100.0 - percentile_rank(wide[ind]) if ind in wide.columns else float("nan")

    # own-country history, per WGI series: raw WGI is HIGHER = BETTER, the
    # opposite convention of every other engine's raw_value -- orientation=-1
    # flips it so pct_own_history/pct_group stay 'higher percentile = riskier'
    # like everywhere else (see scoring.own_history_pct's orientation kwarg).
    by_country_series = {
        country: {ind: g[["date", "value"]] for ind, g in panel[panel["country_iso3"] == country]
                  .groupby("indicator_id")}
        for country in risk.index
    }

    sha = git_short_sha()
    now = datetime.now(timezone.utc)
    raw_by_component: "dict[str, dict]" = {k: {} for k in _WGI}
    records = []
    for country in risk.index:
        components = {k: (None if pd.isna(risk.loc[country, k]) else float(risk.loc[country, k]))
                     for k in _WGI}
        raw_values = {
            k: (None if ind not in wide.columns or pd.isna(wide.loc[country, ind])
                else round(float(wide.loc[country, ind]), 4))
            for k, ind in _WGI.items()
        }
        obs_dates = {
            k: (None if ind not in obs_wide.columns or pd.isna(obs_wide.loc[country, ind])
                else str(pd.Timestamp(obs_wide.loc[country, ind]).date()))
            for k, ind in _WGI.items()
        }
        pct_own_history = {
            k: own_history_pct(by_country_series.get(country, {}).get(ind), raw_values[k],
                               obs_dates[k], -1, _OWN_HISTORY_MIN_OBS)
            for k, ind in _WGI.items()
        }
        # oriented -1 for the SAME reason as pct_own_history above: raw WGI
        # is higher-is-better, group percentile must read higher = riskier.
        for k in _WGI:
            v = raw_values[k]
            raw_by_component[k][country] = -v if v is not None else None

        score, n_avail, n_exp = weighted_average(components, weights)
        tier = coverage_tier(n_avail, n_exp)
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)
        data_through = max((d for d in obs_dates.values() if d), default=None)

        records.append(dict(country=country, score=score, label=label, tier=tier, conf=conf,
                            n_avail=n_avail, n_exp=n_exp, raw_values=raw_values,
                            obs_dates=obs_dates, components=components,
                            pct_own_history=pct_own_history, data_through=data_through))

    if not records:
        return pd.DataFrame(columns=_COLUMNS)

    pct_group_by_component = {k: percentile_group(vals, dev) for k, vals in raw_by_component.items()}

    rows = []
    for r in records:
        country = r["country"]
        pct_group = {k: pct_group_by_component[k].get(country) for k in _WGI}
        relative_score = relative_score_from_pct_group(pct_group)
        relative_label = bucket_with_hysteresis(relative_score, bucket_thresholds, bucket_labels, None, margin_pct)

        audit = {
            "model_version": sha, "ref_date": str(ref_date),
            "asof": str(ref_date) if vintage_safe else None,
            "data_through": r["data_through"],
            "components": {
                k: {"raw_value": r["raw_values"][k], "score": r["components"][k],
                    "weight": weights.get(k, 0), "obs_date": r["obs_dates"].get(k),
                    "pct_own_history": round_or_none(r["pct_own_history"].get(k)),
                    "pct_group": round_or_none(pct_group.get(k))}
                for k in _WGI
            },
            "missing_components": [k for k, v in r["components"].items() if v is None],
            "coverage_tier": r["tier"], "vintage_safe": vintage_safe,
        }
        rows.append((country, ref_date, ENGINE,
                    None if r["score"] is None else round(r["score"], 2), r["label"], r["tier"], r["conf"],
                    r["n_avail"], r["n_exp"], json.dumps(audit), now,
                    round_or_none(relative_score), relative_label))

    return pd.DataFrame(rows, columns=_COLUMNS)

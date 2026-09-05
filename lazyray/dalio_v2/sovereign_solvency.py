# -*- coding: utf-8 -*-
"""
sovereign_solvency.py — Dalio v2 Engine 1: Sovereign Solvency.

Can the state service its debt without default, extreme financial repression,
high inflation, persistent monetization, or politically destabilizing
austerity? See docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 1 for
the full design (7 components, r-g formula, income-group thresholds), and
docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.2.3/3.1.5-6/5 for WP2 (dual
comparability scale, the FX-constraint gate, the DSA fan-chart component).

Reads the panel through lazyray.dalio_v2.panel.load_panel_ext: live values
for a run dated today, point-in-time (hub vintage tables) for an earlier
ref_date — components_json.vintage_safe records which path was used. Level
components are clipped to the last completed year (actual_cutoff), never a
WEO projection; only the trend component looks at forecast years.

debt_trend_5y reuses dalio.py's _slope()/_first_avail()/_latest() so the two
systems' debt trajectories are defined identically (same window, same
forecast inclusion), not two subtly different formulas.

Bucket labels are DESCRIPTIVE, not evaluative (WP2/§3.1.6): low /
moderate / elevated / high / critical, not "strong"/"weak" -- a low
low-debt reading is not by itself a statement that the country is fiscally
"strong". `fx_constrained` is a separate, POST-assignment override (see
_apply_fx_gate below), not a sixth bucket.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import duckdb
import pandas as pd
from market_data_hub.config_loader import get_countries

from lazyray.config_loader import get_settings
from lazyray.dalio import _first_avail, _latest, _slope
from lazyray.dalio_v2 import dsa
from lazyray.dalio_v2.external_constraint import _reserve_currency_iso3s
from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path
from lazyray.dalio_v2.scoring import (
    actual_cutoff, bucket_with_hysteresis, clip_to_cutoff, confidence_for,
    coverage_tier, fresh_first_avail, fresh_latest, git_short_sha, notna,
    own_history_pct, percentile_group, prev_label, relative_score_from_pct_group,
    round_or_none, score_threshold, suppress_insufficient, weighted_average,
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
    "fx_debt_share": "fx_debt_share",
}

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

# Components that map cleanly onto ONE country's own single-indicator history
# (a direct read or a plain sign flip of it) get an own-history percentile;
# components derived from 2+ series or a rolling window (interest_revenue,
# r_minus_g, debt_trend_5y, debt_p_up_5y) don't -- reconstructing a full
# annual history of the DERIVED quantity is out of scope here, so they stay
# None (an audited omission, not a bug; see scoring.own_history_pct's "None
# when unavailable" contract).
_OWN_HISTORY_MIN_OBS = 15
_GATE_FX_DEBT_SHARE_THRESHOLD = 50.0
_GATE_INFLATION_THRESHOLD = 15.0
_DSA_START_YEAR = 2000
_DSA_THRESHOLDS = (0.5, 0.7, 0.85)   # ASSUMED (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.5)


def _annual_series(df: "Optional[pd.DataFrame]") -> dict:
    """{year: last value in that year} from a (date, value) frame, sorted so
    a duplicate year deterministically keeps its LATEST print."""
    if df is None or df.empty:
        return {}
    d = df.dropna(subset=["value"]).copy()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    return d.sort_values("date").groupby("year")["value"].last().to_dict()


def _annual_fallback_series(by_ind: dict, ids, cutoff) -> dict:
    """Fallback-list version of _annual_series: later ids in `ids` are
    applied first so an earlier (primary) id's year always wins when both
    cover it -- same "first candidate wins" contract as fresh_first_avail,
    just across the whole history instead of one latest point."""
    if isinstance(ids, str):
        ids = [ids]
    out: dict = {}
    for i in reversed(ids):
        out.update(_annual_series(clip_to_cutoff(by_ind.get(i), cutoff)))
    return out


def _r_effective_series(by_ind: dict, cutoff) -> dict:
    """implied_interest_rate's own annual history, with the spec's fallback
    (interest_on_debt_gdp / LAGGED public_debt_gdp) filling any year the
    derived indicator itself is missing for (panel.py only derives it where
    both its inputs already coincide on the same date)."""
    r = _annual_series(clip_to_cutoff(by_ind.get(_IND["r_effective"]), cutoff))
    interest = _annual_series(clip_to_cutoff(by_ind.get(_IND["interest_gdp"]), cutoff))
    debt = _annual_series(clip_to_cutoff(by_ind.get(_IND["debt_gdp"]), cutoff))
    for y, v in interest.items():
        if y in r:
            continue
        prev_debt = debt.get(y - 1)
        if prev_debt:
            r[y] = v / prev_debt * 100.0
    return r


def _nominal_growth_series(by_ind: dict, cutoff) -> dict:
    growth = _annual_fallback_series(by_ind, _IND["growth"], cutoff)
    infl = _annual_fallback_series(by_ind, _IND["inflation"], cutoff)
    return {y: ((1 + growth[y] / 100.0) * (1 + infl[y] / 100.0) - 1) * 100.0
           for y in growth if y in infl}


def _dsa_component(by_ind: dict, cutoff, last_actual_year_ceiling: int
                   ) -> "tuple[Optional[float], dict]":
    """(p_up, audit_dict). Anchors the simulation on the MOST RECENT year
    (at/before `last_actual_year_ceiling` = cutoff.year) where debt, r, g AND
    pb are ALL actuals -- not rigidly `last_actual_year_ceiling` itself: WEO's
    fiscal aggregates (debt, interest, primary balance) routinely lag its own
    growth/inflation estimate by a further year (live-observed: 2026-09-04,
    debt/interest/pb actuals stopped at 2024 while growth/inflation already
    had a 2025 estimate) -- requiring the ceiling year exactly would silently
    skip the DSA component for most of the panel even though 2024 is a
    perfectly good, fully-actual anchor. p_up is None unless a common year
    exists AND at least MIN_OBS paired annual (r-g, pb) years exist back to
    _DSA_START_YEAR -- see dsa.py for the simulation itself."""
    r_series = _r_effective_series(by_ind, cutoff)
    g_series = _nominal_growth_series(by_ind, cutoff)
    pb_series = _annual_series(clip_to_cutoff(by_ind.get(_IND["primary_balance_gdp"]), cutoff))
    d_series = _annual_series(clip_to_cutoff(by_ind.get(_IND["debt_gdp"]), cutoff))
    common_years = sorted(y for y in r_series
                          if y <= last_actual_year_ceiling and y in g_series
                          and y in pb_series and y in d_series)
    if not common_years:
        return None, {"skipped": "no year with debt/r/g/pb all actual"}
    last_actual_year = common_years[-1]
    rg_hist, pb_hist = dsa.build_annual_history(r_series, g_series, pb_series,
                                                last_actual_year, _DSA_START_YEAR)
    n_obs = len(rg_hist)
    if n_obs < dsa.MIN_OBS:
        return None, {"skipped": f"only {n_obs} paired annual obs, need {dsa.MIN_OBS}",
                      "last_actual_year": last_actual_year}
    r_last, g_last, pb_last = r_series[last_actual_year], g_series[last_actual_year], pb_series[last_actual_year]
    d_last = d_series[last_actual_year]
    d_paths = dsa.simulate(float(d_last), r_last, g_last, pb_last, rg_hist, pb_hist)
    summary = dsa.summarize(d_paths, float(d_last))
    if summary is None:
        return None, {"skipped": "simulation returned no paths"}
    return summary["p_up"], {
        "last_actual_year": last_actual_year, "n_obs_years": n_obs,
        "r_last": round(r_last, 4), "g_last": round(g_last, 4),
        "pb_last": round(pb_last, 4), "d_last": round(float(d_last), 4),
        "p10": round(summary["p10"], 4), "p50": round(summary["p50"], 4),
        "p90": round(summary["p90"], 4),
    }


def _apply_fx_gate(label: Optional[str], country: str, reserve_currencies: set,
                   fx_debt_share: Optional[float], inflation: Optional[float],
                   fx_debt_share_threshold: float = _GATE_FX_DEBT_SHARE_THRESHOLD,
                   inflation_threshold: float = _GATE_INFLATION_THRESHOLD,
                   ) -> "tuple[Optional[str], Optional[dict]]":
    """A `low` label overstates solvency for a country that does not
    issue a reserve currency AND carries either a majority-FX debt stock or
    high inflation (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.6): "low
    debt" is not the same claim as "fiscally strong" once a currency
    mismatch or inflation could force default/repression regardless of the
    debt LEVEL. Returns (possibly-overridden label, gate audit dict or
    None)."""
    if label != "low" or country in reserve_currencies:
        return label, None
    fx_trip = notna(fx_debt_share) and fx_debt_share > fx_debt_share_threshold
    infl_trip = notna(inflation) and inflation > inflation_threshold
    if not (fx_trip or infl_trip):
        return label, None
    reason = "+".join(r for r, hit in (("fx_debt_share", fx_trip), ("inflation", infl_trip)) if hit)
    return "fx_constrained", {
        "reason": reason,
        "fx_debt_share": round_or_none(fx_debt_share),
        "inflation": round_or_none(inflation),
    }


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
    gate_cfg = cfg.get("gate", {})
    fx_debt_share_threshold = gate_cfg.get("fx_debt_share_threshold", _GATE_FX_DEBT_SHARE_THRESHOLD)
    inflation_threshold = gate_cfg.get("inflation_threshold", _GATE_INFLATION_THRESHOLD)
    th = cfg.get("thresholds", {})
    weights = cfg.get("weights", {})
    bucket_thresholds = cfg.get("bucket_thresholds", [20, 40, 60, 80])
    bucket_labels = cfg.get("bucket_labels",
                            ["low", "moderate", "elevated", "high", "critical"])
    margin_pct = settings.get("hysteresis_margin_pct", 0.10)
    max_age = settings.get("staleness_max_age_years", 4)

    countries = get_countries()
    dev = {c["iso3"]: c.get("development", "EM") for c in countries}
    reserve_currencies = _reserve_currency_iso3s()

    panel = load_panel_ext(ref_date, hub_db_path)
    if panel.empty:
        return pd.DataFrame(columns=_COLUMNS)
    panel["date"] = pd.to_datetime(panel["date"])
    ref_ts = pd.Timestamp(ref_date)
    cutoff = actual_cutoff(ref_date)
    last_actual_year = cutoff.year
    vintage_safe = used_asof_path(ref_date)

    sha = git_short_sha()
    now = datetime.now(timezone.utc)
    records = []
    raw_by_component: "dict[str, dict]" = {}   # component -> {country: oriented value}

    for country, cdf_full in panel.groupby("country_iso3"):
        cdf = cdf_full[cdf_full["date"] <= ref_ts]
        if cdf.empty:
            continue
        by_ind = {i: g[["date", "value"]] for i, g in cdf.groupby("indicator_id")}

        # fresh_first_avail (not dalio._first_avail): a stale primary series
        # must not shadow a fresh fallback (gdp_growth_weo vs real_gdp_growth).
        # cutoff=cutoff on every one of these: they are all LEVEL ("current
        # condition") reads of WEO/FM/GDD annual series, which can carry a
        # forecast row dated in ref_date's own year (the debt_gdp 2026
        # projection that motivated this fix) -- see scoring.actual_cutoff().
        # debt_trend_5y below deliberately does NOT use this: it is the one
        # TREND component allowed to read the forecast-inclusive window.
        debt, debt_dt = fresh_first_avail(by_ind, _IND["debt_gdp"], ref_ts, max_age, cutoff)
        net_debt, net_debt_dt = fresh_first_avail(by_ind, _IND["net_debt_gdp"], ref_ts, max_age, cutoff)
        interest_gdp, interest_dt = fresh_first_avail(by_ind, _IND["interest_gdp"], ref_ts, max_age, cutoff)
        revenue_gdp, _ = fresh_first_avail(by_ind, _IND["revenue_gdp"], ref_ts, max_age, cutoff)
        primary_balance, primary_dt = fresh_first_avail(by_ind, _IND["primary_balance_gdp"], ref_ts, max_age, cutoff)
        growth, _ = fresh_first_avail(by_ind, _IND["growth"], ref_ts, max_age, cutoff)
        infl, _ = fresh_first_avail(by_ind, _IND["inflation"], ref_ts, max_age, cutoff)
        r_eff, r_eff_dt = fresh_first_avail(by_ind, _IND["r_effective"], ref_ts, max_age, cutoff)
        fx_debt_share, _ = fresh_latest(
            _latest(clip_to_cutoff(_first_avail(by_ind, _IND["fx_debt_share"]), cutoff)), ref_ts, max_age)

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

        debt_p_up_5y, dsa_audit = _dsa_component(by_ind, cutoff, last_actual_year)

        raw_values = {
            "debt_gdp": debt, "net_debt_gdp": net_debt, "interest_revenue": interest_revenue,
            "interest_gdp": interest_gdp, "primary_deficit_gdp": primary_deficit,
            "r_minus_g": r_minus_g, "debt_trend_5y": debt_trend,
            "debt_p_up_5y": debt_p_up_5y,
        }
        # observation date of each component's primary input (None for the
        # windowed trend and the DSA probability, which has no single obs
        # date); derived components inherit their numerator's date
        obs_dates = {
            "debt_gdp": debt_dt, "net_debt_gdp": net_debt_dt,
            "interest_revenue": interest_dt, "interest_gdp": interest_dt,
            "primary_deficit_gdp": primary_dt, "r_minus_g": r_eff_dt,
            "debt_trend_5y": None, "debt_p_up_5y": None,
        }
        components = {
            "debt_gdp": score_threshold(debt, *debt_th),
            "net_debt_gdp": score_threshold(net_debt, *net_debt_th),
            "interest_revenue": score_threshold(interest_revenue, *th.get("interest_revenue", [10, 15, 25])),
            "interest_gdp": score_threshold(interest_gdp, *th.get("interest_gdp", [3, 5, 7])),
            "primary_deficit_gdp": score_threshold(primary_deficit, *th.get("primary_deficit_gdp", [2, 4, 6])),
            "r_minus_g": score_threshold(r_minus_g, *th.get("r_minus_g", [1, 3, 5])),
            "debt_trend_5y": score_threshold(debt_trend, *th.get("debt_trend_5y", [0.7, 1.5, 3.0])),
            "debt_p_up_5y": score_threshold(debt_p_up_5y, *th.get("debt_p_up_5y", list(_DSA_THRESHOLDS))),
        }
        score, n_avail, n_exp = weighted_average(components, weights)
        tier = coverage_tier(n_avail, n_exp)
        score = suppress_insufficient(score, tier)
        conf = confidence_for(tier)
        prev = prev_label(con, country, ENGINE, ref_date)
        label = bucket_with_hysteresis(score, bucket_thresholds, bucket_labels, prev, margin_pct)
        label, gate = _apply_fx_gate(label, country, reserve_currencies, fx_debt_share, infl,
                                     fx_debt_share_threshold, inflation_threshold)
        data_through = max((d for d in obs_dates.values() if d), default=None)

        # own-history percentile: only for components with a direct (or
        # plain sign-flipped) single-indicator series -- see module docstring.
        pct_own_history = {
            "debt_gdp": own_history_pct(debt_hist, debt, debt_dt, 1, _OWN_HISTORY_MIN_OBS),
            "net_debt_gdp": own_history_pct(
                clip_to_cutoff(by_ind.get(_IND["net_debt_gdp"]), cutoff), net_debt, net_debt_dt, 1, _OWN_HISTORY_MIN_OBS),
            "interest_gdp": own_history_pct(
                clip_to_cutoff(by_ind.get(_IND["interest_gdp"]), cutoff), interest_gdp, interest_dt, 1, _OWN_HISTORY_MIN_OBS),
            "interest_revenue": None,     # derived from 2 series, see module docstring
            "primary_deficit_gdp": own_history_pct(
                clip_to_cutoff(by_ind.get(_IND["primary_balance_gdp"]), cutoff), primary_balance, primary_dt,
                -1, _OWN_HISTORY_MIN_OBS),
            "r_minus_g": None,            # derived from 3 series
            "debt_trend_5y": None,        # rolling-window derived, no single "own history" series
            "debt_p_up_5y": None,         # simulation output, no historical series of itself
        }

        for comp, v in raw_values.items():
            raw_by_component.setdefault(comp, {})[country] = v if notna(v) else None

        records.append(dict(
            country=country, score=score, label=label, tier=tier, conf=conf,
            n_avail=n_avail, n_exp=n_exp, raw_values=raw_values, obs_dates=obs_dates,
            components=components, pct_own_history=pct_own_history, gate=gate,
            data_through=data_through, growth=growth, r_eff=r_eff,
            debt_trend_actuals=debt_trend_actuals, dsa_audit=dsa_audit,
        ))

    if not records:
        return pd.DataFrame(columns=_COLUMNS)

    dev_by_country = dev
    pct_group_by_component = {
        comp: percentile_group(vals, dev_by_country) for comp, vals in raw_by_component.items()
    }

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
            "income_group": dev_by_country.get(country, "EM"),
            "debt_trend_actuals_5y": None if pd.isna(r["debt_trend_actuals"])
                else round(float(r["debt_trend_actuals"]), 4),
            "debt_trend_forecast_dependent": (
                None if pd.isna(r["raw_values"]["debt_trend_5y"]) or pd.isna(r["debt_trend_actuals"])
                else bool(abs(r["raw_values"]["debt_trend_5y"] - r["debt_trend_actuals"]) > 1.0)),
            "real_growth_pct": round(float(r["growth"]), 4) if notna(r["growth"]) else None,
            "r_effective_pct": round(float(r["r_eff"]), 4) if notna(r["r_eff"]) else None,
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
            "dsa": r["dsa_audit"],
            "gate": r["gate"],
        }
        rows.append((country, ref_date, ENGINE,
                    None if r["score"] is None else round(r["score"], 2), r["label"], r["tier"], r["conf"],
                    r["n_avail"], r["n_exp"], json.dumps(audit), now,
                    round_or_none(relative_score), relative_label))

    return pd.DataFrame(rows, columns=_COLUMNS)

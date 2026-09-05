# -*- coding: utf-8 -*-
"""
test_dalio_v2.py — Dalio v2 Phase 1: Sovereign Solvency + Political Execution.

Validates the shared scoring helpers in isolation, then a full pipeline run
(seed macro_panel on a simulated hub DB -> run_dalio_v2 -> engine_scores on
LazyRay's own DB) with hand-picked, clearly separated country profiles so
the expected score ordering is unambiguous (see
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 1 for the design this
exercises).

Two DBs are involved everywhere a pipeline runs: get_hub_conn() seeds
macro_panel on the simulated market-data-hub DB (input, read via
reader.read_macro_panel_ext inside each engine); get_lazyray_conn() reads/
writes engine_scores and friends on LazyRay's own DB (output). The tmp_db
fixture points MARKET_DATA_DB and LAZYRAY_DB at two separate temp files.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import record_vintage, upsert

from lazyray.dalio_v2 import scoring
from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn as get_lazyray_conn


# ---------------------------------------------------------------------------
# scoring.py unit tests
# ---------------------------------------------------------------------------

def test_score_threshold_interpolates_and_flips_orientation():
    assert scoring.score_threshold(50, 90, 110, 130) == 0.0          # below watch
    assert scoring.score_threshold(100, 90, 110, 130) == 25.0        # w..s interpolation
    assert scoring.score_threshold(110, 90, 110, 130) == 50.0        # exactly at stress
    assert scoring.score_threshold(120, 90, 110, 130) == 75.0        # s..c interpolation
    assert scoring.score_threshold(200, 90, 110, 130) == 100.0       # above critical
    assert scoring.score_threshold(None, 90, 110, 130) is None
    # orientation=-1: a LOW value is worse, so raw thresholds DESCEND
    # (watch is always the mildest cut point), e.g. reserves_months [4, 3, 2]
    assert scoring.score_threshold(5.0, 4, 3, 2, orientation=-1) == 0.0
    assert scoring.score_threshold(3.5, 4, 3, 2, orientation=-1) == 25.0
    assert scoring.score_threshold(3.0, 4, 3, 2, orientation=-1) == 50.0
    assert scoring.score_threshold(2.5, 4, 3, 2, orientation=-1) == 75.0
    assert scoring.score_threshold(1.0, 4, 3, 2, orientation=-1) == 100.0


def test_score_threshold_rejects_misordered_thresholds():
    # ascending thresholds under orientation=-1 used to silently degenerate
    # into a 0/100 cliff at `watch` (stress/critical ignored); now they raise
    import pytest
    with pytest.raises(ValueError):
        scoring.score_threshold(-8, 2, 4, 6, orientation=-1)
    with pytest.raises(ValueError):
        scoring.score_threshold(50, 130, 110, 90)      # descending with +1
    with pytest.raises(ValueError):
        scoring.score_threshold(50, 90, 90, 130)       # degenerate (equal)


def test_weighted_average_drops_missing_and_reports_coverage():
    components = {"a": 80.0, "b": None, "c": 40.0}
    weights = {"a": 1, "b": 1, "c": 1}
    score, n_avail, n_exp = scoring.weighted_average(components, weights)
    assert n_avail == 2 and n_exp == 3
    assert score == 60.0                          # (80+40)/2, b dropped entirely
    assert scoring.coverage_tier(n_avail, n_exp) == "proxy"    # 2/3 = 0.667 -> proxy
    assert scoring.coverage_tier(3, 3) == "full"
    assert scoring.coverage_tier(1, 3) == "insufficient"
    assert scoring.confidence_for("full") == "high"
    assert scoring.confidence_for("insufficient") == "low"


def test_weighted_average_excludes_zero_weight_components_from_coverage():
    # a present component whose weight key is missing/zero (e.g. a typo'd
    # settings.yaml key) contributes nothing to the score, so it must not
    # count as coverage either
    components = {"a": 80.0, "b": 40.0}
    score, n_avail, n_exp = scoring.weighted_average(components, {"a": 1})
    assert score == 80.0 and n_avail == 1 and n_exp == 2
    # all weights unknown -> no score AND no coverage (tier will be
    # insufficient, never the contradictory score=None/tier=full row)
    score, n_avail, n_exp = scoring.weighted_average(components, {})
    assert score is None and n_avail == 0 and n_exp == 2


def test_bucket_with_hysteresis_smooths_single_boundary_flutter():
    thresholds = [20, 40, 60, 80]
    labels = ["strong", "stable", "watch", "stressed", "critical"]
    # first-ever computation: plain threshold assignment, no prior label
    assert scoring.bucket_with_hysteresis(41, thresholds, labels, None) == "watch"
    # small move back across the same boundary should NOT flip immediately
    assert scoring.bucket_with_hysteresis(39, thresholds, labels, "watch") == "watch"
    # move that clears threshold + margin (36 = 40 - 10%*(60-20)) flips it
    assert scoring.bucket_with_hysteresis(35, thresholds, labels, "watch") == "stable"
    # a jump spanning more than one bucket always applies immediately
    assert scoring.bucket_with_hysteresis(95, thresholds, labels, "strong") == "critical"


def test_percentile_rank_orders_ascending():
    s = pd.Series({"a": -1.0, "b": 1.0, "c": 2.0})
    ranked = scoring.percentile_rank(s)
    assert ranked["c"] > ranked["b"] > ranked["a"]
    assert ranked["c"] == 100.0


def test_git_short_sha_never_raises():
    sha = scoring.git_short_sha()
    assert isinstance(sha, str) and sha


# ---------------------------------------------------------------------------
# WP2 deliverable A: percentile_own_history / percentile_group
# ---------------------------------------------------------------------------

def test_percentile_own_history_below_min_obs_is_none():
    history = pd.Series([10.0, 20.0, 30.0])   # only 3 points, min_obs defaults to 15
    assert scoring.percentile_own_history(history, 25.0) is None


def test_percentile_own_history_share_at_or_below_value():
    history = pd.Series(list(range(1, 21)))   # 1..20, 20 observations
    # 15 of 20 values are <= 15 -> 75th percentile
    assert scoring.percentile_own_history(history, 15.0, min_obs=15) == 75.0
    assert scoring.percentile_own_history(history, None, min_obs=15) is None


def test_percentile_group_ranks_within_income_group_only():
    values = {"USA": 10.0, "DEU": 90.0, "ARG": 50.0, "TUR": 80.0}
    groups = {"USA": "DM", "DEU": "DM", "ARG": "EM", "TUR": "EM"}
    out = scoring.percentile_group(values, groups)
    # percentile_rank() is pandas' pct rank (1/n .. n/n) -- with 2 members
    # per group the highest raw value ranks 100, the lowest ranks 50, never 0
    assert out["DEU"] == 100.0 and out["USA"] == 50.0   # ranked only against each other
    assert out["TUR"] == 100.0 and out["ARG"] == 50.0
    # a group of one (Frontier here) is meaningless -> None, not a fake 100
    values2 = {"USA": 10.0, "ZWE": 50.0}
    groups2 = {"USA": "DM", "ZWE": "Frontier"}
    out2 = scoring.percentile_group(values2, groups2)
    assert out2["ZWE"] is None


def test_own_history_pct_orientation_flip_and_date_filtering():
    df = pd.DataFrame({
        "date": pd.to_datetime([f"{y}-12-31" for y in range(2000, 2020)]),
        "value": [4.0] * 19 + [1.0],   # reserves_months: last obs (2019) is the LOW/worst one
    })
    # orientation=-1 (reserves_months: lower is worse): the worst (lowest)
    # value must rank at the TOP of the oriented percentile, not the bottom
    pct = scoring.own_history_pct(df, 1.0, "2019-12-31", orientation=-1, min_obs=15)
    assert pct == 100.0
    # missing obs_date/series/value all degrade to None, never raise
    assert scoring.own_history_pct(None, 1.0, "2019-12-31") is None
    assert scoring.own_history_pct(df, None, "2019-12-31") is None
    assert scoring.own_history_pct(df, 1.0, None) is None


def test_relative_score_from_pct_group_means_available_only():
    assert scoring.relative_score_from_pct_group({"a": 40.0, "b": 60.0, "c": None}) == 50.0
    assert scoring.relative_score_from_pct_group({"a": None}) is None


# ---------------------------------------------------------------------------
# WP2 deliverable B: fx_constrained gate + bucket_with_hysteresis override
# ---------------------------------------------------------------------------

def test_bucket_with_hysteresis_override_stands_in_and_never_returned():
    thresholds = [20, 40, 60, 80]
    labels = ["low", "moderate", "elevated", "high", "critical"]
    # prev_label is the override string itself (as read back from a DB row
    # written by a prior gated run) -- must be treated as bucket index 0
    # (low) for hysteresis, and NEVER returned verbatim.
    held = scoring.bucket_with_hysteresis(21, thresholds, labels, "fx_constrained")
    assert held in labels
    assert held == "low"   # small move stays inside the dead-band
    # a genuine large move still applies immediately even from an override
    jumped = scoring.bucket_with_hysteresis(95, thresholds, labels, "fx_constrained")
    assert jumped == "critical"


def test_sovereign_fx_gate_overrides_low_for_non_reserve_currency(tmp_db):
    from lazyray.dalio_v2.sovereign_solvency import _apply_fx_gate
    # non-reserve-currency, fx_debt_share over the threshold -> gated
    label, gate = _apply_fx_gate("low", "TUR", {"USA", "JPN"}, 88.0, 5.0, 50.0, 15.0)
    assert label == "fx_constrained"
    assert gate["reason"] == "fx_debt_share"
    # inflation-only trip
    label2, gate2 = _apply_fx_gate("low", "ARG", {"USA"}, 10.0, 30.0, 50.0, 15.0)
    assert label2 == "fx_constrained" and gate2["reason"] == "inflation"
    # reserve-currency issuer is exempt regardless of the inputs
    label3, gate3 = _apply_fx_gate("low", "USA", {"USA"}, 95.0, 40.0, 50.0, 15.0)
    assert label3 == "low" and gate3 is None
    # a non-low label is never touched by the gate
    label4, gate4 = _apply_fx_gate("moderate", "TUR", {"USA"}, 95.0, 40.0, 50.0, 15.0)
    assert label4 == "moderate" and gate4 is None


def test_sovereign_gate_applies_end_to_end_and_relabels_fx_constrained(tmp_db):
    # TUR: comfortably low debt (would plainly bucket 'low') but not a
    # reserve currency, with fx_debt_share > 50 -- must read as
    # fx_constrained, never 'low'.
    con = get_hub_conn()
    rows = [
        _row(_LEVEL_DATE, "TUR", "public_debt_gdp", 25.0),
        _row(_LEVEL_DATE, "TUR", "govt_net_debt_gdp", 20.0),
        _row(_LEVEL_DATE, "TUR", "interest_on_debt_gdp", 1.0),
        _row(_LEVEL_DATE, "TUR", "government_revenue_gdp", 30.0),
        _row(_LEVEL_DATE, "TUR", "primary_balance_gdp", 1.0),
        _row(_LEVEL_DATE, "TUR", "gdp_growth_weo", 3.0),
        _row(_LEVEL_DATE, "TUR", "inflation_avg_weo", 3.0),
        _row(_LEVEL_DATE, "TUR", "fx_debt_usd", 80.0),
        _row(_LEVEL_DATE, "TUR", "ext_debt_nonres_usd", 100.0),   # fx_debt_share = 80%
    ]
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT label, components_json FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 = 'TUR'").fetchone()
    con.close()
    label, components_json = row
    audit = json.loads(components_json)
    assert label == "fx_constrained"
    assert audit["gate"]["reason"] == "fx_debt_share"
    assert audit["gate"]["fx_debt_share"] > 50


def test_suppress_insufficient_nulls_the_score():
    # a lone "safe" component must not read as a confident 0/strong when
    # coverage is too thin to trust it
    assert scoring.suppress_insufficient(0.0, "insufficient") is None
    assert scoring.suppress_insufficient(0.0, "proxy") == 0.0
    assert scoring.suppress_insufficient(55.2, "full") == 55.2


# ---------------------------------------------------------------------------
# Full pipeline: seed (hub) -> run_dalio_v2 -> engine_scores (LazyRay's own)
# ---------------------------------------------------------------------------

_DEBT_TRAJECTORY = {           # public_debt_gdp, 2023..2027 (2027 = WEO forecast)
    "USA": [50, 50, 50, 50, 50],           # flat, comfortable
    "SGP": [170, 170, 170, 171, 171],      # high gross debt, ~flat (the "SGP paradox")
    "ARG": [100, 110, 125, 140, 150],      # rising fast
}

_LATEST = {                    # every other component, single snapshot at ref_date
    "USA": dict(govt_net_debt_gdp=50, interest_on_debt_gdp=1.0, government_revenue_gdp=40,
               primary_balance_gdp=2.0, gdp_growth_weo=3.0, inflation_avg_weo=2.0,
               wgi_government_effectiveness=1.0, wgi_rule_of_law=1.0, wgi_control_corruption=1.0,
               wgi_political_stability=1.0, wgi_regulatory_quality=1.0),
    "SGP": dict(govt_net_debt_gdp=20, interest_on_debt_gdp=2.0, government_revenue_gdp=25,
               primary_balance_gdp=2.0, gdp_growth_weo=2.5, inflation_avg_weo=1.5,
               wgi_government_effectiveness=2.0, wgi_rule_of_law=2.0, wgi_control_corruption=2.0,
               wgi_political_stability=2.0, wgi_regulatory_quality=2.0),
    "ARG": dict(govt_net_debt_gdp=150, interest_on_debt_gdp=10.0, government_revenue_gdp=15,
               primary_balance_gdp=-8.0, gdp_growth_weo=-2.0, inflation_avg_weo=5.0,
               wgi_government_effectiveness=-1.0, wgi_rule_of_law=-1.0, wgi_control_corruption=-1.0,
               wgi_political_stability=-1.0, wgi_regulatory_quality=-1.0),
}

_ORIENT = {
    "govt_net_debt_gdp": -1, "interest_on_debt_gdp": -1, "government_revenue_gdp": 0,
    "primary_balance_gdp": 1, "gdp_growth_weo": 1, "inflation_avg_weo": -1,
    "wgi_government_effectiveness": 1, "wgi_rule_of_law": 1, "wgi_control_corruption": 1,
    "wgi_political_stability": 1, "wgi_regulatory_quality": 1,
}
_WGI_IDS = {"wgi_government_effectiveness", "wgi_rule_of_law", "wgi_control_corruption",
           "wgi_political_stability", "wgi_regulatory_quality"}


def _row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "governance" if ind in _WGI_IDS else "sovereign",
            "orientation": _ORIENT.get(ind, 0), "source": "test", "provider_dataset": "X",
            "provider_code": "Y", "unit": "pct", "frequency": "A"}


# _LATEST fields are all WEO/FM annual LEVEL indicators, so they must sit at
# or before actual_cutoff(ref_date) = Dec 31 of (ref_year - 1) to survive the
# new cutoff-gated freshness check (scoring.actual_cutoff) -- ref_year below
# is always 2026, so 2025-12-31. _DEBT_TRAJECTORY is left untouched: it feeds
# BOTH the level read (also cutoff-gated, so effectively read at its 2025
# point) AND the forecast-inclusive debt_trend_5y TREND component, which is
# explicitly exempt from the cutoff.
_LEVEL_DATE = dt.date(2025, 12, 31)


def _seed(con):
    rows = []
    for iso3, traj in _DEBT_TRAJECTORY.items():
        for y, v in zip(range(2023, 2028), traj):
            rows.append(_row(dt.date(y, 12, 31), iso3, "public_debt_gdp", v))
    for iso3, vals in _LATEST.items():
        for ind, v in vals.items():
            rows.append(_row(_LEVEL_DATE, iso3, ind, v))
    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_sovereign_solvency_and_political_execution(tmp_db):
    con = get_hub_conn()
    _seed(con)
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["sovereign_solvency", "political_execution"],
                          ref_date=dt.date(2026, 12, 31))
    assert summary["sovereign_solvency"] == 3 and summary["political_execution"] == 3
    assert "cycle_classifier" in summary   # Fase 5 always runs alongside the engines

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, engine, score, label, coverage_tier, confidence, "
        "components_json FROM engine_scores ORDER BY engine, country_iso3").fetch_df()
    con.close()

    ss = scores[scores.engine == "sovereign_solvency"].set_index("country_iso3")
    pe = scores[scores.engine == "political_execution"].set_index("country_iso3")

    # every component was seeded for all 3 countries -> full coverage throughout
    assert (scores["coverage_tier"] == "full").all()
    assert (scores["confidence"] == "high").all()

    # Sovereign Solvency: USA flat/low debt (safest) < SGP high-but-flat gross
    # debt / low net debt (the "Singapore paradox") < ARG rising debt + double
    # digit inflation + primary deficit (clearly worst)
    assert ss.loc["USA", "score"] < ss.loc["SGP", "score"] < ss.loc["ARG", "score"]

    # Political Execution: WGI ordering is consistent across all 5 dimensions
    # (SGP > USA > ARG on every one), so risk score must follow the same order
    assert pe.loc["SGP", "score"] < pe.loc["USA", "score"] < pe.loc["ARG", "score"]
    assert pe.loc["SGP", "score"] == 0.0    # best on every dimension -> zero risk

    # audit trail: components_json is valid JSON with the documented schema
    audit = json.loads(ss.loc["ARG", "components_json"])
    assert audit["coverage_tier"] == "full"
    assert audit["vintage_safe"] is False
    # debt_p_up_5y (DSA) is the one component this seed can never populate --
    # it needs >= 12 annual (r, g, pb) observations back to 2000, and _seed()
    # only carries a handful of years; still 7/8 = 87.5% -> stays 'full'.
    assert set(audit["missing_components"]) == {"debt_p_up_5y"}
    assert "model_version" in audit and audit["model_version"]


def test_sparse_coverage_suppresses_the_score_instead_of_faking_zero(tmp_db):
    # MEX gets exactly ONE of the 8 Sovereign Solvency inputs (a comfortably
    # "safe" debt/GDP level). Before suppress_insufficient() this produced a
    # confident-looking 0.0/"low" row -- misleading, since 7 of 8
    # inputs are simply missing, not actually safe. It must now read as no
    # score.
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(_LEVEL_DATE, "MEX", "public_debt_gdp", 50.0),
    ]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT score, label, coverage_tier, n_components, n_expected FROM engine_scores "
        "WHERE engine = 'sovereign_solvency' AND country_iso3 = 'MEX'").fetchone()
    con.close()

    score, label, tier, n_comp, n_exp = row
    assert tier == "insufficient"
    assert n_comp == 1 and n_exp == 8
    assert score is None
    assert label is None


def test_missing_label_survives_as_real_null_not_literal_nan_string(tmp_db):
    # Regression: pandas' string-dtype inference silently turns a mixed
    # None/str "label" column's None entries into its own NA sentinel, which
    # itertuples() yields as a bare float('nan') -- DuckDB then wrote that
    # into the VARCHAR column as the literal text "nan", which survives
    # every downstream pd.isna(label) check (it's a normal string) and
    # leaked into the HTML report as visible "nan" text. Only reproduces
    # with a MIX of populated and missing labels in the same DataFrame (a
    # column of all-None doesn't trigger the buggy dtype inference) -- USA
    # gets full data (a real label), MEX gets none (label must stay None).
    con = get_hub_conn()
    rows = [_row(_LEVEL_DATE, "USA", ind, v) for ind, v in
           _LATEST["USA"].items()]
    rows += [_row(dt.date(y, 12, 31), "USA", "public_debt_gdp", v)
            for y, v in zip(range(2023, 2028), _DEBT_TRAJECTORY["USA"])]
    rows.append(_row(_LEVEL_DATE, "MEX", "public_debt_gdp", 50.0))
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    # .fetchall() (not .fetch_df()) so a real SQL NULL comes back as Python
    # None rather than being turned into a float NaN by the pandas bridge --
    # that pandas-side NaN is a separate, expected conversion; what this test
    # guards against is the literal 3-character STRING "nan" that the write
    # path used to produce instead of ever writing SQL NULL at all.
    con = get_lazyray_conn(read_only=True)
    rows = con.execute(
        "SELECT country_iso3, label FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 IN ('USA', 'MEX')").fetchall()
    con.close()
    labels = dict(rows)
    assert labels["USA"] == "low"
    assert labels["MEX"] is None    # real NULL, not the literal string "nan"


# ---------------------------------------------------------------------------
# Private Credit Cycle: BIS-covered country vs BIS-uncovered (proxy) country
# ---------------------------------------------------------------------------

def _pc_row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "debt_cycle", "orientation": -1,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "A"}


def _seed_private_credit(con):
    # private_debt_gdp (IMF GDD) and npl_ratio/gdp_growth_weo (WDI/WEO) are
    # LEVEL indicators, cutoff-gated to _LEVEL_DATE (2025-12-31, see _seed()
    # above); bis_credit_gap/bis_dsr_private are BIS quarterly actuals with
    # no forecast rows, deliberately exempt from the cutoff, so they stay at
    # ref_date's own year (2026-12-31).
    rows = []
    # USA: BIS-covered, calm profile
    rows.append(_pc_row(dt.date(2026, 12, 31), "USA", "bis_credit_gap", 1.0))
    dsr_usa = [10, 12, 13, 14, 15, 16, 15, 14, 15, 15]     # latest=15, min=10, max=16 -> pct=0.83? see below
    for y, v in zip(range(2017, 2027), dsr_usa):
        rows.append(_pc_row(dt.date(y, 12, 31), "USA", "bis_dsr_private", v))
    for y, v in zip((2024, 2025), (100, 101)):
        rows.append(_pc_row(dt.date(y, 12, 31), "USA", "private_debt_gdp", v))
    rows.append(_pc_row(_LEVEL_DATE, "USA", "npl_ratio", 2.0))
    # real credit growth = ratio change + real GDP growth (~1% + 2% = 3%, calm)
    rows.append(_pc_row(_LEVEL_DATE, "USA", "gdp_growth_weo", 2.0))

    # TUR: BIS-covered, textbook private credit boom
    rows.append(_pc_row(dt.date(2026, 12, 31), "TUR", "bis_credit_gap", 15.0))
    dsr_tur = [30, 32, 34, 36, 38, 40, 42, 44, 46, 50]     # rising to a fresh peak
    for y, v in zip(range(2017, 2027), dsr_tur):
        rows.append(_pc_row(dt.date(y, 12, 31), "TUR", "bis_dsr_private", v))
    for y, v in zip((2024, 2025), (100, 130)):
        rows.append(_pc_row(dt.date(y, 12, 31), "TUR", "private_debt_gdp", v))
    rows.append(_pc_row(_LEVEL_DATE, "TUR", "npl_ratio", 12.0))
    # ratio change +30% with real growth 4% -> unambiguous double-digit boom
    rows.append(_pc_row(_LEVEL_DATE, "TUR", "gdp_growth_weo", 4.0))

    # VNM: no BIS coverage at all (one of the 21 countries in the 2026-07
    # coverage audit) -> exercises the private_debt_gdp linear-detrend proxy
    debt_vnm = [60, 62, 64, 66, 68, 72, 78, 86, 96, 110]   # accelerating late
    for y, v in zip(range(2016, 2026), debt_vnm):
        rows.append(_pc_row(dt.date(y, 12, 31), "VNM", "private_debt_gdp", v))
    rows.append(_pc_row(_LEVEL_DATE, "VNM", "npl_ratio", 4.0))
    rows.append(_pc_row(_LEVEL_DATE, "VNM", "gdp_growth_weo", 6.0))

    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_private_credit_bis_vs_proxy(tmp_db):
    con = get_hub_conn()
    _seed_private_credit(con)
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["private_credit"], ref_date=dt.date(2026, 12, 31))
    assert summary["private_credit"] == 3
    assert "cycle_classifier" in summary

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, score, label, coverage_tier, components_json "
        "FROM engine_scores WHERE engine = 'private_credit'").fetch_df()
    con.close()
    pc = scores.set_index("country_iso3")

    # USA calm < TUR textbook boom (credit gap, DSR at a fresh peak, double
    # digit real credit growth, high NPLs all firing at once)
    assert pc.loc["USA", "score"] < pc.loc["TUR", "score"]
    assert pc.loc["TUR", "label"] == "bubble"

    # BIS-covered countries get full coverage; the BIS-blind country never
    # does, even though every other input it has is populated
    assert pc.loc["USA", "coverage_tier"] == "full"
    assert pc.loc["TUR", "coverage_tier"] == "full"
    assert pc.loc["VNM", "coverage_tier"] in ("proxy", "insufficient")

    usa_audit = json.loads(pc.loc["USA", "components_json"])
    vnm_audit = json.loads(pc.loc["VNM", "components_json"])
    assert usa_audit["credit_gap_source"] == "bis"
    assert vnm_audit["credit_gap_source"].startswith("proxy")
    # VNM has no BIS DSR at all -> that component must be reported missing,
    # not silently defaulted to a "safe" score
    assert "private_dsr" in vnm_audit["missing_components"]

    # real credit growth = ratio change + real GDP growth, NOT the bare
    # nominal change of the credit/GDP ratio (which nets out nominal GDP)
    assert usa_audit["components"]["real_credit_growth"]["raw_value"] == 3.0   # 1% + 2%
    assert json.loads(pc.loc["TUR", "components_json"])[
        "components"]["real_credit_growth"]["raw_value"] == 34.0              # 30% + 4%
    # every component carries its observation date in the audit trail
    # (npl_ratio is WDI annual, a LEVEL read gated to _LEVEL_DATE = 2025-12-31
    # by actual_cutoff(); bis_credit_gap/bis_dsr_private above stay at
    # ref_date's own 2026-12-31, exempt from the cutoff)
    assert usa_audit["components"]["npl_ratio"]["obs_date"] == "2025-12-31"


def test_own_history_percentile_is_outlier_immune():
    from lazyray.dalio_v2.private_credit import _own_history_percentile
    # one crisis spike (30) must not permanently rescale the component the
    # way a min-max range position does: latest 15 sits above 8/9 of the
    # history, so the percentile must read high, not (15-14)/(30-14) = 6%
    s = pd.DataFrame({
        "date": [dt.date(2016 + i, 12, 31) for i in range(9)],
        "value": [14, 14, 14, 14, 14, 14, 14, 30, 15],
    })
    pct = _own_history_percentile(s)
    assert pct is not None and pct > 85.0


def test_yoy_level_change_rejects_multi_year_gaps():
    from lazyray.dalio_v2.funding_liquidity import _yoy_level_change
    # prior observation ~29 months back: a "12m" change spanning years must
    # be treated as missing, not scored against 12-month thresholds
    s = pd.DataFrame({"date": [pd.Timestamp("2024-01-31"), pd.Timestamp("2026-06-30")],
                      "value": [2.0, 6.0]})
    assert _yoy_level_change(s) is None
    # a clean 12-month spacing still works
    s2 = pd.DataFrame({"date": [pd.Timestamp("2025-06-30"), pd.Timestamp("2026-06-30")],
                       "value": [2.0, 3.5]})
    assert _yoy_level_change(s2) == 1.5


def test_stale_observation_is_treated_as_missing(tmp_db):
    # MEX's only debt print is from 2019; at ref 2026 (age 7y > the 4y
    # staleness cap) it must not be scored as the current condition -- and
    # the audit trail must still record the observation date that was dropped
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(dt.date(2019, 12, 31), "MEX", "public_debt_gdp", 50.0),
    ]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    audit = json.loads(con.execute(
        "SELECT components_json FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 = 'MEX'").fetchone()[0])
    con.close()
    assert audit["components"]["debt_gdp"]["raw_value"] is None
    assert audit["components"]["debt_gdp"]["obs_date"] == "2019-12-31"
    assert "debt_gdp" in audit["missing_components"]


# ---------------------------------------------------------------------------
# External Currency Constraint: reserve-currency issuer vs FX-fragile EM
# ---------------------------------------------------------------------------

def _ec_row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "markets", "orientation": 0,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "A"}


def _seed_external_constraint(con):
    rows = []
    # USA: reserve currency (via _EXPLICIT_RESERVE_CURRENCY), calm external
    # position, has fx_debt_usd/ext_debt_nonres_usd -> fx_debt_share = 8%
    # (matches the real-world figure cited throughout the docs)
    usa = dict(current_account_gdp=-2.0, iip_net_position=-1000.0, gdp_current_usd=25000.0,
              short_term_debt_reserves=20.0, debt_service_exports=5.0,
              fx_debt_usd=8.0, ext_debt_nonres_usd=100.0, inflation_avg_weo=2.5,
              fx_reserves_months_imports=5.0)
    # every one of these is a WEO/WDI/IIP/IIPCC annual LEVEL indicator ->
    # cutoff-gated to _LEVEL_DATE (2025-12-31), see scoring.actual_cutoff().
    for ind, v in usa.items():
        rows.append(_ec_row(_LEVEL_DATE, "USA", ind, v))

    # TUR: not a reserve currency, textbook FX-fragile profile, and
    # deliberately WITHOUT fx_debt_usd/ext_debt_nonres_usd so fx_debt_share
    # stays missing -> exercises the coverage-tier cap even though every
    # other input is present
    tur = dict(current_account_gdp=-6.0, iip_net_position=-500.0, gdp_current_usd=1000.0,
              short_term_debt_reserves=180.0, debt_service_exports=35.0,
              inflation_avg_weo=60.0, fx_reserves_months_imports=2.0)
    for ind, v in tur.items():
        rows.append(_ec_row(_LEVEL_DATE, "TUR", ind, v))

    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_external_constraint_reserve_currency_vs_fragile_em(tmp_db):
    con = get_hub_conn()
    _seed_external_constraint(con)
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["external_constraint"], ref_date=dt.date(2026, 12, 31))
    assert summary["external_constraint"] == 2
    assert "cycle_classifier" in summary

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, score, label, coverage_tier, components_json "
        "FROM engine_scores WHERE engine = 'external_constraint'").fetch_df()
    con.close()
    ec = scores.set_index("country_iso3")

    assert ec.loc["USA", "score"] < ec.loc["TUR", "score"]
    assert ec.loc["TUR", "label"] in ("high", "severe")

    usa_audit = json.loads(ec.loc["USA", "components_json"])
    tur_audit = json.loads(ec.loc["TUR", "components_json"])
    assert usa_audit["is_reserve_currency"] is True
    assert usa_audit["caveats"]                       # discount caveat recorded
    assert tur_audit["is_reserve_currency"] is False
    assert tur_audit["caveats"] == []

    # USA has fx_debt_share (the highest-quality input) -> full coverage;
    # TUR is missing exactly that input -> capped to proxy even though every
    # other component is present
    assert ec.loc["USA", "coverage_tier"] == "full"
    assert ec.loc["TUR", "coverage_tier"] == "proxy"
    assert "fx_debt_share" in tur_audit["missing_components"]


def test_external_constraint_reserves_months_peer_percentile_is_not_inverted(tmp_db):
    # F3 regression (Codex review of PR #10): reserves_months is scored with
    # orientation=-1 (fewer months of import cover is worse), but the peer
    # percentile (raw_by_component -> percentile_group) fed it the raw,
    # un-flipped months figure. That put the country with the MOST reserves
    # (RUS, 16.8 months, in production) at pct_group=100 -- flagged as the
    # riskiest -- while a country with almost none (LUX, 0.06 months) landed
    # near the safe end. DEU here plays the thin-reserves country, FRA the
    # well-reserved one; both DM, so percentile_group ranks them together.
    rows = [_ec_row(_LEVEL_DATE, "DEU", "fx_reserves_months_imports", 0.5),
            _ec_row(_LEVEL_DATE, "FRA", "fx_reserves_months_imports", 10.0)]
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["external_constraint"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, components_json FROM engine_scores "
        "WHERE engine = 'external_constraint' AND country_iso3 IN ('DEU', 'FRA')").fetch_df()
    con.close()
    audit = {r.country_iso3: json.loads(r.components_json) for r in scores.itertuples()}

    deu_resm = audit["DEU"]["components"]["reserves_months"]
    fra_resm = audit["FRA"]["components"]["reserves_months"]
    assert deu_resm["raw_value"] == 0.5 and fra_resm["raw_value"] == 10.0
    # the thin-reserves country (DEU) must rank RISKIER (higher pct_group)
    # than the well-reserved one (FRA), not the other way around
    assert deu_resm["pct_group"] > fra_resm["pct_group"]
    assert deu_resm["pct_group"] == 100.0 and fra_resm["pct_group"] == 50.0
    # raw_values/audit trail must stay the TRUE, unflipped months-of-reserves
    # figure -- only the internal peer-percentile input is negated
    assert deu_resm["raw_value"] == 0.5


# ---------------------------------------------------------------------------
# Funding Liquidity: branch split (market / external / none), WP2 deliverable
# C -- never 'full' coverage in any branch, that's still a structural cap.
# ---------------------------------------------------------------------------

def _fl_row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "markets", "orientation": 0,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "M" if ind in ("bond_yield_10y", "bis_policy_rate", "reer_broad") else "A"}


def _seed_funding_liquidity(con):
    # short_term_debt_reserves (WDI, LEVEL) -> _LEVEL_DATE; bond_yield_10y/
    # bis_policy_rate/reer_broad (monthly, no forecast rows) stay exempt from
    # the cutoff. DEU also carries short_term_debt_reserves to exercise
    # has_both_branches_data -- it must still land on 'market' (branch
    # selection prefers market when both are fresh, see module docstring).
    rows = [
        _fl_row(_LEVEL_DATE, "DEU", "short_term_debt_reserves", 20.0),
        _fl_row(dt.date(2025, 12, 31), "DEU", "bond_yield_10y", 2.0),
        _fl_row(dt.date(2026, 12, 31), "DEU", "bond_yield_10y", 2.5),      # +50bp, mild
        _fl_row(dt.date(2025, 12, 31), "ITA", "bond_yield_10y", 4.0),
        _fl_row(dt.date(2026, 12, 31), "ITA", "bond_yield_10y", 8.0),      # +400bp, spread blowout
    ]
    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_funding_liquidity_market_branch(tmp_db):
    con = get_hub_conn()
    _seed_funding_liquidity(con)
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["funding_liquidity"], ref_date=dt.date(2026, 12, 31))
    assert summary["funding_liquidity"] == 2
    assert "cycle_classifier" in summary

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, score, label, coverage_tier, components_json "
        "FROM engine_scores WHERE engine = 'funding_liquidity'").fetch_df()
    con.close()
    fl = scores.set_index("country_iso3")

    assert fl.loc["DEU", "score"] < fl.loc["ITA", "score"]
    assert fl.loc["ITA", "label"] in ("stress", "severe")
    # this engine can never report 'full' in any branch -- structural cap,
    # not a per-country gap (see module docstring)
    assert fl.loc["DEU", "coverage_tier"] == "proxy"
    assert fl.loc["ITA", "coverage_tier"] == "proxy"

    deu_audit = json.loads(fl.loc["DEU", "components_json"])
    ita_audit = json.loads(fl.loc["ITA", "components_json"])
    assert deu_audit["branch"] == "market" and ita_audit["branch"] == "market"
    # DEU has short_term_debt_reserves too, but keeps 'market' regardless
    assert deu_audit["has_both_branches_data"] is True
    assert ita_audit["has_both_branches_data"] is False
    # term_spread_pp/reer_change_12m_pct were never seeded -> missing, but
    # yield_change_12m_pp alone is enough to score the branch
    assert "yield_change_12m_pp" not in ita_audit["missing_components"]
    assert {"term_spread_pp", "reer_change_12m_pct"} <= set(ita_audit["missing_components"])


def test_funding_liquidity_term_spread_peer_percentile_is_not_inverted(tmp_db):
    # F3 regression (Codex review of PR #10): term_spread_pp is scored with
    # orientation=-1 (a MORE inverted/negative curve is worse), but the peer
    # percentile (raw_by_component -> percentile_group) fed it the raw,
    # un-flipped spread. That put the country with the deeply inverted curve
    # (HUN, -0.49pp, in production) at pct_group=20 -- near the SAFE end --
    # while the widest positive spread (MEX, +2.95pp) landed at 100, the
    # riskiest. DEU here plays the inverted-curve country, ITA the normal
    # positive-curve one; both DM, so percentile_group ranks them together.
    rows = [
        _fl_row(dt.date(2026, 6, 30), "DEU", "bond_yield_10y", 1.0),
        _fl_row(dt.date(2026, 6, 30), "DEU", "bis_policy_rate", 3.0),   # term_spread = -2.0 (inverted)
        _fl_row(dt.date(2026, 6, 30), "ITA", "bond_yield_10y", 5.0),
        _fl_row(dt.date(2026, 6, 30), "ITA", "bis_policy_rate", 3.0),   # term_spread = +2.0 (normal)
    ]
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["funding_liquidity"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    scores = con.execute(
        "SELECT country_iso3, components_json FROM engine_scores "
        "WHERE engine = 'funding_liquidity' AND country_iso3 IN ('DEU', 'ITA')").fetch_df()
    con.close()
    audit = {r.country_iso3: json.loads(r.components_json) for r in scores.itertuples()}

    deu_spread = audit["DEU"]["components"]["term_spread_pp"]
    ita_spread = audit["ITA"]["components"]["term_spread_pp"]
    assert deu_spread["raw_value"] == -2.0 and ita_spread["raw_value"] == 2.0
    # the inverted curve (DEU) must rank RISKIER (higher pct_group) than the
    # normal positive curve (ITA), not the other way around
    assert deu_spread["pct_group"] > ita_spread["pct_group"]
    assert deu_spread["pct_group"] == 100.0 and ita_spread["pct_group"] == 50.0
    # raw_values/audit trail must stay the TRUE, unflipped spread -- only the
    # internal peer-percentile input is negated, never what the user sees
    assert deu_spread["raw_value"] == -2.0


def _seed_funding_liquidity_external(con):
    rows = [
        _fl_row(_LEVEL_DATE, "VNM", "short_term_debt_reserves", 120.0),
        _fl_row(_LEVEL_DATE, "VNM", "debt_service_exports", 30.0),
    ]
    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_funding_liquidity_external_branch(tmp_db):
    con = get_hub_conn()
    _seed_funding_liquidity_external(con)
    con.commit()
    con.close()

    run_dalio_v2(engines=["funding_liquidity"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT score, label, coverage_tier, components_json FROM engine_scores "
        "WHERE engine = 'funding_liquidity' AND country_iso3 = 'VNM'").fetchone()
    con.close()
    score, label, tier, components_json = row
    assert tier == "proxy"
    assert score is not None and label is not None
    audit = json.loads(components_json)
    assert audit["branch"] == "external"
    assert audit["missing_components"] == []


def test_funding_liquidity_no_data_branch(tmp_db):
    # a country with NEITHER a fresh 10Y yield NOR short_term_debt_reserves
    # (docs/DALIO_PROD_ASSESSMENT_2026-09.md's 14-country example) gets
    # coverage_tier='no_data' and a real NULL score/label, never a silent 0.
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(_LEVEL_DATE, "QAT", "public_debt_gdp", 40.0),   # unrelated indicator, just seeds the country
    ]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["funding_liquidity"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT score, label, coverage_tier, components_json FROM engine_scores "
        "WHERE engine = 'funding_liquidity' AND country_iso3 = 'QAT'").fetchone()
    con.close()
    score, label, tier, components_json = row
    assert tier == "no_data"
    assert score is None and label is None
    assert json.loads(components_json)["branch"] == "none"


def test_run_dalio_v2_rejects_unknown_engine(tmp_db):
    con = get_hub_conn()
    con.close()
    try:
        run_dalio_v2(engines=["not_a_real_engine"])
        assert False, "expected ValueError"
    except ValueError as e:
        assert "not_a_real_engine" in str(e)


def test_run_dalio_v2_empty_panel_returns_zero_counts(tmp_db):
    con = get_hub_conn()
    con.close()
    summary = run_dalio_v2(ref_date=dt.date(2026, 12, 31))
    # cycle_classifier: None, not 0 -- with zero countries no engine wrote
    # any row at all, so the "have all gate engines run for this ref_date"
    # guard skips the refresh entirely (see test_partial_engine_run_on_a_new_
    # ref_year_does_not_clobber_the_prior_years_classification for the guard
    # itself).
    assert summary == {"sovereign_solvency": 0, "political_execution": 0,
                       "private_credit": 0, "external_constraint": 0,
                       "funding_liquidity": 0, "cycle_classifier": None}


def test_rerun_drops_stale_countries_and_is_idempotent(tmp_db):
    # A country that produced a row in an earlier run and then drops out of
    # the panel must NOT survive the re-run as a stale row (old score, old
    # model_version) for the same (ref_date, engine).
    con = get_hub_conn()
    _seed(con)                                       # USA / SGP / ARG
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    con = get_hub_conn()
    con.execute("DELETE FROM macro_panel WHERE country_iso3 = 'ARG'")
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))
    assert summary["sovereign_solvency"] == 2

    con = get_lazyray_conn(read_only=True)
    left = [r[0] for r in con.execute(
        "SELECT country_iso3 FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "ORDER BY country_iso3").fetchall()]
    con.close()
    assert left == ["SGP", "USA"]                    # ARG's stale row is gone

    # and a plain re-run with unchanged data is idempotent
    assert run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))["sovereign_solvency"] == 2


def test_prev_label_skips_null_label_gaps(tmp_db):
    # hysteresis must survive a period of insufficient coverage: the last
    # NON-NULL label is the anchor, not the most recent (possibly NULL) row
    con = get_lazyray_conn()
    con.execute(
        "INSERT INTO engine_scores VALUES "
        "('USA', DATE '2024-12-31', 'sovereign_solvency', 41.0, 'watch', 'full', "
        " 'high', 7, 7, '{}', now(), NULL, NULL), "
        "('USA', DATE '2025-12-31', 'sovereign_solvency', NULL, NULL, 'insufficient', "
        " 'low', 1, 7, '{}', now(), NULL, NULL)")
    assert scoring.prev_label(con, "USA", "sovereign_solvency",
                              dt.date(2026, 12, 31)) == "watch"
    con.close()


def test_fx_overvaluation_ignores_post_ref_date_reer(tmp_db):
    # REER is actual monthly data (no forecasts): a historical run must not
    # anchor the overvaluation trend on observations after ref_date.
    # ref_date=2023-12-31 is in the past (< today), so this now exercises
    # load_panel_ext's asof branch -- record_vintage() makes the rows visible
    # to a point-in-time read from macro_panel_vintage, not just macro_panel.
    # The look-ahead protection under test is the ENGINE's own
    # `cdf = cdf_full[cdf_full["date"] <= ref_ts]` filter, which applies
    # identically on the asof path (see panel.py): the vintage_date chosen
    # here does not itself hide the 2024-2026 rows from the read, only the
    # engine's date<=ref_ts step does -- exactly as on the live path this
    # test exercised before.
    rows = []
    for y in range(2021, 2024):                      # flat REER through 2023
        for m in range(1, 13):
            rows.append(_row(dt.date(y, m, 28), "MEX", "reer_broad", 100.0))
    for y in range(2024, 2027):                      # huge post-ref_date spike
        for m in range(1, 13):
            rows.append(_row(dt.date(y, m, 28), "MEX", "reer_broad", 200.0))
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    record_vintage(con, "macro_panel", pd.DataFrame(rows), vintage_date=dt.date(2023, 12, 31))
    con.commit()
    con.close()

    run_dalio_v2(engines=["external_constraint"], ref_date=dt.date(2023, 12, 31))

    con = get_lazyray_conn(read_only=True)
    audit = json.loads(con.execute(
        "SELECT components_json FROM engine_scores WHERE engine = 'external_constraint' "
        "AND country_iso3 = 'MEX'").fetchone()[0])
    con.close()
    raw = audit["components"]["fx_overvaluation_pct"]["raw_value"]
    # flat series through ref_date -> ~0% deviation; with the look-ahead bug
    # the 2024-2026 spike dragged this to a large positive number
    assert raw is not None and abs(raw) < 1.0


def test_hysteresis_end_to_end_through_the_db(tmp_db):
    # 5 countries so the middle one lands exactly on a bucket boundary
    # (percentile 60 -> risk 40, the strong/adequate/watch cut): with a prior
    # 'adequate' label stored in engine_scores, the 40 must NOT flip to
    # 'watch' (needs >= boundary + margin = 44) -- the first test where
    # prev_label() actually returns a value instead of None
    # WGI (World Bank) is a LEVEL indicator, cutoff-gated to _LEVEL_DATE by
    # political_execution's new level_cutoff filter (see scoring.actual_cutoff).
    wgi = ["wgi_government_effectiveness", "wgi_rule_of_law", "wgi_control_corruption",
           "wgi_political_stability", "wgi_regulatory_quality"]
    rows = []
    for rank, iso in enumerate(["USA", "GBR", "DEU", "FRA", "JPN"], start=1):
        for ind in wgi:
            rows.append(_row(_LEVEL_DATE, iso, ind, float(rank)))
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    con = get_lazyray_conn()
    con.execute(
        "INSERT INTO engine_scores VALUES ('DEU', DATE '2025-12-31', "
        "'political_execution', 38.0, 'adequate', 'full', 'high', 5, 5, '{}', now(), NULL, NULL)")
    con.commit()
    con.close()

    run_dalio_v2(engines=["political_execution"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    got = dict(con.execute(
        "SELECT country_iso3, label FROM engine_scores "
        "WHERE engine = 'political_execution' AND ref_date = DATE '2026-12-31' "
        "AND country_iso3 IN ('DEU', 'FRA')").fetchall())
    con.close()
    assert got["DEU"] == "adequate"     # held by hysteresis at score 40
    assert got["FRA"] == "adequate"     # plain assignment (risk 20 -> idx 1... sanity anchor)


# ---------------------------------------------------------------------------
# Codex review follow-ups (PR #24): the staleness gate must also cover
# fallback lists and series-derived metrics, not only point reads
# ---------------------------------------------------------------------------

def test_stale_primary_does_not_shadow_fresh_fallback(tmp_db):
    # gdp_growth_weo exists but is ancient; real_gdp_growth is current. The
    # old behavior (_first_avail then freshness) dropped the component
    # entirely; the fresh fallback must be used instead.
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(_LEVEL_DATE, "USA", "public_debt_gdp", 50.0),
        _row(_LEVEL_DATE, "USA", "implied_interest_rate", 3.0),
        _row(_LEVEL_DATE, "USA", "inflation_avg_weo", 2.0),
        _row(dt.date(2015, 12, 31), "USA", "gdp_growth_weo", -5.0),   # stale primary
        _row(_LEVEL_DATE, "USA", "real_gdp_growth", 3.0),   # fresh fallback, within cutoff
    ]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    audit = json.loads(con.execute(
        "SELECT components_json FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 = 'USA'").fetchone()[0])
    con.close()
    rmg = audit["components"]["r_minus_g"]
    # r_minus_g present and built on the FRESH +3% growth (g_nom ~ 5.06%,
    # r-g ~ -2.06 -> below watch -> risk 0), not missing and not the stale -5%
    assert rmg["raw_value"] is not None and rmg["raw_value"] < 0
    assert rmg["score"] == 0.0


def test_stale_yield_falls_back_to_external_branch(tmp_db):
    # bond_yield_10y stopped printing in 2019 (its latest print itself is
    # stale at ref 2026, not just the 12m-change window) -- the engine must
    # not select 'market' on a stale defining indicator, and must fall back
    # to 'external' since short_term_debt_reserves IS fresh.
    rows = [_row(dt.date(2018, 6, 30), "MEX", "bond_yield_10y", 6.0),
            _row(dt.date(2019, 6, 30), "MEX", "bond_yield_10y", 9.0),
            _row(_LEVEL_DATE, "MEX", "short_term_debt_reserves", 40.0)]
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["funding_liquidity"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT score, coverage_tier, components_json FROM engine_scores "
        "WHERE engine = 'funding_liquidity' AND country_iso3 = 'MEX'").fetchone()
    con.close()
    score, tier, components_json = row
    audit = json.loads(components_json)
    assert audit["branch"] == "external"
    assert tier == "proxy"
    assert score is not None
    assert "yield_change_12m_pp" not in audit["components"]


def test_stale_private_debt_series_produces_no_derived_metrics(tmp_db):
    # private_debt_gdp stopped updating in 2018: neither the detrend-proxy
    # credit gap nor the ratio-growth leg of real_credit_growth may survive
    # as the 2026 condition, even with a fresh GDP growth print available
    rows = [_pc_row(dt.date(2009 + i, 12, 31), "VNM", "private_debt_gdp", 60.0 + i)
            for i in range(10)]                                   # ends 2018
    rows.append(_pc_row(_LEVEL_DATE, "VNM", "gdp_growth_weo", 6.0))
    rows.append(_pc_row(_LEVEL_DATE, "VNM", "npl_ratio", 4.0))
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio_v2(engines=["private_credit"], ref_date=dt.date(2026, 12, 31))

    con = get_lazyray_conn(read_only=True)
    audit = json.loads(con.execute(
        "SELECT components_json FROM engine_scores WHERE engine = 'private_credit' "
        "AND country_iso3 = 'VNM'").fetchone()[0])
    con.close()
    assert audit["components"]["credit_gap"]["raw_value"] is None
    assert audit["components"]["real_credit_growth"]["raw_value"] is None
    assert {"credit_gap", "real_credit_growth"} <= set(audit["missing_components"])

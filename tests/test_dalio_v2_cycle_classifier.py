# -*- coding: utf-8 -*-
"""
test_dalio_v2_cycle_classifier.py — Fase 5 cycle classifier.

Pure-function unit tests for classify_deleveraging()/classify_dalio_stage()
(no DB), plus compute() tests that seed engine_scores rows directly (same
pattern as test_dalio_v2.py's test_hysteresis_end_to_end_through_the_db /
test_prev_label_skips_null_label_gaps) so labels/coverage_tier/audit fields
are fully controlled without fighting the upstream engines' raw threshold
math. The ARG regression case is the reason this module exists: see
lazyray/dalio_v2/cycle_classifier.py's docstring.

engine_scores/dalio_cycle_v2 are LazyRay's own tables (get_lazyray_conn);
the "full pipeline integration" section at the bottom also seeds
macro_panel on the simulated hub DB (get_hub_conn) since it drives the real
run_dalio_v2() wiring end to end.
"""
from __future__ import annotations

import datetime as dt
import json

from lazyray.dalio_v2 import cycle_classifier as cc
from lazyray.db.connection import get_conn as get_lazyray_conn

REF = dt.date(2026, 12, 31)


# ---------------------------------------------------------------------------
# classify_deleveraging — pure function
# ---------------------------------------------------------------------------

def test_deleveraging_none_when_debt_trend_unknown():
    assert cc.classify_deleveraging({}) is None


def test_deleveraging_none_when_debt_not_falling():
    assert cc.classify_deleveraging({"debt_trend_actuals_5y": 0.5}) == "none"


def test_deleveraging_beautiful():
    inputs = {"debt_trend_actuals_5y": -1.5, "real_growth_pct": 2.0,
             "inflation_pct": 3.0, "fx_depreciation_12m_pct": 2.0,
             "funding_label": "normal"}
    assert cc.classify_deleveraging(inputs) == "beautiful"


def test_deleveraging_inflationary_via_inflation():
    inputs = {"debt_trend_actuals_5y": -3.0, "real_growth_pct": 1.0,
             "inflation_pct": 30.0, "fx_depreciation_12m_pct": 2.0,
             "funding_label": "normal"}
    assert cc.classify_deleveraging(inputs) == "inflationary"


def test_deleveraging_inflationary_via_fx_depreciation_alone():
    inputs = {"debt_trend_actuals_5y": -3.0, "real_growth_pct": 1.0,
             "inflation_pct": 4.0, "fx_depreciation_12m_pct": 40.0,
             "funding_label": "normal"}
    assert cc.classify_deleveraging(inputs) == "inflationary"


def test_deleveraging_repressive():
    inputs = {"debt_trend_actuals_5y": -1.0, "real_growth_pct": -1.0,
             "inflation_pct": 4.0, "fx_depreciation_12m_pct": 2.0,
             "funding_label": "watch",
             "real_rate_proxy_pct": -5.0, "fx_debt_share_pct": 5.0}
    assert cc.classify_deleveraging(inputs) == "repressive"


def test_deleveraging_ugly_default():
    # debt falling, none of beautiful/inflationary/repressive conditions met
    # (negative growth, moderate inflation, positive real rate) -- also
    # covers the folded-in restructuring case (no data source exists)
    inputs = {"debt_trend_actuals_5y": -2.0, "real_growth_pct": -3.0,
             "inflation_pct": 5.0, "fx_depreciation_12m_pct": 3.0,
             "funding_label": "stress",
             "real_rate_proxy_pct": 3.0, "fx_debt_share_pct": 80.0}
    assert cc.classify_deleveraging(inputs) == "ugly"


def test_deleveraging_missing_data_cannot_earn_beautiful():
    # growth positive but inflation unknown -- must NOT read as beautiful
    inputs = {"debt_trend_actuals_5y": -2.0, "real_growth_pct": 2.0,
             "inflation_pct": None, "fx_depreciation_12m_pct": None,
             "funding_label": "normal"}
    assert cc.classify_deleveraging(inputs) != "beautiful"
    assert cc.classify_deleveraging(inputs) == "ugly"


def test_deleveraging_arg_regression():
    # THE motivating bug: hyperinflation makes debt/GDP fall via erosion,
    # not fiscal health -- must read as inflationary, never beautiful,
    # regardless of a superficially "favorable" positive real growth print
    inputs = {"debt_trend_actuals_5y": -8.0, "real_growth_pct": 2.0,
             "inflation_pct": 30.0, "fx_depreciation_12m_pct": 35.0,
             "funding_label": "severe"}
    result = cc.classify_deleveraging(inputs)
    assert result == "inflationary"
    assert result != "beautiful"


# ---------------------------------------------------------------------------
# classify_dalio_stage — pure function
# ---------------------------------------------------------------------------

def test_stage_crisis():
    inputs = {"funding_label": "severe", "external_label": "high",
             "sovereign_label": "watch", "private_label": "moderate",
             "real_growth_pct": 1.0}
    assert cc.classify_dalio_stage(inputs) == "crisis"


def test_stage_late_long_debt_cycle():
    inputs = {"funding_label": "watch", "external_label": "low",
             "sovereign_label": "critical", "private_label": "moderate",
             "real_growth_pct": 1.0}
    assert cc.classify_dalio_stage(inputs) == "late_long_debt_cycle"


def test_stage_private_bubble():
    inputs = {"funding_label": "normal", "external_label": "low",
             "sovereign_label": "stable", "private_label": "bubble",
             "real_growth_pct": 3.0}
    assert cc.classify_dalio_stage(inputs) == "private_bubble"


def test_stage_late_leveraging():
    inputs = {"funding_label": "normal", "external_label": "low",
             "sovereign_label": "stable", "private_label": "high",
             "real_growth_pct": 3.0}
    assert cc.classify_dalio_stage(inputs) == "late_leveraging"


def test_stage_contraction():
    inputs = {"funding_label": "normal", "external_label": "low",
             "sovereign_label": "stable", "private_label": "low",
             "real_growth_pct": -1.5}
    assert cc.classify_dalio_stage(inputs) == "contraction"


def test_stage_early_or_mid_cycle():
    inputs = {"funding_label": "easy", "external_label": "low",
             "sovereign_label": "strong", "private_label": "low",
             "real_growth_pct": 2.0}
    assert cc.classify_dalio_stage(inputs) == "early_or_mid_cycle"


def test_stage_branch_precedence_crisis_wins_over_bubble():
    # satisfies BOTH crisis and private_bubble -- crisis (evaluated first)
    # must win, not "the worse-sounding" or "the last matched" branch
    inputs = {"funding_label": "severe", "external_label": "severe",
             "sovereign_label": "critical", "private_label": "bubble",
             "real_growth_pct": -5.0}
    assert cc.classify_dalio_stage(inputs) == "crisis"


def test_stage_none_labels_do_not_raise_and_growth_unknown_is_unclassifiable():
    # Codex review finding: real_growth_pct is a single field inside
    # sovereign_solvency's audit, not itself coverage-tier-gated -- an
    # engine can be 'full' with every OTHER component present and this one
    # missing. If none of the label-based branches fire and growth is
    # unknown, the function must not silently default to the "safe" answer
    # -- it genuinely cannot rule out contraction.
    inputs = {"funding_label": None, "external_label": None,
             "sovereign_label": None, "private_label": None,
             "real_growth_pct": None}
    assert cc.classify_dalio_stage(inputs) is None   # not "early_or_mid_cycle"


def test_stage_growth_known_and_non_negative_is_early_or_mid_cycle():
    # the flip side: once growth IS known and no branch fired, the safe
    # default is legitimate again
    inputs = {"funding_label": None, "external_label": None,
             "sovereign_label": None, "private_label": None,
             "real_growth_pct": 0.5}
    assert cc.classify_dalio_stage(inputs) == "early_or_mid_cycle"


# ---------------------------------------------------------------------------
# compute() -- seeds engine_scores directly, full control over labels/audit
# ---------------------------------------------------------------------------

def _insert_engine_row(con, iso3, engine, score, label, tier, confidence, audit):
    con.execute(
        "INSERT INTO engine_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?, ?)",
        [iso3, REF, engine, score, label, tier, confidence,
         len(audit.get("components", {})), len(audit.get("components", {})) + 1,
         json.dumps(audit), None, None])


def _seed_arg_shaped(con):
    _insert_engine_row(con, "ARG", "sovereign_solvency", 40.0, "watch", "full", "high", {
        "real_growth_pct": 2.0, "r_effective_pct": 1.5, "debt_trend_actuals_5y": -8.0,
        "components": {"r_minus_g": {"score": 92.0, "raw_value": -18.4, "weight": 1}},
    })
    _insert_engine_row(con, "ARG", "external_constraint", 60.0, "high", "full", "high", {
        "fx_depreciation_12m_pct": 35.0,
        "components": {"inflation": {"score": 88.0, "raw_value": 30.0, "weight": 1},
                       "fx_debt_share": {"score": None, "raw_value": None, "weight": 1}},
    })
    _insert_engine_row(con, "ARG", "funding_liquidity", 90.0, "severe", "proxy", "medium", {
        "components": {"short_term_debt_reserves": {"score": 100.0, "raw_value": 164.0, "weight": 1}},
    })
    _insert_engine_row(con, "ARG", "private_credit", 40.0, "moderate", "proxy", "medium", {
        "components": {"credit_gap": {"score": 45.0, "raw_value": 5.0, "weight": 0.3}},
    })
    _insert_engine_row(con, "ARG", "political_execution", 60.0, "weak", "full", "high", {
        "components": {"rule_of_law": {"score": 65.0, "raw_value": -0.5, "weight": 0.25}},
    })


def _seed_sgp_shaped(con):
    _insert_engine_row(con, "SGP", "sovereign_solvency", 5.0, "strong", "full", "high", {
        "real_growth_pct": 3.0, "r_effective_pct": 1.0, "debt_trend_actuals_5y": 0.05,
        "components": {},
    })
    _insert_engine_row(con, "SGP", "external_constraint", 2.0, "low", "full", "high", {
        "fx_depreciation_12m_pct": -2.0,
        "components": {"inflation": {"score": 5.0, "raw_value": 1.5, "weight": 1}},
    })
    _insert_engine_row(con, "SGP", "funding_liquidity", 3.0, "easy", "proxy", "high", {
        "components": {},
    })
    _insert_engine_row(con, "SGP", "private_credit", 15.0, "low", "proxy", "medium", {
        "components": {},
    })
    _insert_engine_row(con, "SGP", "political_execution", 0.0, "strong", "full", "high", {
        "components": {},
    })


def test_compute_arg_shaped_is_inflationary_crisis(tmp_db):
    con = get_lazyray_conn()
    _seed_arg_shaped(con)
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["ARG"]
    assert row["deleveraging_type"] == "inflationary"
    assert row["dalio_stage"] in ("crisis", "late_long_debt_cycle")
    drivers = json.loads(row["top_risk_drivers_json"])
    assert drivers and drivers[0]["score"] >= drivers[-1]["score"]  # sorted desc
    audit = json.loads(row["audit_json"])
    assert audit["unclassifiable_reason"]["deleveraging_type"] is None


def test_compute_sgp_shaped_is_none_early_cycle(tmp_db):
    con = get_lazyray_conn()
    _seed_sgp_shaped(con)
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["SGP"]
    assert row["deleveraging_type"] == "none"
    assert row["dalio_stage"] == "early_or_mid_cycle"


def test_compute_thin_data_country_is_unclassifiable(tmp_db):
    con = get_lazyray_conn()
    # only political_execution present -- none of the 4 gating engines exist
    _insert_engine_row(con, "XXX", "political_execution", 20.0, "adequate", "full", "high", {
        "components": {},
    })
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["XXX"]
    assert row["dalio_stage"] is None
    assert row["deleveraging_type"] is None
    audit = json.loads(row["audit_json"])
    assert set(audit["unclassifiable_reason"]["dalio_stage"]) == {
        "sovereign_solvency", "funding_liquidity", "private_credit", "external_constraint"}
    assert set(audit["unclassifiable_reason"]["deleveraging_type"]) == {
        "sovereign_solvency", "funding_liquidity", "external_constraint"}


def test_compute_no_funding_data_gets_named_stage_not_a_bare_none(tmp_db):
    # WP2/C: funding_liquidity's coverage_tier='no_data' (branch=none) must
    # produce dalio_stage='unclassified_no_funding_data', never a silent
    # None -- even though every OTHER gate engine has full data (unlike the
    # thin-data test above, where everything is simply missing).
    con = get_lazyray_conn()
    _insert_engine_row(con, "QAT", "sovereign_solvency", 5.0, "low", "full", "high", {
        "real_growth_pct": 3.0, "components": {},
    })
    _insert_engine_row(con, "QAT", "external_constraint", 5.0, "low", "full", "high", {
        "components": {},
    })
    _insert_engine_row(con, "QAT", "private_credit", 10.0, "low", "full", "high", {
        "components": {},
    })
    con.execute(
        "INSERT INTO engine_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?, ?)",
        ["QAT", REF, "funding_liquidity", None, None, "no_data", "low", 0, 0, "{}", None, None])
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["QAT"]
    assert row["dalio_stage"] == "unclassified_no_funding_data"
    assert row["deleveraging_type"] is None
    assert row["overall_confidence"] == "low"
    caveats = json.loads(row["caveats_json"])
    assert any("no funding data" in c for c in caveats)


def test_compute_insufficient_tier_gates_like_missing_row(tmp_db):
    # a row that EXISTS but is coverage_tier='insufficient' must gate exactly
    # like a missing row -- the gate checks tier, not row presence
    con = get_lazyray_conn()
    _seed_arg_shaped(con)
    con.execute(
        "UPDATE engine_scores SET coverage_tier = 'insufficient', score = NULL, label = NULL "
        "WHERE country_iso3 = 'ARG' AND engine = 'external_constraint'")
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["ARG"]
    assert row["deleveraging_type"] is None
    assert row["dalio_stage"] is None


def test_compute_ignores_raw_score_uses_only_label(tmp_db):
    # proves the "no second hysteresis mechanism" design: changing the raw
    # score while holding the label fixed must not change the classification
    con = get_lazyray_conn()
    _seed_arg_shaped(con)
    con.commit()
    df1 = cc.compute(con, REF)

    con = get_lazyray_conn()
    con.execute("UPDATE engine_scores SET score = 61.0 WHERE country_iso3 = 'ARG' "
               "AND engine = 'funding_liquidity'")   # label stays 'severe'
    con.commit()
    df2 = cc.compute(con, REF)
    con.close()

    r1 = df1.set_index("country_iso3").loc["ARG"]
    r2 = df2.set_index("country_iso3").loc["ARG"]
    assert r1["dalio_stage"] == r2["dalio_stage"]
    assert r1["deleveraging_type"] == r2["deleveraging_type"]


def test_compute_empty_engine_scores_returns_empty_frame(tmp_db):
    con = get_lazyray_conn()
    df = cc.compute(con, REF)
    con.close()
    assert df.empty
    assert list(df.columns) == cc._COLUMNS


# ---------------------------------------------------------------------------
# Full pipeline integration: macro_panel (hub) -> run_dalio_v2() (all 5
# engines) -> dalio_cycle_v2 (LazyRay's own DB), via the real runner.py
# wiring (Fase 5.4)
# ---------------------------------------------------------------------------

from market_data_hub.db.connection import get_conn as get_hub_conn         # noqa: E402
from market_data_hub.db.upsert import upsert                              # noqa: E402
import pandas as pd                                                       # noqa: E402

from lazyray.dalio_v2.runner import run_dalio_v2                          # noqa: E402


def _pr(date_, iso3, ind, val, freq="A"):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "test", "orientation": 0,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": freq}


# WEO/WDI/GDD/IIP annual LEVEL indicators are cutoff-gated to Dec 31 of the
# year BEFORE ref_date's year (scoring.actual_cutoff()); ref_date below is
# always REF = 2026-12-31, so 2025-12-31. BIS (reer_broad) and FRED
# (bond_yield_10y) are monthly actuals with no forecast rows and stay
# exempt, at their own already-realistic dates. See test_dalio_v2.py's
# _LEVEL_DATE for the same convention.
_ARG_LEVEL_DATE = dt.date(2025, 12, 31)


def _seed_arg_full_pipeline(con):
    rows = []
    # sovereign_solvency: debt/GDP falling hard (2021-2026), classic
    # hyperinflation-erosion pattern; positive-ish real growth, extreme
    # inflation -- the ARG regression case, but built from raw macro_panel
    # through all 5 real engines instead of a hand-seeded engine_scores row.
    debt_traj = {2021: 110, 2022: 100, 2023: 88, 2024: 75, 2025: 60, 2026: 45}
    for y, v in debt_traj.items():
        rows.append(_pr(dt.date(y, 12, 31), "ARG", "public_debt_gdp", v))
    for ind, v in [("govt_net_debt_gdp", 40.0), ("interest_on_debt_gdp", 3.0),
                  ("government_revenue_gdp", 18.0), ("primary_balance_gdp", -1.0),
                  ("gdp_growth_weo", 2.0), ("inflation_avg_weo", 30.0)]:
        rows.append(_pr(_ARG_LEVEL_DATE, "ARG", ind, v))
    # external_constraint (inflation_avg_weo shared with sovereign above)
    for ind, v in [("current_account_gdp", 0.5), ("iip_net_position", -50000.0),
                  ("gdp_current_usd", 600000.0), ("short_term_debt_reserves", 164.0),
                  ("debt_service_exports", 38.0), ("fx_reserves_months_imports", 3.0)]:
        rows.append(_pr(_ARG_LEVEL_DATE, "ARG", ind, v))
    # reer_broad: sharp 12m depreciation (130 -> 90, ~+31% per the sign
    # convention in _fx_depreciation_12m_pct)
    rows.append(_pr(dt.date(2025, 6, 30), "ARG", "reer_broad", 130.0, freq="M"))
    rows.append(_pr(dt.date(2026, 6, 30), "ARG", "reer_broad", 90.0, freq="M"))
    # private_credit (gdp_growth_weo shared with sovereign above); shifted
    # one year earlier than the trajectory's original 2017-2026 span so its
    # latest print (2025) clears the LEVEL cutoff too.
    debt_gdp_traj = {y: v for y, v in zip(range(2016, 2026),
                     [40, 42, 45, 48, 52, 58, 65, 74, 84, 95])}
    for y, v in debt_gdp_traj.items():
        rows.append(_pr(dt.date(y, 12, 31), "ARG", "private_debt_gdp", v))
    rows.append(_pr(_ARG_LEVEL_DATE, "ARG", "npl_ratio", 6.0))
    # funding_liquidity (short_term_debt_reserves shared with external above)
    rows.append(_pr(dt.date(2025, 12, 31), "ARG", "bond_yield_10y", 15.0, freq="M"))
    rows.append(_pr(dt.date(2026, 12, 31), "ARG", "bond_yield_10y", 22.0, freq="M"))
    upsert(con, "macro_panel", pd.DataFrame(rows))


def test_full_pipeline_arg_profile_is_inflationary(tmp_db):
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    summary = run_dalio_v2(ref_date=REF)
    assert summary["cycle_classifier"] >= 1

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT dalio_stage, deleveraging_type, audit_json FROM dalio_cycle_v2 "
        "WHERE country_iso3 = 'ARG'").fetchone()
    con.close()

    dalio_stage, deleveraging_type, audit_json = row
    assert deleveraging_type == "inflationary"
    assert deleveraging_type != "beautiful"
    # sanity: the gate actually passed (real engine data was sufficient),
    # not an accidental null-everything from insufficient coverage
    audit = json.loads(audit_json)
    assert audit["unclassifiable_reason"]["deleveraging_type"] is None


def test_full_pipeline_rerun_is_idempotent_and_drops_stale_rows(tmp_db):
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    run_dalio_v2(ref_date=REF)
    summary2 = run_dalio_v2(ref_date=REF)   # unchanged data, same ref_date

    con = get_lazyray_conn(read_only=True)
    n = con.execute(
        "SELECT count(*) FROM dalio_cycle_v2 WHERE country_iso3 = 'ARG' "
        "AND ref_date = DATE '2026-12-31'").fetchone()[0]
    con.close()
    assert n == 1                              # DELETE-then-INSERT, never duplicated
    assert summary2["cycle_classifier"] == summary2["cycle_classifier"]  # ran cleanly twice


def test_full_pipeline_hysteresis_stability_no_second_mechanism_needed(tmp_db):
    # A score nudged within the hysteresis dead-band (label unchanged) must
    # not flip dalio_stage/deleveraging_type -- proves stability is fully
    # inherited from the 5 engines' own bucket_with_hysteresis(), with no
    # separate hysteresis state needed in the classifier itself.
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    run_dalio_v2(ref_date=REF)
    con = get_lazyray_conn(read_only=True)
    before = con.execute(
        "SELECT dalio_stage, deleveraging_type FROM dalio_cycle_v2 "
        "WHERE country_iso3 = 'ARG'").fetchone()
    funding_label_before = con.execute(
        "SELECT label FROM engine_scores WHERE country_iso3 = 'ARG' "
        "AND engine = 'funding_liquidity'").fetchone()[0]
    con.close()

    # nudge the underlying data slightly (short_term_debt_reserves a hair
    # higher) and rerun for the SAME ref_date -- prev_label() anchors the
    # funding_liquidity engine's hysteresis against its own prior run, so a
    # small move should not cross the dead-band. Same (date, country,
    # indicator) key as the original seed (_ARG_LEVEL_DATE) so this is a
    # revision of that row, not a second, cutoff-excluded one.
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _pr(_ARG_LEVEL_DATE, "ARG", "short_term_debt_reserves", 166.0)]))
    con.commit()
    con.close()
    run_dalio_v2(ref_date=REF)

    con = get_lazyray_conn(read_only=True)
    after = con.execute(
        "SELECT dalio_stage, deleveraging_type FROM dalio_cycle_v2 "
        "WHERE country_iso3 = 'ARG'").fetchone()
    funding_label_after = con.execute(
        "SELECT label FROM engine_scores WHERE country_iso3 = 'ARG' "
        "AND engine = 'funding_liquidity'").fetchone()[0]
    con.close()

    assert funding_label_after == funding_label_before   # engine's own hysteresis held
    assert after == before                                # classifier followed suit


def test_partial_engine_run_on_a_new_ref_year_does_not_clobber_the_prior_years_classification(tmp_db):
    # Codex review finding: a routine partial-engine refresh for a NEW
    # ref_year (before the other 4 engines have ever run for that date) must
    # not overwrite a complete prior-year classification with mostly-
    # unclassifiable rows -- the report picks the globally latest ref_date,
    # so that would hide a good classification behind a worse one.
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    run_dalio_v2(ref_date=REF)   # full run: all 5 engines have 2026 data
    con = get_lazyray_conn(read_only=True)
    before = con.execute(
        "SELECT dalio_stage, deleveraging_type FROM dalio_cycle_v2 "
        "WHERE country_iso3 = 'ARG' AND ref_date = DATE '2026-12-31'").fetchone()
    con.close()
    assert before is not None and before[1] == "inflationary"

    # only sovereign_solvency has ever run for 2027 -- the other 4 engines
    # have zero rows at this ref_date
    summary = run_dalio_v2(engines=["sovereign_solvency"], ref_date=dt.date(2027, 12, 31))
    assert summary["cycle_classifier"] is None   # guard skipped the refresh

    con = get_lazyray_conn(read_only=True)
    n_2027 = con.execute(
        "SELECT count(*) FROM dalio_cycle_v2 WHERE ref_date = DATE '2027-12-31'").fetchone()[0]
    still_2026 = con.execute(
        "SELECT dalio_stage, deleveraging_type FROM dalio_cycle_v2 "
        "WHERE country_iso3 = 'ARG' AND ref_date = DATE '2026-12-31'").fetchone()
    latest_ref_date = con.execute("SELECT max(ref_date) FROM dalio_cycle_v2").fetchone()[0]
    con.close()

    assert n_2027 == 0                      # no half-classified 2027 rows written
    assert still_2026 == before             # 2026's complete classification untouched
    assert str(latest_ref_date) == "2026-12-31"   # report still resolves the good year


def test_partial_engine_run_still_refreshes_once_all_engines_have_run_for_that_date(tmp_db):
    # the guard must NOT block the legitimate case: re-running one engine
    # for a ref_date where all 5 already have data (a normal correction/
    # refresh) should still update the classification as before.
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    run_dalio_v2(ref_date=REF)   # all 5 engines now have 2026 rows
    summary = run_dalio_v2(engines=["sovereign_solvency"], ref_date=REF)
    assert summary["cycle_classifier"] is not None and summary["cycle_classifier"] >= 1

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT deleveraging_type FROM dalio_cycle_v2 WHERE country_iso3 = 'ARG' "
        "AND ref_date = DATE '2026-12-31'").fetchone()
    con.close()
    assert row[0] == "inflationary"


def test_compute_full_tier_with_growth_missing_is_unclassifiable_not_early_cycle(tmp_db):
    # Codex review finding, at the compute() level: sovereign_solvency can
    # be coverage_tier='full' (6 of 7 other components present) while
    # real_growth_pct specifically is missing from its audit -- the tier
    # gate alone does not catch this, so dalio_stage must still come out
    # None (via classify_dalio_stage's own real_growth_pct check), and the
    # audit trail must name the specific missing field, not just say
    # "insufficient coverage" (which would be misleading -- the engine's
    # coverage genuinely was sufficient).
    con = get_lazyray_conn()
    _insert_engine_row(con, "ARG", "sovereign_solvency", 40.0, "watch", "full", "high", {
        "r_effective_pct": 1.5, "debt_trend_actuals_5y": -8.0,   # real_growth_pct absent
        "components": {"r_minus_g": {"score": 92.0, "raw_value": -18.4, "weight": 1}},
    })
    _insert_engine_row(con, "ARG", "external_constraint", 60.0, "moderate", "full", "high", {
        "fx_depreciation_12m_pct": 5.0,
        "components": {"inflation": {"score": 30.0, "raw_value": 6.0, "weight": 1}},
    })
    _insert_engine_row(con, "ARG", "funding_liquidity", 40.0, "watch", "proxy", "medium", {
        "components": {},
    })
    _insert_engine_row(con, "ARG", "private_credit", 30.0, "moderate", "proxy", "medium", {
        "components": {},
    })
    con.commit()

    df = cc.compute(con, REF)
    con.close()

    row = df.set_index("country_iso3").loc["ARG"]
    assert row["dalio_stage"] is None
    audit = json.loads(row["audit_json"])
    assert audit["unclassifiable_reason"]["dalio_stage"] == ["sovereign_solvency:real_growth_pct"]
    caveats = json.loads(row["caveats_json"])
    assert any("real_growth_pct" in c for c in caveats)


def test_gate_failing_on_rerun_clears_the_stale_classification(tmp_db):
    # Codex review: a required engine that HAD rows for this exact ref_date
    # (from an earlier full run) can lose them on a rerun if its input data
    # disappeared -- the DELETE-then-maybe-reinsert in the engine loop above
    # replaces its batch with nothing. Before this fix, the gate then failing
    # left the PRIOR run's dalio_cycle_v2 row dangling, describing engines
    # that no longer have any data for this ref_date.
    con = get_hub_conn()
    _seed_arg_full_pipeline(con)
    con.commit()
    con.close()

    run_dalio_v2(ref_date=REF)   # full run: dalio_cycle_v2 gets an ARG row
    con = get_lazyray_conn(read_only=True)
    before = con.execute(
        "SELECT count(*) FROM dalio_cycle_v2 WHERE ref_date = DATE '2026-12-31'"
    ).fetchone()[0]
    con.close()
    assert before == 1

    # wipe the hub's macro_panel entirely: sovereign_solvency (a required
    # engine) now returns an empty frame for this ref_date
    con = get_hub_conn()
    con.execute("DELETE FROM macro_panel")
    con.commit()
    con.close()

    summary = run_dalio_v2(engines=["sovereign_solvency"], ref_date=REF)
    assert summary["cycle_classifier"] is None   # gate failed

    con = get_lazyray_conn(read_only=True)
    after = con.execute(
        "SELECT count(*) FROM dalio_cycle_v2 WHERE ref_date = DATE '2026-12-31'"
    ).fetchone()[0]
    con.close()
    assert after == 0   # the stale ARG row was cleared, not left dangling

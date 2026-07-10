# -*- coding: utf-8 -*-
"""Regression tests for dalio.py (moved here from market-data-hub's
tests/test_audit_fixes_sources.py along with dalio.py itself when the Dalio
analytical layer was extracted into this repo).

Covers: orientation-0 exclusion from the cross-country z, policy-rate vs
implied-rate split, deleveraging quality using the policy rate while the
debt-cycle phase itself stays on the implied stock rate.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import upsert

from lazyray.dalio import classify_cycle_phase, run_dalio
from lazyray.db.connection import get_conn as get_lazyray_conn


def _panel_row(date, iso3, ind, value, pillar, orient, freq):
    return {"date": date, "country_iso3": iso3, "indicator_id": ind,
            "value": value, "indicator_name": ind, "pillar": pillar,
            "orientation": orient, "source": "test", "provider_dataset": "X",
            "provider_code": "Y", "unit": "pct", "frequency": freq}


def test_orientation_zero_not_coerced_to_pos(tmp_db):
    con = get_hub_conn()
    rows = []
    for iso3, g, r in [("USA", 10.0, 10.0), ("ITA", 0.0, 5.0), ("CHE", -10.0, 0.5)]:
        rows.append(_panel_row(dt.date(2025, 12, 31), iso3, "gdp_growth_weo",
                               g, "growth", 1, "A"))
        rows.append(_panel_row(dt.date(2026, 6, 30), iso3, "bis_policy_rate",
                               r, "liquidity", 0, "M"))
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio()

    con = get_lazyray_conn()
    try:
        pol = con.execute(
            "SELECT z_score, signal FROM dalio_signals "
            "WHERE indicator_id = 'bis_policy_rate'").fetch_df()
        # rows stay visible but never scored: no POS 'strength' for the
        # highest policy rate
        assert len(pol) == 3
        assert pol["z_score"].isna().all()
        assert (pol["signal"] == "NEUTRAL").all()

        gro = con.execute(
            "SELECT country_iso3, z_score, signal FROM dalio_signals "
            "WHERE indicator_id = 'gdp_growth_weo'").fetch_df()
        usa = gro.set_index("country_iso3").loc["USA"]
        assert usa["z_score"] > 0 and usa["signal"] == "POS"   # oriented: scored

        pillars = con.execute(
            "SELECT DISTINCT pillar FROM pillar_scores").fetch_df()["pillar"]
        assert "growth" in set(pillars)
        assert "liquidity" not in set(pillars)   # only orientation-0 members
    finally:
        con.close()


_TH = {"credit_gap_bubble": 10.0, "dsr_high": 20.0, "dsr_peak_pct": 0.8,
       "rate_near_zero": 1.0, "credit_gap_late": 5.0, "weak_growth": 1.5,
       "debt_high_level": 100.0, "debt_crisis_level": 130.0,
       "deficit_large": -4.5, "debt_trend_high": 1.5, "debt_trend_moderate": 0.7}


def test_pushing_on_string_reads_policy_rate():
    x = {"growth": 0.5, "credit_gap": 0.0, "nom_growth": 2.0,
         "nom_rate": 4.0,          # implied stock rate, never near zero
         "policy_rate": 0.25, "debt_level": 60.0, "debt_falling": False,
         "debt_trend": 0.0, "fiscal_balance": -2.0, "dsr": 10.0, "dsr_pct": 0.5}
    assert classify_cycle_phase(x, _TH) == "PUSHING_ON_STRING"
    # without a policy rate the branch must not fire on the implied rate
    x2 = dict(x, policy_rate=None)
    assert classify_cycle_phase(x2, _TH) == "EARLY_EXPANSION"


def test_deleveraging_phase_keeps_implied_rate():
    # the r-vs-g debt-dynamics test stays on the stock rate: gn < rn -> UGLY
    # even when the policy rate is far below nominal growth
    x = {"growth": 2.0, "credit_gap": 0.0, "nom_growth": 5.0,
         "nom_rate": 7.0, "policy_rate": 1.5, "debt_level": 90.0,
         "debt_falling": True, "debt_trend": -3.0, "fiscal_balance": -2.0,
         "dsr": 10.0, "dsr_pct": 0.5}
    assert classify_cycle_phase(x, _TH) == "UGLY_DELEVERAGING"
    assert classify_cycle_phase(dict(x, nom_rate=4.0), _TH) == "BEAUTIFUL_DELEVERAGING"


def test_deleveraging_quality_uses_policy_rate(tmp_db):
    # One country, debt falling; nominal growth 5%: implied stock rate ~7.2%
    # (UGLY r-vs-g phase) but policy rate 1.5% (BEAUTIFUL deleveraging quality).
    con = get_hub_conn()
    rows = []
    for y in range(2023, 2032):                       # ry-3 .. ry+5 window
        rows.append(_panel_row(dt.date(y, 12, 31), "USA", "public_debt_gdp",
                               120.0 - 3.0 * (y - 2023), "sovereign", -1, "A"))
    for y in (2024, 2025, 2026):
        rows.append(_panel_row(dt.date(y, 12, 31), "USA", "gdp_growth_weo",
                               2.0, "growth", 1, "A"))
        rows.append(_panel_row(dt.date(y, 12, 31), "USA", "inflation_avg_weo",
                               3.0, "liquidity", -1, "A"))
    # implied_interest_rate is DERIVED by v_macro_panel_ext: ie / debt * 100
    rows.append(_panel_row(dt.date(2026, 12, 31), "USA", "interest_on_debt_gdp",
                           8.0, "markets", -1, "A"))
    rows.append(_panel_row(dt.date(2026, 6, 30), "USA", "bis_policy_rate",
                           1.5, "liquidity", 0, "M"))
    upsert(con, "macro_panel", pd.DataFrame(rows))
    con.commit()
    con.close()

    run_dalio(ref_year=2026)

    con = get_lazyray_conn()
    try:
        phase, delev, nom_rate = con.execute(
            "SELECT debt_cycle_phase, deleveraging_quality, nom_rate "
            "FROM regime_state WHERE country_iso3 = 'USA'").fetchone()
    finally:
        con.close()
    assert phase == "UGLY_DELEVERAGING"          # r-vs-g on the implied rate
    assert delev == "BEAUTIFUL"                  # quality on the policy rate
    assert nom_rate == pytest.approx(8.0 / 111.0 * 100.0, rel=1e-6)

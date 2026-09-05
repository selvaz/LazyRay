# -*- coding: utf-8 -*-
"""
test_dalio_v2_cutoff.py — Fase 1 deliverable B
(docs/DALIO_PROD_ASSESSMENT_2026-09.md §2.3/§3.1.4): a LEVEL component must
never read an observation dated after scoring.actual_cutoff(ref_date) (Dec
31 of ref_date.year - 1), while the one exempt TREND component
(debt_trend_5y) keeps reading the full forecast-inclusive window unchanged.

This is exactly the flagship bug the assessment measured in production:
"USA sovereign 44,6 'watch': debt_gdp 125,8 (forecast WEO 2026, obs_date
2026-12-31)... Tre date diverse dentro un solo punteggio 'as of 2026-12-31'."
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import upsert

from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn as get_lazyray_conn

REF = dt.date(2026, 12, 31)   # >= today (2026-09-04): live path, plain upsert() works


def _row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "sovereign", "orientation": -1,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "A"}


def _seed_projection_vs_actual(con):
    # public_debt_gdp: a real actual through 2025 (comfortable, flat-ish),
    # then a much higher 2026 WEO *projection* -- exactly the shape of the
    # measured USA bug (actual debt in the 40s-50s, forecast row ~126).
    traj = {2021: 48.0, 2022: 49.0, 2023: 50.0, 2024: 51.0, 2025: 52.0, 2026: 126.0}
    for y, v in traj.items():
        upsert(con, "macro_panel", pd.DataFrame([
            _row(dt.date(y, 12, 31), "USA", "public_debt_gdp", v)]))
    # everything else the engine needs, dated safely at/before the cutoff
    # (2025-12-31) so the row is not itself the thing under test
    for ind, v in [("govt_net_debt_gdp", 45.0), ("interest_on_debt_gdp", 2.0),
                  ("government_revenue_gdp", 32.0), ("primary_balance_gdp", -1.0),
                  ("gdp_growth_weo", 2.2), ("inflation_avg_weo", 2.1)]:
        upsert(con, "macro_panel", pd.DataFrame([
            _row(dt.date(2025, 12, 31), "USA", ind, v)]))


def test_level_component_ignores_the_projection_row_the_trend_still_uses_it(tmp_db):
    con = get_hub_conn()
    _seed_projection_vs_actual(con)
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=REF)

    con = get_lazyray_conn(read_only=True)
    audit = json.loads(con.execute(
        "SELECT components_json FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 = 'USA'").fetchone()[0])
    con.close()

    debt_gdp = audit["components"]["debt_gdp"]
    debt_trend = audit["components"]["debt_trend_5y"]

    # LEVEL: the 2026 projection (126.0) must be invisible -- the component
    # falls back to the last actual at/before the cutoff (2025, 52.0)
    assert debt_gdp["raw_value"] == 52.0
    assert debt_gdp["obs_date"] == "2025-12-31"
    assert debt_gdp["raw_value"] != 126.0

    # TREND: debt_trend_5y is explicitly exempt and keeps reading the
    # forecast-inclusive window (dalio._slope over ref_ts.year-3..+5) -- a
    # steep positive slope only explainable by the 2026 jump being included
    assert debt_trend["raw_value"] is not None and debt_trend["raw_value"] > 10.0

    # data_through: the audit-wide "as of which date are the levels real"
    # marker must reflect the actual (2025-12-31), never the 2026 forecast
    assert audit["data_through"] == "2025-12-31"


def test_cutoff_is_a_noop_when_no_row_exists_past_it(tmp_db):
    # Sanity check the cutoff doesn't change anything when the only data IS
    # the actual (no forecast row at all) -- guards against a cutoff that's
    # accidentally too aggressive and drops legitimate current data.
    con = get_hub_conn()
    for ind, v in [("public_debt_gdp", 50.0), ("govt_net_debt_gdp", 45.0),
                  ("interest_on_debt_gdp", 2.0), ("government_revenue_gdp", 32.0),
                  ("primary_balance_gdp", -1.0), ("gdp_growth_weo", 2.2),
                  ("inflation_avg_weo", 2.1)]:
        upsert(con, "macro_panel", pd.DataFrame([
            _row(dt.date(2025, 12, 31), "USA", ind, v)]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=REF)

    con = get_lazyray_conn(read_only=True)
    row = con.execute(
        "SELECT coverage_tier, score FROM engine_scores WHERE engine = 'sovereign_solvency' "
        "AND country_iso3 = 'USA'").fetchone()
    con.close()
    # 6/8: debt_trend_5y and debt_p_up_5y (DSA) both need multi-year history
    # this single-year seed doesn't have -- coverage tier, not the cutoff
    # mechanism under test here, which is otherwise a no-op (see test name).
    assert row[0] == "proxy"
    assert row[1] is not None

# -*- coding: utf-8 -*-
"""
test_make_dalio_report.py — the unified v1+v2 dashboard: collect() must wire
Dalio v2 engine scores onto the same per-country dict (has_v2 flag, v2 sub-
dict) when engine_scores is populated, and degrade cleanly (has_v2=False,
empty v2 dicts) when it isn't. See make_dalio_report.py's module docstring.
"""
from __future__ import annotations

import datetime as dt
import random

import pandas as pd

from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import upsert

from lazyray.classify import classify_countries
from lazyray.dalio import run_dalio
from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn as get_lazyray_conn
from tests.test_dalio_v2 import _seed as seed_v2

import make_dalio_report as mdr

# Copied from market-data-hub's tests/test_pipeline.py (not importable
# across repos in CI) -- kept in sync manually if the upstream fixture ever
# changes shape.
V1_IND = [
    ("gdp_growth_weo", "growth", 1, "A"), ("real_gdp_growth", "growth", 1, "A"),
    ("inflation_avg_weo", "liquidity", -1, "A"), ("inflation_cpi", "liquidity", -1, "A"),
    ("public_debt_gdp", "sovereign", -1, "A"), ("fiscal_balance_gdp", "sovereign", 1, "A"),
    ("labor_productivity_level", "growth", 1, "A"), ("bis_dsr_private", "debt_cycle", -1, "Q"),
    ("bis_credit_gap", "debt_cycle", -1, "Q"), ("bis_policy_rate", "liquidity", 0, "M"),
    ("fuel_exports_share", "geopolitical", 0, "A"), ("fuel_imports_share", "external", 0, "A"),
    ("exports_gdp", "external", 1, "A"), ("imports_gdp", "external", 0, "A"),
    ("natural_resource_rents_gdp", "geopolitical", 0, "A"),
    ("tourism_exports_share", "external", 0, "A"), ("remittances_gdp", "external", 0, "A"),
    ("metals_exports_share", "geopolitical", 0, "A"),
]


def _seed_v1_macro_panel(con):
    random.seed(0)
    upsert(con, "macro_panel", pd.DataFrame([{
        "date": dt.date(y, 12, 31), "country_iso3": c, "indicator_id": i,
        "value": random.uniform(1, 50), "indicator_name": i, "pillar": p,
        "orientation": o, "source": "imf" if "weo" in i else "wb",
        "provider_dataset": "X", "provider_code": "Y", "unit": "pct", "frequency": f}
        for c in ["USA", "ITA", "CHE"] for i, p, o, f in V1_IND
        for y in range(2015, 2031)]))


def test_collect_without_v2_degrades_cleanly(tmp_db):
    con = get_hub_conn()
    _seed_v1_macro_panel(con)
    con.commit()
    con.close()

    run_dalio()
    classify_countries()

    con = get_lazyray_conn(read_only=True)
    d = mdr.collect(con)
    con.close()

    assert d["has_v2"] is False
    assert d["has_cycle"] is False
    assert "USA" in d["countries"]
    assert d["countries"]["USA"]["v2"] == {}
    assert d["countries"]["USA"]["cycle"] == {}
    # render_html must not choke on the empty-v2/empty-cycle case
    html = mdr.render_html(d)
    assert "V2_ENGINE_NAMES" in html
    assert "STAGE_NAMES" in html


def test_collect_with_v2_wires_engine_scores_onto_the_same_country(tmp_db):
    con = get_hub_conn()
    _seed_v1_macro_panel(con)
    seed_v2(con)   # adds USA/SGP/ARG dalio_v2 indicators (USA overlaps with v1's seed)
    con.commit()
    con.close()

    run_dalio()
    classify_countries()
    run_dalio_v2(engines=["sovereign_solvency", "political_execution"])

    con = get_lazyray_conn(read_only=True)
    d = mdr.collect(con)
    con.close()

    assert d["has_v2"] is True
    usa_v2 = d["countries"]["USA"]["v2"]
    assert set(usa_v2) == {"sovereign_solvency", "political_execution"}
    assert usa_v2["sovereign_solvency"]["coverage_tier"] in ("full", "proxy", "insufficient")
    # ITA shares public_debt_gdp/fiscal_balance_gdp with v1's seed, so the
    # engine still emits a (2/7, insufficient, score=None) row for it -- the
    # coverage-tier discipline, not a bug (see suppress_insufficient). It
    # has no WGI data at all, so political_execution is absent entirely.
    ita_v2 = d["countries"]["ITA"]["v2"]
    assert set(ita_v2) == {"sovereign_solvency"}
    assert ita_v2["sovereign_solvency"]["coverage_tier"] == "insufficient"
    assert ita_v2["sovereign_solvency"]["score"] is None

    # a country with v2 scores but no v1 regime_state row must still get a
    # country sheet (v1 fields null) instead of being silently dropped
    con = get_lazyray_conn()
    con.execute(
        "INSERT INTO engine_scores VALUES ('MEX', DATE '2026-12-31', "
        "'sovereign_solvency', 55.0, 'watch', 'proxy', 'medium', 5, 7, '{}', now(), NULL, NULL)")
    con.close()
    con = get_lazyray_conn(read_only=True)
    d = mdr.collect(con)
    con.close()
    assert "MEX" in d["countries"]
    assert d["countries"]["MEX"]["phase"] is None
    assert d["countries"]["MEX"]["v2"]["sovereign_solvency"]["score"] == 55.0

    html = mdr.render_html(d)
    assert "Dalio v2" in html
    # </script> inside embedded JSON is escaped so DB data can never
    # terminate the script block and blank the page
    assert "</script>" not in mdr._js_str({"x": "</script>"})


def test_collect_wires_cycle_classifier_onto_the_same_country(tmp_db):
    con = get_hub_conn()
    _seed_v1_macro_panel(con)
    con.commit()
    con.close()

    run_dalio()
    classify_countries()
    con = get_lazyray_conn()
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES ('USA', DATE '2026-12-31', 'crisis', "
        "'inflationary', 'high', '[{\"engine\":\"sovereign_solvency\",\"component\":\"r_minus_g\","
        "\"score\":92.0,\"raw_value\":-18.4,\"weight\":1}]', "
        "'[\"deleveraging_type unclassifiable: test caveat\"]', '{}', now())")
    con.commit()
    con.close()

    con = get_lazyray_conn(read_only=True)
    d = mdr.collect(con)
    con.close()

    assert d["has_cycle"] is True
    usa_cycle = d["countries"]["USA"]["cycle"]
    assert usa_cycle["dalio_stage"] == "crisis"
    assert usa_cycle["deleveraging_type"] == "inflationary"
    assert usa_cycle["top_risk_drivers"][0]["component"] == "r_minus_g"
    assert usa_cycle["caveats"] == ["deleveraging_type unclassifiable: test caveat"]
    # a country with ONLY a dalio_cycle_v2 row (no v1/v2) must still get a sheet
    assert "ITA" in d["countries"]
    assert d["countries"]["ITA"]["cycle"] == {}

    html = mdr.render_html(d)
    assert "Cycle stage (v2)" in html or "STAGE_NAMES" in html

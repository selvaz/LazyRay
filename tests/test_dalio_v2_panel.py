# -*- coding: utf-8 -*-
"""
test_dalio_v2_panel.py — lazyray.dalio_v2.panel.load_panel_ext.

Covers Fase 1 deliverable C (docs/DALIO_PROD_ASSESSMENT_2026-09.md §3.1.3):
  - ref_date < today reads point-in-time from the hub's *_vintage tables
    (via reader.read_macro_panel(asof=...)/reader.read_macro(asof=...)),
    picking the value as known on ref_date, not a later revision.
  - the three v_macro_panel_ext-only derived indicators (bond_yield_10y,
    implied_interest_rate, fx_debt_share) are reproduced from the
    *_vintage tables exactly as the live view computes them.
  - ref_date >= today is unchanged: read_macro_panel_ext() live.

Uses record_vintage() (not upsert() alone) to populate the vintage tables:
upsert() only writes the "current" macro_panel/macro_series tables --
*_vintage is a separate history the ingestion pipeline appends to
point-in-time, which is what asof reads query.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import record_vintage, upsert

from lazyray.dalio_v2.panel import load_panel_ext, used_asof_path

TODAY = dt.date.today()
PAST = TODAY - dt.timedelta(days=400)          # safely < today, and < the
                                                # 2026-07-10 vintage-log start
                                                # is irrelevant here: we seed
                                                # our own vintage rows.
EARLIER_VINTAGE = PAST - dt.timedelta(days=10)
LATER_VINTAGE = PAST + dt.timedelta(days=5)


def _panel_row(date_, iso3, ind, value):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": value,
            "indicator_name": ind, "pillar": "test", "orientation": 0,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "A"}


def test_used_asof_path_boundary():
    assert used_asof_path(TODAY - dt.timedelta(days=1)) is True
    assert used_asof_path(TODAY) is False
    assert used_asof_path(TODAY + dt.timedelta(days=1)) is False


def test_asof_path_picks_the_earlier_vintage_not_the_latest_revision(tmp_db):
    # The value known as of PAST (100.0, recorded at EARLIER_VINTAGE) must be
    # what a run AT ref_date=PAST sees, even though the value was later
    # revised to 999.0 (recorded at LATER_VINTAGE, which is still <= today
    # but AFTER the ref_date we are asking about).
    con = get_hub_conn()
    row_v1 = pd.DataFrame([_panel_row(PAST, "USA", "public_debt_gdp", 100.0)])
    row_v2 = pd.DataFrame([_panel_row(PAST, "USA", "public_debt_gdp", 999.0)])
    upsert(con, "macro_panel", row_v1)
    record_vintage(con, "macro_panel", row_v1, vintage_date=EARLIER_VINTAGE)
    upsert(con, "macro_panel", row_v2)   # revise the "current" value
    record_vintage(con, "macro_panel", row_v2, vintage_date=LATER_VINTAGE)
    con.commit()
    con.close()

    df = load_panel_ext(PAST, None)
    row = df[(df["country_iso3"] == "USA") & (df["indicator_id"] == "public_debt_gdp")]
    assert len(row) == 1
    assert row["value"].iloc[0] == 100.0   # the vintage known AS OF PAST, not the later 999.0


def test_asof_path_reproduces_bond_yield_10y_from_fred_vintage(tmp_db):
    # bond_yield_10y is NOT a macro_panel indicator -- it's derived by
    # v_macro_panel_ext from macro_series rows whose series_id starts with
    # IRLTLT01 (see panel.py's _bond_yield_10y_asof / schema.sql's view).
    con = get_hub_conn()
    row = pd.DataFrame([{
        "date": PAST, "series_id": "IRLTLT01USM156N", "value": 4.25,
        "series_name": "x", "unit": "percent", "frequency": "M",
        "source": "fred", "country": "USA",
    }])
    upsert(con, "macro_series", row)
    record_vintage(con, "macro_series", row, vintage_date=EARLIER_VINTAGE)
    con.commit()
    con.close()

    df = load_panel_ext(PAST, None)
    hit = df[(df["country_iso3"] == "USA") & (df["indicator_id"] == "bond_yield_10y")]
    assert len(hit) == 1
    assert hit["value"].iloc[0] == 4.25
    assert hit["pillar"].iloc[0] == "markets"


def test_asof_path_reproduces_implied_interest_rate_and_fx_debt_share(tmp_db):
    # Both are derived per (date, country) from two macro_panel rows each --
    # v_macro_panel_ext's own formulas (schema.sql ~line 622), reproduced in
    # Python by panel.py's _derived_from_native() from the SAME asof read.
    con = get_hub_conn()
    rows = pd.DataFrame([
        _panel_row(PAST, "USA", "interest_on_debt_gdp", 8.0),
        _panel_row(PAST, "USA", "public_debt_gdp", 111.0),
        _panel_row(PAST, "USA", "fx_debt_usd", 8.0),
        _panel_row(PAST, "USA", "ext_debt_nonres_usd", 100.0),
    ])
    upsert(con, "macro_panel", rows)
    record_vintage(con, "macro_panel", rows, vintage_date=EARLIER_VINTAGE)
    con.commit()
    con.close()

    df = load_panel_ext(PAST, None)
    implied = df[(df["country_iso3"] == "USA") & (df["indicator_id"] == "implied_interest_rate")]
    fx_share = df[(df["country_iso3"] == "USA") & (df["indicator_id"] == "fx_debt_share")]
    assert len(implied) == 1
    assert implied["value"].iloc[0] == pytest.approx(8.0 / 111.0 * 100.0, rel=1e-9)
    assert len(fx_share) == 1
    assert fx_share["value"].iloc[0] == pytest.approx(8.0 / 100.0 * 100.0, rel=1e-9)


def test_live_path_unchanged_for_ref_date_ge_today(tmp_db):
    # ref_date >= today must be exactly read_macro_panel_ext(): no vintage
    # seeding at all, plain upsert() into macro_panel is visible immediately.
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _panel_row(TODAY, "USA", "public_debt_gdp", 55.5),
    ]))
    con.commit()
    con.close()

    df_today = load_panel_ext(TODAY, None)
    df_future = load_panel_ext(TODAY + dt.timedelta(days=30), None)
    for df in (df_today, df_future):
        hit = df[(df["country_iso3"] == "USA") & (df["indicator_id"] == "public_debt_gdp")]
        assert len(hit) == 1 and hit["value"].iloc[0] == 55.5


def test_asof_before_vintage_log_start_returns_whatever_exists(tmp_db):
    # Documented caveat (panel.py / load_panel_ext docstring): no special
    # case for the hub's real vintage log only starting 2026-07-10 -- an
    # asof date with nothing recorded before it simply returns empty/sparse
    # data, exactly like reader.read_macro_panel(asof=...) does on its own.
    con = get_hub_conn()
    con.close()   # bootstraps an empty, schema'd hub DB, nothing seeded

    df = load_panel_ext(dt.date(2020, 1, 1), None)
    assert df.empty

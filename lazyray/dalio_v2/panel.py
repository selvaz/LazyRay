# -*- coding: utf-8 -*-
"""
panel.py — point-in-time panel reads for the Dalio v2 engines.

load_panel_ext(ref_date, hub_db_path) returns the same shape as
market_data_hub.reader.read_macro_panel_ext(): long-format rows with columns
date, country_iso3, indicator_id, value, indicator_name, pillar, orientation,
source, provider_dataset, provider_code, unit, frequency, updated_at.

For ref_date >= today it is exactly read_macro_panel_ext() (the live view:
latest known values). For ref_date < today it reads the hub's *_vintage
tables via reader.read_macro_panel(asof=...) / reader.read_macro(asof=...)
so a historical run sees the data AS IT WAS KNOWN on ref_date, not revised
with hindsight -- avoiding the look-ahead that made every "As of <ref_date>"
report actually describe today's numbers regardless of ref_date.

v_macro_panel_ext is macro_panel PLUS three views/derivations that the plain
*_vintage tables cannot reproduce for free (see
market_data_hub/db/schema.sql's CREATE VIEW v_macro_panel_ext, ~line 622):
  - bond_yield_10y        -- FRED single-country series (macro_series,
                             series_id LIKE 'IRLTLT01%'), remapped by country.
  - implied_interest_rate -- interest_on_debt_gdp / public_debt_gdp * 100,
                             derived per (date, country) from two macro_panel
                             rows.
  - fx_debt_share         -- fx_debt_usd / ext_debt_nonres_usd * 100, derived
                             per (date, country) from two macro_panel rows.
The as-of branch below reproduces exactly these three formulas in Python
from the two source tables' own point-in-time (asof) reads, so the
point-in-time panel and the live view stay consistent in shape and content.

CAVEAT -- the hub's vintage log only starts 2026-07-10 (per
docs/DALIO_PROD_ASSESSMENT_2026-09.md §2). An `asof` date before that simply
returns whatever the vintage tables happen to hold as of that call (which
may be sparse or empty for a given key): this module does not special-case
that boundary -- it is the hub reader's own contract, and read_macro_panel
already documents "avoiding revision look-ahead in backtests" without a
minimum-date guard. Point-in-time correctness on this data source is only as
good as the vintage log's own coverage.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Optional

import pandas as pd
from market_data_hub.config_loader import get_fred_series, get_macro_panel_specs
from market_data_hub.reader import read_macro, read_macro_panel, read_macro_panel_ext

_EXT_COLUMNS = [
    "date", "country_iso3", "indicator_id", "value", "indicator_name", "pillar",
    "orientation", "source", "provider_dataset", "provider_code", "unit",
    "frequency", "updated_at",
]

# FRED series remapped into 'bond_yield_10y' panel rows by v_macro_panel_ext
# (WHERE series_id LIKE 'IRLTLT01%' AND value IS NOT NULL).
_BOND_YIELD_PREFIX = "IRLTLT01"


def used_asof_path(ref_date) -> bool:
    """True if load_panel_ext(ref_date, ...) reads point-in-time (asof)
    rather than the live view -- i.e. ref_date is strictly before today.
    Engines use this to set their audit trail's vintage_safe flag; keeping
    the decision here (rather than re-deriving `< date.today()` inline in
    five places) guarantees the flag and the actual read path never drift
    apart."""
    return bool(ref_date < _date.today())


def _empty_ext() -> pd.DataFrame:
    return pd.DataFrame(columns=_EXT_COLUMNS)


def _native_panel_asof(asof: str, hub_db_path: Optional[str]) -> pd.DataFrame:
    """Point-in-time read of every native macro_panel indicator (the
    'indicators=None' case read_macro_panel_ext supports live, but
    read_macro_panel itself requires an explicit list -- see module
    docstring / reader.py:363), enriched with the same metadata columns
    v_macro_panel_ext exposes (from the static macro_panel.yaml spec, since
    the *_vintage tables only carry date/country/indicator/value)."""
    specs = get_macro_panel_specs()
    ids = [s["id"] for s in specs if s.get("id")]
    if not ids:
        return _empty_ext()
    raw = read_macro_panel(indicators=ids, asof=asof, db_path=hub_db_path)
    if raw is None or raw.empty:
        return _empty_ext()
    raw = raw.dropna(subset=["value"]).copy()
    if raw.empty:
        return _empty_ext()
    spec_by_id = {s["id"]: s for s in specs}

    def _meta(ind_id, key):
        s = spec_by_id.get(ind_id) or {}
        return s.get(key)

    raw["indicator_name"] = raw["indicator_id"].map(lambda i: _meta(i, "name"))
    raw["pillar"] = raw["indicator_id"].map(lambda i: _meta(i, "pillar"))
    raw["orientation"] = raw["indicator_id"].map(lambda i: _meta(i, "orientation"))
    raw["source"] = raw["indicator_id"].map(lambda i: _meta(i, "source"))
    raw["provider_dataset"] = raw["indicator_id"].map(lambda i: _meta(i, "dataset"))
    raw["provider_code"] = raw["indicator_id"].map(lambda i: _meta(i, "code"))
    raw["unit"] = raw["indicator_id"].map(lambda i: _meta(i, "unit"))
    raw["frequency"] = raw["indicator_id"].map(lambda i: _meta(i, "freq"))
    raw["updated_at"] = pd.NaT
    return raw[_EXT_COLUMNS]


def _bond_yield_10y_asof(asof: str, hub_db_path: Optional[str]) -> pd.DataFrame:
    """Point-in-time reproduction of v_macro_panel_ext's bond_yield_10y
    branch: every FRED series whose id starts with 'IRLTLT01', remapped from
    macro_series.country -> country_iso3 (see schema.sql's comment: 'country
    holds the ISO3 for these cross-country series, so country AS
    country_iso3 is direct')."""
    fred = [e for e in get_fred_series() if str(e.get("symbol", "")).startswith(_BOND_YIELD_PREFIX)]
    if not fred:
        return _empty_ext()
    series_ids = [e["symbol"] for e in fred]
    country_by_symbol = {e["symbol"]: e.get("country") for e in fred}
    m = read_macro(series_ids=series_ids, asof=asof, wide=False, db_path=hub_db_path)
    if m is None or m.empty:
        return _empty_ext()
    m = m.dropna(subset=["value"]).copy()
    if m.empty:
        return _empty_ext()
    m["country_iso3"] = m["series_id"].map(country_by_symbol)
    m = m.dropna(subset=["country_iso3"])
    if m.empty:
        return _empty_ext()
    m["indicator_id"] = "bond_yield_10y"
    m["indicator_name"] = "10Y government bond yield (OECD via FRED)"
    m["pillar"] = "markets"
    m["orientation"] = 0
    m["source"] = "fred"
    m["provider_dataset"] = "FRED"
    m["provider_code"] = m["series_id"]
    m["unit"] = "percent"
    m["frequency"] = "M"
    m["updated_at"] = pd.NaT
    return m[_EXT_COLUMNS]


def _derived_from_native(native_raw: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time reproduction of v_macro_panel_ext's two per-(date,
    country) derived branches (implied_interest_rate, fx_debt_share) from
    the SAME point-in-time native rows _native_panel_asof() already read --
    never a second, independently-vintaged read of the source indicators."""
    if native_raw.empty:
        return _empty_ext()
    need = {"interest_on_debt_gdp", "public_debt_gdp", "fx_debt_usd", "ext_debt_nonres_usd"}
    sub = native_raw[native_raw["indicator_id"].isin(need)]
    if sub.empty:
        return _empty_ext()
    wide = sub.pivot_table(index=["date", "country_iso3"], columns="indicator_id",
                           values="value", aggfunc="last").reset_index()

    frames = []
    if {"interest_on_debt_gdp", "public_debt_gdp"} <= set(wide.columns):
        d = wide[["date", "country_iso3", "interest_on_debt_gdp", "public_debt_gdp"]].copy()
        d = d.dropna(subset=["interest_on_debt_gdp"])
        d = d[d["public_debt_gdp"] > 0]
        if not d.empty:
            d["indicator_id"] = "implied_interest_rate"
            d["value"] = d["interest_on_debt_gdp"] / d["public_debt_gdp"] * 100.0
            d["indicator_name"] = ("Implied interest rate on govt debt "
                                   "(IMF interest %GDP ÷ debt %GDP)")
            d["pillar"] = "markets"
            d["orientation"] = 0
            d["source"] = "derived"
            d["provider_dataset"] = "IMF(derived)"
            d["provider_code"] = "ie/GGXWDG_NGDP*100"
            d["unit"] = "percent"
            d["frequency"] = "A"
            d["updated_at"] = pd.NaT
            frames.append(d[_EXT_COLUMNS])

    if {"fx_debt_usd", "ext_debt_nonres_usd"} <= set(wide.columns):
        d = wide[["date", "country_iso3", "fx_debt_usd", "ext_debt_nonres_usd"]].copy()
        d = d.dropna(subset=["fx_debt_usd"])
        d = d[d["ext_debt_nonres_usd"] > 0]
        if not d.empty:
            d["indicator_id"] = "fx_debt_share"
            d["value"] = d["fx_debt_usd"] / d["ext_debt_nonres_usd"] * 100.0
            d["indicator_name"] = "FX-denominated share of external debt (IMF IIPCC)"
            d["pillar"] = "markets"
            d["orientation"] = 0
            d["source"] = "derived"
            d["provider_dataset"] = "IIPCC(derived)"
            d["provider_code"] = "DLNRES_DIC.FC/_T*100"
            d["unit"] = "percent"
            d["frequency"] = "A"
            d["updated_at"] = pd.NaT
            frames.append(d[_EXT_COLUMNS])

    if not frames:
        return _empty_ext()
    return pd.concat(frames, ignore_index=True)


def load_panel_ext(ref_date, hub_db_path: Optional[str] = None) -> pd.DataFrame:
    """Point-in-time (ref_date < today) or live (ref_date >= today) read of
    the full v_macro_panel_ext shape. See module docstring for the asof
    reconstruction and its vintage-log-coverage caveat."""
    if not used_asof_path(ref_date):
        return read_macro_panel_ext(db_path=hub_db_path)

    asof = str(ref_date)
    native = _native_panel_asof(asof, hub_db_path)
    bond = _bond_yield_10y_asof(asof, hub_db_path)
    derived = _derived_from_native(native)

    frames = [f for f in (native, bond, derived) if not f.empty]
    if not frames:
        return _empty_ext()
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["indicator_id", "country_iso3", "date"]).reset_index(drop=True)

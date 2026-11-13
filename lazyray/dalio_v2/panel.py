# -*- coding: utf-8 -*-
"""Extended panel reads with resolved monthly sovereign yields.

Native historical indicators use hub vintages; resolved rates are observation-date
history, not point-in-time release or revision history.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Optional

import pandas as pd
from market_data_hub.config_loader import get_countries, get_macro_panel_specs
from market_data_hub.reader import (
    read_macro_panel,
    read_macro_panel_ext,
    read_resolved_history,
)
from market_data_hub.resolved_rates import load_resolved_rates

_EXT_COLUMNS = [
    "date",
    "country_iso3",
    "indicator_id",
    "value",
    "indicator_name",
    "pillar",
    "orientation",
    "source",
    "provider_dataset",
    "provider_code",
    "unit",
    "frequency",
    "updated_at",
]


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


def _bond_yield_10y(asof: str, hub_db_path: Optional[str]) -> pd.DataFrame:
    # Resolved history is not point-in-time: its cutoff is observation date only.
    _, concepts = load_resolved_rates()
    frames = []
    for country in get_countries():
        concept = f"{country.get('iso2')}_10Y"
        if concept not in concepts or not country.get("iso3"):
            continue
        history = read_resolved_history(concept, end=asof, db_path=hub_db_path)
        if history is None or history.empty:
            continue
        history = history.dropna(subset=["value"]).copy()
        if history.empty:
            continue
        history["date"] = pd.to_datetime(history["date"])
        history = (
            history.sort_values("date")
            .groupby(history["date"].dt.to_period("M"), sort=True)
            .tail(1)
            .copy()
        )
        # Label completed months at month-end; retain the cutoff for a partial month.
        history["date"] = (
            history["date"]
            .dt.to_period("M")
            .dt.to_timestamp("M")
            .clip(upper=pd.Timestamp(asof))
        )
        history["country_iso3"] = country["iso3"]
        history["indicator_id"] = "bond_yield_10y"
        history["indicator_name"] = "10Y government bond yield"
        history["pillar"] = "markets"
        history["orientation"] = 0
        history["source"] = "resolved_rates"
        history["provider_dataset"] = "resolved_rates"
        history["provider_code"] = concept
        history["unit"] = "percent"
        history["frequency"] = "M"
        history["updated_at"] = pd.NaT
        frames.append(history[_EXT_COLUMNS])
    return pd.concat(frames, ignore_index=True) if frames else _empty_ext()


def _derived_from_native(native_raw: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time reproduction of v_macro_panel_ext's two per-(date,
    country) derived branches (implied_interest_rate, fx_debt_share) from
    the SAME point-in-time native rows _native_panel_asof() already read --
    never a second, independently-vintaged read of the source indicators."""
    if native_raw.empty:
        return _empty_ext()
    need = {
        "interest_on_debt_gdp",
        "public_debt_gdp",
        "fx_debt_usd",
        "ext_debt_nonres_usd",
    }
    sub = native_raw[native_raw["indicator_id"].isin(need)]
    if sub.empty:
        return _empty_ext()
    wide = sub.pivot_table(
        index=["date", "country_iso3"],
        columns="indicator_id",
        values="value",
        aggfunc="last",
    ).reset_index()

    frames = []
    if {"interest_on_debt_gdp", "public_debt_gdp"} <= set(wide.columns):
        d = wide[
            ["date", "country_iso3", "interest_on_debt_gdp", "public_debt_gdp"]
        ].copy()
        d = d.dropna(subset=["interest_on_debt_gdp"])
        d = d[d["public_debt_gdp"] > 0]
        if not d.empty:
            d["indicator_id"] = "implied_interest_rate"
            d["value"] = d["interest_on_debt_gdp"] / d["public_debt_gdp"] * 100.0
            d["indicator_name"] = (
                "Implied interest rate on govt debt (IMF interest %GDP ÷ debt %GDP)"
            )
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


def replace_bond_yields(
    panel: pd.DataFrame, ref_date, hub_db_path: Optional[str] = None
) -> pd.DataFrame:
    """Replace the hub view's bond rows with monthly resolved histories."""
    panel = (
        panel[panel["indicator_id"] != "bond_yield_10y"].copy()
        if not panel.empty
        else _empty_ext()
    )
    bond = _bond_yield_10y(str(ref_date), hub_db_path)
    frames = [f for f in (panel, bond) if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else _empty_ext()


def load_panel_ext(ref_date, hub_db_path: Optional[str] = None) -> pd.DataFrame:
    """Read the extended panel, replacing yields with resolved history."""
    if used_asof_path(ref_date):
        native = _native_panel_asof(str(ref_date), hub_db_path)
        frames = [f for f in (native, _derived_from_native(native)) if not f.empty]
        panel = pd.concat(frames, ignore_index=True) if frames else _empty_ext()
    else:
        panel = read_macro_panel_ext(db_path=hub_db_path)
    out = replace_bond_yields(panel, ref_date, hub_db_path)
    if not out.empty:
        out["date"] = pd.to_datetime(out["date"])
        out = out.sort_values(["indicator_id", "country_iso3", "date"]).reset_index(
            drop=True
        )
    return out

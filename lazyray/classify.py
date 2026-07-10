# -*- coding: utf-8 -*-
"""
classify.py — country classification on two levels:
  - STATIC     : geography, development (DM/EM/Frontier), income, FX regime,
                IMF program, G7/EU/ASEAN flags (from config/countries.yaml)
  - DATA-DRIVEN: energy position (oil exporter/importer), dependence on
                natural resources, tourism, remittances — derived from macro_panel.

Writes the country_classification table (static + data-driven, one row per
country), recomputed on every run.

Oil/energy formula (corrected relative to FRONTIER, which used a single
denominator): fuel imports are scaled on IMPORTS/GDP, exports on EXPORTS/GDP.
    net_fuel_gdp = fuel_exp%·(export/GDP)/100  −  fuel_imp%·(import/GDP)/100

Input (macro_panel) is read from market-data-hub's public
``reader.read_macro_panel()`` API. Output (country_classification) is written
to LazyRay's own DB via ``lazyray.db.connection.get_conn()``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
from market_data_hub.config_loader import get_countries
from market_data_hub.reader import read_macro_panel

from lazyray.db.connection import get_conn

# macro_panel indicators used for the data-driven classification
_NEED = ["fuel_exports_share", "fuel_imports_share", "exports_gdp", "imports_gdp",
         "natural_resource_rents_gdp", "tourism_exports_share", "remittances_gdp",
         "metals_exports_share"]


def _bucket(v, edges, labels, unknown="unknown"):
    """Assign v to a bucket given the increasing edges and the labels (len+1)."""
    if v is None or pd.isna(v):
        return unknown
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def energy_position(fe, fi, ex, im):
    """Energy position. Primary: net fuel %GDP (corrected formula). If
    export/import %GDP are missing (e.g. Nigeria in WDI), fall back on the
    SHARES: if fuel dominates exports the country is an exporter anyway.
    Returns (label, net_fuel_gdp | None)."""
    if None not in (fe, fi, ex, im):
        net = fe * ex / 100 - fi * im / 100
        return _bucket(net, [-10, -3, 3, 10],
                      ["strong_importer", "importer", "neutral", "exporter",
                       "strong_exporter"]), round(net, 2)
    # --- fallback on export shares (no GDP scaling available) ---
    if fe is not None:
        if fe > 50:
            return "strong_exporter", None      # exports dominated by fuel
        if fe > 20:
            return "exporter", None
        if fi is not None and fi > 15 and fe < 10:
            return "importer", None             # imports fuel, does not export
        return "neutral", None
    return "unknown", None


def resource_dependence(rents):
    return _bucket(rents, [5, 20], ["diversified", "resource_significant", "resource_driven"])


def tourism_dependence(t):
    return _bucket(t, [5, 15, 30], ["low", "moderate", "significant", "dominant"])


def remittance_dependence(r):
    return _bucket(r, [3, 10], ["low", "moderate", "high"])


def classify_countries(db_path: Optional[str] = None,
                       ref_year: Optional[int] = None,
                       hub_db_path: Optional[str] = None) -> dict:
    """
    db_path     -- LazyRay's own DuckDB file (output: country_classification).
    hub_db_path -- market-data-hub's DuckDB file (input, read-only).
    """
    now = datetime.now(timezone.utc)
    ry = ref_year or now.year

    # latest value <= current year for each required (country, indicator)
    df = read_macro_panel(indicators=_NEED, end=f"{ry}-12-31",
                          db_path=hub_db_path)
    df = df[["country_iso3", "indicator_id", "date", "value"]]
    df = df[df["value"].notna()]
    df = df.sort_values("date").drop_duplicates(
        ["country_iso3", "indicator_id"], keep="last")
    latest: Dict[str, Dict[str, float]] = {}
    for _, r in df.iterrows():
        latest.setdefault(r["country_iso3"], {})[r["indicator_id"]] = r["value"]

    countries = get_countries()
    rows = []
    for c in countries:
        iso = c["iso3"]
        d = latest.get(iso, {})

        def g(k):
            v = d.get(k)
            return None if v is None or pd.isna(v) else float(v)

        fe, fi = g("fuel_exports_share"), g("fuel_imports_share")
        ex, im = g("exports_gdp"), g("imports_gdp")
        energy, net_fuel = energy_position(fe, fi, ex, im)
        rents = g("natural_resource_rents_gdp")
        # sanity: an "exporter" with no natural-resource rents (~0) is NOT a
        # producer but a re-export/bunkering hub (e.g. Cyprus) -> downgrade to
        # neutral. Real exporters have rents > ~2% GDP.
        if energy in ("exporter", "strong_exporter") and rents is not None and rents < 2.0:
            energy = "neutral"
        tour = g("tourism_exports_share")
        remit = g("remittances_gdp")

        rows.append((
            iso, c.get("name", iso),
            # --- static ---
            c.get("region_group", ""), c.get("region_geo", ""),
            c.get("income", ""), c.get("development", ""),
            c.get("fx_regime", ""), bool(c.get("imf_program", False)),
            bool(c.get("g7", False)), bool(c.get("eu", False)),
            bool(c.get("euro", False)),
            # --- data-driven ---
            energy,
            net_fuel,
            resource_dependence(rents),
            tourism_dependence(tour),
            remittance_dependence(remit),
            now,
        ))

    con = get_conn(db_path)
    con.execute("DROP TABLE IF EXISTS country_classification")
    con.execute("""CREATE TABLE country_classification (
        country_iso3 VARCHAR PRIMARY KEY, name VARCHAR,
        region_group VARCHAR, region_geo VARCHAR, income VARCHAR,
        development VARCHAR, fx_regime VARCHAR, imf_program BOOLEAN,
        g7 BOOLEAN, eu BOOLEAN, euro BOOLEAN,
        energy_position VARCHAR, net_fuel_gdp DOUBLE,
        resource_dependence VARCHAR, tourism_dependence VARCHAR,
        remittance_dependence VARCHAR, computed_at TIMESTAMP)""")
    con.executemany(
        "INSERT INTO country_classification VALUES (" + ",".join("?" * 17) + ")", rows)
    con.commit()

    summ = pd.DataFrame(rows, columns=[
        "iso", "name", "rg", "geo", "inc", "dev", "fx", "imf", "g7", "eu", "euro",
        "energy", "netfuel", "res", "tour", "remit", "t"])
    out = {
        "countries": len(rows),
        "development": summ["dev"].value_counts().to_dict(),
        "energy": summ["energy"].value_counts().to_dict(),
        "resource": summ["res"].value_counts().to_dict(),
    }
    con.close()
    return out

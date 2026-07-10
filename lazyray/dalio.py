# -*- coding: utf-8 -*-
"""
dalio.py — "Ray Dalio" analytical layer on top of macro_panel.

Implements Part A of the Dalio v3 spec:
  - z-score signals (x direction) per (country, indicator), 10y window
  - derived calculations: debt-to-income gap, nominal growth vs nominal rate,
    productivity trend, growth/inflation delta (surprises vs trend)
  - debt cycle phase classifier (7 phases, configurable thresholds)
  - four-box growth/inflation regime (Q1-Q4)
  - pillar_scores + weighted composite + 3 categorical labels

Writes: dalio_signals, pillar_scores, regime_state.
Robust: missing indicators -> NaN / INDETERMINATE phase, never crashes.

Usage:
    from lazyray.dalio import run_dalio
    run_dalio()                       # computes and writes to LazyRay's own DB
    run_dalio(ref_year=2026)          # specific reference date

Input (macro_panel + FRED remap) is read from market-data-hub's public
``reader.read_macro_panel_ext()`` API — this module never touches the hub's
DuckDB file directly. Output (dalio_signals/pillar_scores/regime_state) is
written to LazyRay's own DB via ``lazyray.db.connection.get_conn()``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from typing_extensions import TypeGuard

import numpy as np
import pandas as pd
from market_data_hub.reader import read_macro_panel_ext

from lazyray.config_loader import get_settings
from lazyray.db.connection import get_conn

# map of key Dalio indicators -> id in macro_panel (with fallback)
IND = {
    "credit_gap": "bis_credit_gap",
    "dsr": "bis_dsr_private",
    # Two DISTINCT rate series, split by what "r" means at each consumption site:
    #  - policy_rate: the central-bank POLICY stance (BIS, IMF MFS fallback).
    #    Reads that ask "can the CB still ease?" — rate_near_zero /
    #    PUSHING_ON_STRING and the deleveraging-quality test — must use this
    #    one: the implied stock rate never approaches zero.
    #  - implied_rate: the effective rate on the debt STOCK (IMF
    #    implied_interest_rate = interest %GDP ÷ debt), a truer "r" for the
    #    r-vs-g debt-dynamics test ONLY, and — unlike the ECB policy rate
    #    shared by the whole euro area — country-specific (Greece != DE).
    #    Falls back to the policy rate where the implied rate is missing.
    "policy_rate": ["bis_policy_rate", "imf_policy_rate"],
    "implied_rate": ["implied_interest_rate", "bis_policy_rate", "imf_policy_rate"],
    "pub_debt": "public_debt_gdp",
    "fiscal": ["fiscal_balance_gdp"],
    "growth": ["gdp_growth_weo", "real_gdp_growth"],
    "inflation": ["inflation_avg_weo", "inflation_cpi"],
    "productivity": ["labor_productivity_level", "gdp_per_capita_growth"],
}


def _first_avail(series_by_ind: Dict[str, pd.DataFrame], ids):
    """Returns the first available (non-empty) series among a list of ids."""
    if isinstance(ids, str):
        ids = [ids]
    for i in ids:
        s = series_by_ind.get(i)
        if s is not None and not s.empty:
            return s
    return None


def _latest(s: Optional[pd.DataFrame]):
    if s is None or s.empty:
        return np.nan, None
    r = s.sort_values("date").iloc[-1]
    return float(r["value"]), pd.Timestamp(r["date"])


def _orient(v) -> int:
    """Indicator orientation as int; NULL/NaN (truthy in `v or 0`!) becomes 0."""
    return 0 if pd.isna(v) else int(v)


def _prev(s: Optional[pd.DataFrame]):
    """Previous value (second-to-last annual observation)."""
    if s is None or len(s) < 2:
        return np.nan
    return float(s.sort_values("date").iloc[-2]["value"])


def _pct_in_range(s: Optional[pd.DataFrame]):
    """Position of the current value in the historical range [0,1]. For the DSR:
    ~1 = at the historical peak (stress, Dalio reading). None if insufficient history."""
    if s is None or s.empty:
        return float("nan")
    v = s.sort_values("date")["value"].dropna()
    if len(v) < 8:
        return float("nan")
    lo, hi = v.min(), v.max()
    if hi <= lo:
        return float("nan")
    return float((v.iloc[-1] - lo) / (hi - lo))


def _slope(s: Optional[pd.DataFrame], y_lo: int, y_hi: int):
    """OLS slope (pp/year) of value vs year in the window [y_lo, y_hi].
    Used for the debt/GDP TRAJECTORY (includes WEO forecasts)."""
    if s is None or s.empty:
        return float("nan")
    d = s.copy()
    d["y"] = pd.to_datetime(d["date"]).dt.year
    d = d[(d["y"] >= y_lo) & (d["y"] <= y_hi)].dropna(subset=["value"])
    if len(d) < 3:
        return float("nan")
    import numpy as _np
    x = d["y"].values - d["y"].values.min()
    return float(_np.polyfit(x, d["value"].values, 1)[0])


def classify_regime(growth_delta, infl_delta):
    """Four-box Bridgewater: Q1 g+/i-, Q2 g+/i+, Q3 g-/i+, Q4 g-/i-."""
    if pd.isna(growth_delta) or pd.isna(infl_delta):
        return None
    g_up = growth_delta >= 0
    i_up = infl_delta >= 0
    if g_up and not i_up:
        return "Q1"   # growth up, inflation down -> risk assets
    if g_up and i_up:
        return "Q2"   # reflation -> commodities, EM, linkers
    if not g_up and i_up:
        return "Q3"   # stagflation -> gold, defensive
    return "Q4"        # disinflation/recession -> bonds, cash


def classify_cycle_phase(x: dict, th: dict) -> str:
    """Threshold tree of the debt cycle (Dalio, extended spec 2.1).

    Extends the spec logic with the "normal" phases that would otherwise fall
    into INDETERMINATE: healthy expansion, late-leveraging (toward the bubble),
    mild contraction, ugly deleveraging. INDETERMINATE remains only when the
    base data (growth AND credit_gap) are both missing.
    """
    cg = x.get("credit_gap")
    g = x.get("growth")
    gn = x.get("nom_growth")
    rn = x.get("nom_rate")           # effective rate on the debt stock (r-vs-g)
    pr = x.get("policy_rate")        # central-bank policy rate (monetary stance)
    dsr = x.get("dsr")
    debt_falling = x.get("debt_falling")
    debt_lvl = x.get("debt_level")
    fisc = x.get("fiscal_balance")
    dtrend = x.get("debt_trend")          # debt/GDP trajectory (pp/year)

    def has(v) -> "TypeGuard[float]":
        return v is not None and not pd.isna(v)

    # debt is RISING if the multi-year trajectory is positive
    debt_rising = has(dtrend) and dtrend > th["debt_trend_moderate"]
    debt_rising_fast = has(dtrend) and dtrend > th["debt_trend_high"]

    # need at least growth OR credit_gap, otherwise nothing can be said
    if not has(g) and not has(cg):
        return "INDETERMINATE"

    # 1) contraction / depression (negative growth). DEPRESSION if the debt
    #    service ratio is at the PEAK of its history (Dalio), not at an absolute
    #    level: 20% is normal for CH/NL, high for IT. Absolute fallback if
    #    history is missing.
    dsr_pct = x.get("dsr_pct")
    dsr_stressed = (dsr_pct > th["dsr_peak_pct"]) if has(dsr_pct) \
        else (has(dsr) and dsr > th["dsr_high"])
    if has(g) and g < 0:
        return "DEPRESSION" if dsr_stressed else "CONTRACTION"

    # 2) deleveraging (debt/GDP in sustained DECLINE): beautiful vs ugly
    #    Uses the multi-year trajectory, not the 1-year change.
    #    Takes precedence -> Japan/Greece (high but declining debt) here.
    if (debt_falling or (has(dtrend) and dtrend < -th["debt_trend_moderate"])) \
            and has(gn) and has(rn):
        return "BEAUTIFUL_DELEVERAGING" if gn > rn else "UGLY_DELEVERAGING"

    # 3) LONG/sovereign debt cycle (Dalio's key thesis): high debt
    #    (>100% GDP) that is not declining is NOT a "healthy expansion". But the
    #    gradation is distinguished:
    #      LATE_LONG_CYCLE   = very high debt (>130%) OR deteriorating
    #                          (rising or with a large deficit) -> toward the reckoning
    #      HIGH_DEBT_STABLE  = high but stabilized/plateaued debt, not worsening
    if has(debt_lvl) and not debt_falling and debt_lvl > th["debt_high_level"]:
        deteriorating = debt_rising or (has(fisc) and fisc < th["deficit_large"])
        if debt_lvl > th["debt_crisis_level"] or deteriorating:
            return "LATE_LONG_CYCLE"
        return "HIGH_DEBT_STABLE"

    # 4) bubble / leveraging up (short-term private credit + debt trajectory)
    if has(cg) and cg > th["credit_gap_bubble"]:
        return "BUBBLE"
    if (has(cg) and cg > th["credit_gap_late"]) or debt_rising_fast:
        return "LATE_LEVERAGING"

    # 4) exhausted monetary policy: POLICY rate ~0 and weak growth. Must read
    #    the policy rate, not the implied stock rate (which never nears zero).
    if has(pr) and pr < th["rate_near_zero"] and has(g) and g < th["weak_growth"]:
        return "PUSHING_ON_STRING"

    # 5) healthy expansion (the "normal" case: positive growth, no excesses)
    if has(g) and g > 0:
        return "EARLY_EXPANSION"

    return "INDETERMINATE"


def _deleveraging_quality(g, gn, rn, debt_falling):
    """Deleveraging quality — only makes sense IF the debt is falling.
    If debt is not falling, there is NO deleveraging -> NA (no contradictions
    like 'EARLY_EXPANSION + UGLY'). Beautiful: nominal growth > nominal rate;
    Ugly: nominal growth < nominal rate (painful/deflationary reduction).
    `rn` here is the POLICY rate (monetary conditions during the deleveraging),
    not the implied stock rate."""
    if not debt_falling:
        return "NA"
    if pd.isna(gn) or pd.isna(rn):
        return "NA"
    return "BEAUTIFUL" if gn > rn else "UGLY"


def run_dalio(db_path: Optional[str] = None, ref_year: Optional[int] = None,
             hub_db_path: Optional[str] = None) -> dict:
    """
    db_path     -- LazyRay's own DuckDB file (output: dalio_signals/
                   pillar_scores/regime_state). Resolution: arg -> LAZYRAY_DB
                   env -> lazyray settings.yaml -> repo-local default.
    hub_db_path -- market-data-hub's DuckDB file (input, read-only via
                   reader.read_macro_panel_ext). Resolution: arg ->
                   MARKET_DATA_DB env -> hub's own settings.yaml default.
    """
    cfg = get_settings().get("dalio", {})
    z_pos, z_neg = cfg.get("z_pos", 1.0), cfg.get("z_neg", -1.0)
    weights = cfg.get("pillar_weights", {})
    th = {k: cfg.get(k) for k in (
        "credit_gap_bubble", "dsr_high", "rate_near_zero",
        "credit_gap_late", "weak_growth",
        "debt_high_level", "debt_crisis_level", "deficit_large",
        "debt_trend_high", "debt_trend_moderate", "dsr_peak_pct")}
    tw_back = cfg.get("debt_trend_window_back", 3)
    tw_fwd = cfg.get("debt_trend_window_fwd", 5)

    # Read from v_macro_panel_ext (macro_panel + FRED single-country series
    # remapped into panel shape), so cross-country FRED inputs (e.g. the 10Y
    # bond yield) are visible to the Dalio layer alongside the native panel.
    # New indicators sit on the unweighted 'markets' pillar, so the composite
    # is unchanged until they are explicitly wired into the methodology.
    panel = read_macro_panel_ext(db_path=hub_db_path)
    if panel.empty:
        raise RuntimeError("macro_panel empty: run the hub's download first.")
    panel["date"] = pd.to_datetime(panel["date"])
    now = datetime.now(timezone.utc)

    # loosely typed: each list holds tuples of differing shapes per row kind
    # (e.g. pil_rows: per-pillar rows vs the COMPOSITE summary row)
    sig_rows: list = []
    pil_rows: list = []
    reg_rows: list = []

    # ref_year = CURRENT year (not the max=latest forecast!). The country state
    # metrics must be computed on the present; forecasts (> ref_year) remain
    # available for the charts but are NOT the "current value".
    ry = ref_year or now.year
    ref_date = pd.Timestamp(ry, 12, 31)

    # --- CROSS-COUNTRY z-score (relative strength vs the other countries) ---
    # For each indicator: latest value <= ref for each country, then z across
    # countries (x-mean)/std x direction, clipped to [-3,3]. This is what makes
    # the composite a country STRENGTH score (Switzerland high, stressed EM low),
    # not a "vs its own history". The temporal z remains for the cyclical
    # readings (e.g. dsr_pct, already separate).
    pref = panel[panel["date"] <= ref_date]
    xz = {}          # indicator -> {country: z_cross}
    ind_meta = {}    # indicator -> (pillar, orientation)
    for ind, g in pref.groupby("indicator_id"):
        last = g.sort_values("date").groupby("country_iso3").tail(1)
        vals = dict(zip(last["country_iso3"], last["value"]))
        orient = _orient(g["orientation"].iloc[-1])
        ind_meta[ind] = (g["pillar"].iloc[-1], orient)
        # orientation 0 = staged/derived input (bond_yield_10y, implied rate,
        # fx_debt_share, ...), NOT a strength signal: coercing it to +1 would
        # write wrong POS/NEG rows (highest 10Y yield = "strength"). No z ->
        # the signal row stays NEUTRAL and the pillar mean skips it.
        if orient == 0:
            continue
        arr = np.array([v for v in vals.values()], dtype=float)
        mean, std = np.nanmean(arr), np.nanstd(arr)
        if not std or np.isnan(std):
            xz[ind] = {c: 0.0 for c in vals}
        else:
            xz[ind] = {c: float(np.clip((v - mean) / std * orient, -3, 3))
                       for c, v in vals.items()}

    for country, cdf_full in panel.groupby("country_iso3"):
        cdf = cdf_full[cdf_full["date"] <= ref_date]
        if cdf.empty:
            continue
        # FULL debt/GDP series (incl. forecast) for the trajectory
        debt_full = cdf_full[cdf_full["indicator_id"] == IND["pub_debt"]][["date", "value"]]
        debt_trend = _slope(debt_full, ry - tw_back, ry + tw_fwd)
        # z-score window by frequency (10y): A->10, Q->40, M->120
        by_ind = {i: g[["date", "value"]] for i, g in cdf.groupby("indicator_id")}
        meta = {i: (g["pillar"].iloc[-1], _orient(g["orientation"].iloc[-1]),
                    g["frequency"].iloc[-1])
                for i, g in cdf.groupby("indicator_id")}

        # ---- signals: CROSS-COUNTRY z-score for each indicator ----
        for ind, s in by_ind.items():
            pillar, orient, freq = meta[ind]
            z = xz.get(ind, {}).get(country)
            val, _ = _latest(s)
            sgl = ("POS" if (z is not None and z >= z_pos) else
                   "NEG" if (z is not None and z <= z_neg) else "NEUTRAL")
            sig_rows.append((country, ref_date.date(), ind, pillar, val,
                             z, len(xz.get(ind, {})), sgl, now))

        # ---- derived Dalio calculations ----
        s_cg = _first_avail(by_ind, IND["credit_gap"])
        s_dsr = _first_avail(by_ind, IND["dsr"])
        s_rate = _first_avail(by_ind, IND["policy_rate"])
        s_impl = _first_avail(by_ind, IND["implied_rate"])
        s_debt = _first_avail(by_ind, IND["pub_debt"])
        s_fisc = _first_avail(by_ind, IND["fiscal"])
        s_g = _first_avail(by_ind, IND["growth"])
        s_i = _first_avail(by_ind, IND["inflation"])

        credit_gap, _ = _latest(s_cg)
        dsr, _ = _latest(s_dsr)
        dsr_pct = _pct_in_range(s_dsr)   # DSR position vs its history (Dalio)
        policy_rate, _ = _latest(s_rate)
        nom_rate, _ = _latest(s_impl)    # stock rate: the r of the r-vs-g test
        debt, _ = _latest(s_debt)
        debt_prev = _prev(s_debt)
        fiscal_balance, _ = _latest(s_fisc)
        growth, _ = _latest(s_g)
        infl, _ = _latest(s_i)

        debt_income_gap = (debt - debt_prev) if not (pd.isna(debt) or pd.isna(debt_prev)) else np.nan
        # "debt falling" = multi-year TRAJECTORY declining (not 1 year)
        debt_falling = (not pd.isna(debt_trend)) and debt_trend < 0
        nom_growth = (growth + infl) if not (pd.isna(growth) or pd.isna(infl)) else np.nan
        # four-box: current growth/inflation vs its OWN POTENTIAL/trend
        # (output gap), not YoY momentum. The potential = medium-term WEO mean
        # [ry+2, ry+5] (IMF estimate of equilibrium, immune to COVID).
        # So a high-growth country ABOVE its potential is "growth up"
        # (e.g. Vietnam), a low-growth one below its trend is "down" (Italy).
        def _potential(ind_ids):
            for iid in ([ind_ids] if isinstance(ind_ids, str) else ind_ids):
                fs = cdf_full[cdf_full["indicator_id"] == iid]
                if fs.empty:
                    continue
                yy = pd.to_datetime(fs["date"]).dt.year
                med = fs[(yy >= ry + 2) & (yy <= ry + 5)]["value"].dropna()
                if len(med) >= 2:
                    return float(med.mean())
            return np.nan
        # growth: vs POTENTIAL (output gap -> strong/weak)
        pot_g = _potential(IND["growth"])
        growth_delta = (growth - pot_g) if not (pd.isna(growth) or pd.isna(pot_g)) else np.nan
        # inflation: vs mean of the 3 PRIOR years (direction -> reflation/
        # disinflation). The WEO potential assumes disinflation and would make
        # almost everyone "rising"; the recent direction is more informative.
        def _prior3(s):
            if s is None or s.empty:
                return np.nan
            yy = pd.to_datetime(s["date"]).dt.year
            v = s[(yy >= ry - 3) & (yy <= ry - 1)]["value"].dropna()
            return float(v.mean()) if len(v) >= 2 else np.nan
        infl_prior = _prior3(s_i)
        infl_delta = (infl - infl_prior) if not (pd.isna(infl) or pd.isna(infl_prior)) else np.nan

        phase = classify_cycle_phase({
            "credit_gap": credit_gap, "debt_income_gap": debt_income_gap,
            "debt_level": debt, "fiscal_balance": fiscal_balance,
            "debt_trend": debt_trend,
            "growth": growth, "nom_growth": nom_growth, "nom_rate": nom_rate,
            "policy_rate": policy_rate,
            "dsr": dsr, "dsr_pct": dsr_pct, "debt_falling": debt_falling}, th)
        quadrant = classify_regime(growth_delta, infl_delta)
        delev = _deleveraging_quality(growth, nom_growth, policy_rate, debt_falling)

        reg_rows.append((country, ref_date.date(), growth_delta, infl_delta,
                         quadrant, phase, nom_growth, nom_rate, delev,
                         credit_gap, dsr, debt_income_gap,
                         None if pd.isna(debt_trend) else round(debt_trend, 2), now))

        # ---- pillar_scores + composite (CROSS-COUNTRY z) ----
        zdf = pd.DataFrame([(meta[i][0], xz.get(i, {}).get(country))
                            for i in by_ind], columns=["pillar", "z"]).dropna()
        comp_num = comp_den = 0.0
        for pillar, pdf in zdf.groupby("pillar"):
            sc = pdf["z"].mean()
            pil_rows.append((country, ref_date.date(), pillar, sc, len(pdf),
                             None, None, None, now))
            w = weights.get(pillar, 0)
            if w and not pd.isna(sc):
                comp_num += w * sc
                comp_den += w
        composite = comp_num / comp_den if comp_den else np.nan
        # short cycle: simple proxy from growth_delta
        short = ("late/contraction" if (not pd.isna(growth_delta) and growth_delta < 0)
                 else "mid/late upswing" if not pd.isna(growth_delta) else None)
        pil_rows.append((country, ref_date.date(), "COMPOSITE",
                         None if pd.isna(composite) else composite, len(zdf),
                         phase, short, quadrant, now))

    # ---- idempotent write (tables come from schema.sql, applied by get_conn) ----
    con = get_conn(db_path)
    con.execute("DELETE FROM regime_state")
    con.execute("DELETE FROM dalio_signals")
    con.execute("DELETE FROM pillar_scores")
    con.executemany(
        "INSERT OR REPLACE INTO dalio_signals VALUES (?,?,?,?,?,?,?,?,?)", sig_rows)
    con.executemany(
        "INSERT OR REPLACE INTO pillar_scores VALUES (?,?,?,?,?,?,?,?,?)", pil_rows)
    con.executemany(
        "INSERT OR REPLACE INTO regime_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", reg_rows)
    con.commit()

    n_countries = panel["country_iso3"].nunique()
    phases = pd.DataFrame(reg_rows, columns=[
        "c", "d", "gd", "id_", "q", "phase", "ng", "nr", "dq", "cg", "dsr", "dig", "dtr", "t"])

    # --- WEO forecast freshness: the horizon must exceed the current year ---
    weo = panel[panel["indicator_id"].isin(["gdp_growth_weo", "public_debt_gdp"])]
    weo_horizon = int(pd.to_datetime(weo["date"]).dt.year.max()) if not weo.empty else None
    cur_year = datetime.now(timezone.utc).year
    forecast_stale = weo_horizon is None or weo_horizon < cur_year + 1

    summary = {
        "countries": n_countries,
        "signals": len(sig_rows),
        "phases": phases["phase"].value_counts().to_dict(),
        "regimes": phases["q"].value_counts(dropna=True).to_dict(),
        "weo_horizon": weo_horizon,
        "forecast_stale": forecast_stale,
    }
    con.close()
    return summary

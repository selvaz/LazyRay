# -*- coding: utf-8 -*-
"""
test_dalio_dsa.py — WP2 deliverable D: lazyray.dalio_v2.dsa's pure
Monte-Carlo debt-dynamics simulation. No DB access; see
tests/test_dalio_v2.py for how sovereign_solvency.py wires the panel's
annual history into build_annual_history()/simulate()/summarize().
"""
from __future__ import annotations

import numpy as np

from lazyray.dalio_v2 import dsa

_YEARS = list(range(2000, 2026))   # 26 years, well above MIN_OBS=12


def test_build_annual_history_pairs_only_years_with_all_three_inputs():
    r = {y: 3.0 for y in _YEARS}
    g = {y: 1.0 for y in _YEARS}
    pb = {y: -2.0 for y in _YEARS if y != 2010}   # one gap year
    rg, pb_arr = dsa.build_annual_history(r, g, pb, 2025)
    assert len(rg) == len(pb_arr) == len(_YEARS) - 1
    assert rg[0] == 3.0 - 1.0


def test_build_annual_history_respects_start_and_last_actual_year():
    r = {y: 3.0 for y in range(1990, 2026)}
    g = {y: 1.0 for y in range(1990, 2026)}
    pb = {y: -2.0 for y in range(1990, 2026)}
    rg, _ = dsa.build_annual_history(r, g, pb, 2020, start_year=2005)
    assert len(rg) == 2020 - 2005 + 1   # 2005..2020 inclusive, not 1990.. or ..2025


def test_simulate_returns_none_below_min_obs():
    rg = np.array([1.0] * (dsa.MIN_OBS - 1))
    pb = np.array([1.0] * (dsa.MIN_OBS - 1))
    assert dsa.simulate(80.0, 3.0, 1.0, -2.0, rg, pb) is None
    assert dsa.summarize(None, 80.0) is None


def test_deterministic_with_fixed_seed():
    r = {y: 3.0 for y in _YEARS}
    g = {y: 1.0 for y in _YEARS}
    pb = {y: -2.0 for y in _YEARS}
    rg_hist, pb_hist = dsa.build_annual_history(r, g, pb, 2025)
    a = dsa.simulate(80.0, 3.0, 1.0, -2.0, rg_hist, pb_hist)
    b = dsa.simulate(80.0, 3.0, 1.0, -2.0, rg_hist, pb_hist)
    assert np.array_equal(a, b)


def test_r_above_g_with_deficit_pushes_p_up_near_one():
    # persistent r > g and a primary deficit: debt/GDP must almost surely
    # rise over 5 years -- with a zero-variance history (constant r/g/pb),
    # the shocks are degenerate and every path takes the SAME deterministic
    # route, so p_up must land exactly at 1.0, not just "near" it.
    r = {y: 6.0 for y in _YEARS}
    g = {y: 1.0 for y in _YEARS}
    pb = {y: -3.0 for y in _YEARS}
    rg_hist, pb_hist = dsa.build_annual_history(r, g, pb, 2025)
    d_paths = dsa.simulate(80.0, 6.0, 1.0, -3.0, rg_hist, pb_hist)
    summary = dsa.summarize(d_paths, 80.0)
    assert summary["p_up"] == 1.0
    assert summary["p50"] > 80.0


def test_r_below_g_with_surplus_pushes_p_up_near_zero():
    r = {y: 1.0 for y in _YEARS}
    g = {y: 5.0 for y in _YEARS}
    pb = {y: 3.0 for y in _YEARS}
    rg_hist, pb_hist = dsa.build_annual_history(r, g, pb, 2025)
    d_paths = dsa.simulate(80.0, 1.0, 5.0, 3.0, rg_hist, pb_hist)
    summary = dsa.summarize(d_paths, 80.0)
    assert summary["p_up"] == 0.0
    assert summary["p50"] < 80.0


def test_summarize_percentiles_are_ordered():
    r = {y: 4.0 + (y % 3) for y in _YEARS}          # some real variation
    g = {y: 2.0 + (y % 2) for y in _YEARS}
    pb = {y: -1.0 + 0.1 * (y % 5) for y in _YEARS}
    rg_hist, pb_hist = dsa.build_annual_history(r, g, pb, 2025)
    d_paths = dsa.simulate(80.0, 5.0, 2.0, -1.0, rg_hist, pb_hist)
    summary = dsa.summarize(d_paths, 80.0)
    assert summary["p10"] <= summary["p50"] <= summary["p90"]
    assert 0.0 <= summary["p_up"] <= 1.0
    assert len(d_paths) == dsa.N_PATHS

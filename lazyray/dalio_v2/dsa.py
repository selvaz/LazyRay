# -*- coding: utf-8 -*-
"""
dsa.py — sovereign debt-sustainability-analysis (DSA) fan-chart simulation
(docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.5, "DSA" row: the
eu-debt-sustainability-analysis / IMF SRDSF references). Feeds
sovereign_solvency.py's `debt_p_up_5y` component: the probability that
debt/GDP five years out exceeds the last actual, from a Monte Carlo
simulation of the standard debt-dynamics identity with annual shocks drawn
from the COUNTRY'S OWN historical volatility of (r-g) and the primary
balance -- not a single deterministic "current trend" projection, which is
what WEO's own 5-year debt trajectory already is (sovereign_solvency.py's
debt_trend_5y) and says nothing about the DISTRIBUTION of outcomes.

Identity (annual, r/g/pb all in percent; pb positive = surplus):
    d_{t+1} = d_t * (1 + r_t/100) / (1 + g_t/100) - pb_{t+1}
g_t is held at the last-actual nominal growth rate for the whole horizon --
only (r-g) and pb are shocked (per spec), so a shock to the SPREAD moves r
without silently moving g too; r_t is reconstructed each year from a running
(r-g) level for exactly that reason.

Pure functions, no DB/panel access -- see sovereign_solvency.py for how the
annual (r, g, pb) history is assembled from the panel and passed in here.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

MIN_OBS = 12
N_PATHS = 5000
N_YEARS = 5
SEED = 42
_WINSOR_LOW, _WINSOR_HIGH = 0.05, 0.95


def _winsorize(x: np.ndarray) -> np.ndarray:
    lo, hi = np.quantile(x, [_WINSOR_LOW, _WINSOR_HIGH])
    return np.clip(x, lo, hi)


def build_annual_history(r_by_year: dict, g_by_year: dict, pb_by_year: dict,
                         last_actual_year: int, start_year: int = 2000
                         ) -> Tuple[np.ndarray, np.ndarray]:
    """Paired annual (r-g, pb) arrays, years sorted ascending, restricted to
    [start_year, last_actual_year] where all three inputs are present for
    that year. A plain data-assembly step (no minimum-length check here --
    the caller compares len() against MIN_OBS, see sovereign_solvency.py)."""
    years = sorted(y for y in range(start_year, last_actual_year + 1)
                   if y in r_by_year and y in g_by_year and y in pb_by_year)
    rg = np.array([r_by_year[y] - g_by_year[y] for y in years], dtype=float)
    pb = np.array([pb_by_year[y] for y in years], dtype=float)
    return rg, pb


def simulate(d_last: float, r_last: float, g_last: float, pb_last: float,
            rg_history: np.ndarray, pb_history: np.ndarray,
            n_years: int = N_YEARS, n_paths: int = N_PATHS, seed: int = SEED
            ) -> Optional[np.ndarray]:
    """N_PATHS debt/GDP paths, n_years ahead, from the identity in the module
    docstring. Annual shocks to (r-g) and pb are drawn JOINTLY (numpy
    Generator, fixed seed) from a normal fit to the winsorised (5/95) first
    differences of `rg_history`/`pb_history` (paired, same length), then
    accumulated as a random walk: each year's shock adds to a running (r-g)
    level and a running pb level, both starting at `r_last - g_last` /
    `pb_last` -- matching how (r-g) and pb are observed to drift historically,
    rather than reverting to a mean every year. None if fewer than MIN_OBS
    paired history points are given (mirrors the engine's own gate; kept here
    too so this function is safe to call directly, e.g. from a test)."""
    if len(rg_history) < MIN_OBS or len(pb_history) < MIN_OBS:
        return None
    d_rg = _winsorize(np.diff(rg_history))
    d_pb = _winsorize(np.diff(pb_history))
    mean = [float(np.mean(d_rg)), float(np.mean(d_pb))]
    cov = np.cov(np.vstack([d_rg, d_pb]))

    rng = np.random.default_rng(seed)
    shocks = rng.multivariate_normal(mean, cov, size=(n_paths, n_years), check_valid="ignore")

    rg_level = np.full(n_paths, r_last - g_last, dtype=float)
    pb_level = np.full(n_paths, pb_last, dtype=float)
    d_paths = np.full(n_paths, d_last, dtype=float)
    for t in range(n_years):
        rg_level = rg_level + shocks[:, t, 0]
        pb_level = pb_level + shocks[:, t, 1]
        r_t = g_last + rg_level
        d_paths = d_paths * (1.0 + r_t / 100.0) / (1.0 + g_last / 100.0) - pb_level
    return d_paths


def summarize(d_paths: Optional[np.ndarray], d_last: float) -> Optional[dict]:
    """p_up (share of paths with d_{t+5} > d_last) plus p10/p50/p90 of the
    +5y distribution. None if `d_paths` is None (simulate() declined)."""
    if d_paths is None:
        return None
    p10, p50, p90 = np.percentile(d_paths, [10, 50, 90])
    return {"p_up": float(np.mean(d_paths > d_last)),
           "p10": float(p10), "p50": float(p50), "p90": float(p90)}

# -*- coding: utf-8 -*-
"""
scoring.py — shared helpers for the Dalio v2 5-engine architecture.

See docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md (Fase 0/Fase 3 design
decisions) for the rationale: linear threshold-to-score interpolation
instead of crisp if/elif buckets, hysteresis on bucket transitions so a
score oscillating near a boundary does not flip label every run, and an
explicit coverage tier per engine score so partial data is never silently
treated as full coverage. (The plan's robust median/MAD z-score helper was
removed as dead code in the 2026-07 audit -- no engine ended up using
cross-country z's; political_execution uses percentile_rank instead.)
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Sequence, Tuple

import pandas as pd

if TYPE_CHECKING:
    from typing_extensions import TypeGuard


def notna(v: Optional[float]) -> "TypeGuard[float]":
    """True iff v is a real (non-None, non-NaN) float.

    Unlike a bare ``not pd.isna(v)`` check, this narrows Optional[float] to
    float for the type checker in the branch where it's True -- every engine
    does arithmetic on values guarded this way immediately afterward."""
    return v is not None and not pd.isna(v)


def round_or_none(v: Optional[float], ndigits: int = 4) -> Optional[float]:
    """round(float(v), ndigits), or None if v is missing.

    A plain ``None if raw_values[k] is None else round(float(raw_values[k]),
    4)`` re-subscripts the dict a second time inside the same expression,
    which the type checker can't narrow through (it only narrows simple
    names, not repeated subscript/attribute expressions) -- binding through
    a real function call sidesteps that."""
    return round(float(v), ndigits) if notna(v) else None


def percentile_rank(values: pd.Series) -> pd.Series:
    """0-100 cross-country percentile rank (100 = highest raw value)."""
    if values.dropna().empty:
        return pd.Series(float("nan"), index=values.index)
    return values.rank(pct=True) * 100.0


def score_threshold(value: Optional[float], watch: float, stress: float,
                    critical: float, orientation: int = 1) -> Optional[float]:
    """Map a raw value onto a 0-100 risk score by linear interpolation between
    three named thresholds: 0 at/below `watch`, 50 at `stress`, 100 at/above
    `critical`. orientation=-1 flips the direction (a LOW value is worse);
    pass the thresholds in the same order regardless of orientation (watch is
    always the mildest cut point — so for orientation=-1 the raw thresholds
    are DESCENDING, e.g. reserves_months [4, 3, 2]). Thresholds that are not
    strictly ordered after the flip raise: without this check the function
    silently degenerates into a 0/100 cliff at `watch` and the other two
    thresholds are ignored. Returns None if value is missing."""
    if value is None or pd.isna(value):
        return None
    if orientation >= 0:
        v, w, s, c = value, watch, stress, critical
    else:
        v, w, s, c = -value, -watch, -stress, -critical
    if not (w < s < c):
        raise ValueError(
            f"score_threshold: thresholds ({watch}, {stress}, {critical}) with "
            f"orientation={orientation} are not strictly ordered mildest-to-worst")
    if v <= w:
        return 0.0
    if v <= s:
        return 50.0 * (v - w) / (s - w)
    if v <= c:
        return 50.0 + 50.0 * (v - s) / (c - s)
    return 100.0


def weighted_average(components: Dict[str, Optional[float]],
                     weights: Dict[str, float]) -> Tuple[Optional[float], int, int]:
    """Weighted average of the available (non-None) component scores. Missing
    components are simply dropped from the denominator (no silent
    redistribution beyond that) — caller reports n_available/n_expected via
    the returned tuple so coverage_tier() can classify the row honestly.
    A component with zero/unknown weight is excluded from n_available too:
    it contributes nothing to the score, so counting it as coverage would
    let a weight-key typo in settings.yaml silently inflate the tier."""
    num = den = 0.0
    n_avail = 0
    for name, val in components.items():
        if val is None or pd.isna(val):
            continue
        w = weights.get(name, 0.0)
        if w <= 0:
            continue
        num += w * val
        den += w
        n_avail += 1
    score = (num / den) if den else None
    return score, n_avail, len(components)


def coverage_tier(n_available: int, n_expected: int) -> str:
    """'full' if >=80% of expected inputs are present, 'proxy' if >=40%,
    otherwise 'insufficient'."""
    if n_expected <= 0 or n_available <= 0:
        return "insufficient"
    ratio = n_available / n_expected
    if ratio >= 0.8:
        return "full"
    if ratio >= 0.4:
        return "proxy"
    return "insufficient"


def suppress_insufficient(score: Optional[float], tier: str) -> Optional[float]:
    """Enforce the coverage-tier discipline end to end: a row with
    'insufficient' coverage must never carry a numeric score, no matter how
    that score was computed. Without this, a country with e.g. 1 of 7
    components available reads as a confident '0.0 / strong' if that one
    lucky component happens to be safe -- the coverage badge would flag it,
    but the number alone looks authoritative and isn't. Call this AFTER any
    tier adjustment (proxy caps, discounts, etc.), right before bucket
    assignment, in every engine."""
    return None if tier == "insufficient" else score


def confidence_for(tier: str) -> str:
    return {"full": "high", "proxy": "medium", "insufficient": "low"}.get(tier, "low")


def _bucket_index(score: float, thresholds: Sequence[float]) -> int:
    idx = 0
    for t in thresholds:
        if score >= t:
            idx += 1
        else:
            break
    return idx


def bucket_with_hysteresis(score: Optional[float], thresholds: Sequence[float],
                           labels: Sequence[str], prev_label: Optional[str] = None,
                           margin_pct: float = 0.10) -> Optional[str]:
    """Assign `score` (0-100) to one of len(thresholds)+1 buckets defined by
    ascending cut points, with a dead-band around the boundary adjacent to
    `prev_label` so single-boundary flutter does not flip the label every
    run. A move spanning more than one bucket always applies immediately
    (hysteresis smooths flutter, it never suppresses a genuine large move).
    `prev_label` = the label previously assigned to this (country, engine)
    pair, or None for a first-ever computation (plain threshold assignment)."""
    if score is None or pd.isna(score):
        return None
    plain_idx = min(_bucket_index(score, thresholds), len(labels) - 1)
    if prev_label is None or prev_label not in labels:
        return labels[plain_idx]
    prev_idx = labels.index(prev_label)
    if abs(plain_idx - prev_idx) != 1:
        return labels[plain_idx]
    boundary_idx = min(plain_idx, prev_idx)
    boundary = thresholds[boundary_idx]
    lo = thresholds[boundary_idx - 1] if boundary_idx - 1 >= 0 else 0.0
    hi = thresholds[boundary_idx + 1] if boundary_idx + 1 < len(thresholds) else 100.0
    margin = margin_pct * (hi - lo)
    if plain_idx > prev_idx:      # moving to a worse (higher) bucket
        return labels[plain_idx] if score >= boundary + margin else prev_label
    return labels[plain_idx] if score <= boundary - margin else prev_label  # moving to a better bucket


def fresh_latest(pair, ref_ts, max_age_years: float = 4.0
                 ) -> Tuple[Optional[float], Optional[str]]:
    """Unpack a (value, date) pair from dalio._latest() into
    (value, iso_date_str), forcing value to None when the observation is
    older than `max_age_years` before `ref_ts`. Without this guard an
    arbitrarily old print (a 2016 NPL ratio, say) is scored at full weight
    as the current condition AND counts toward 'full' coverage. The date is
    returned even for stale/missing values so components_json can record
    why the input was dropped."""
    value, obs = pair
    if value is None or pd.isna(value) or obs is None or pd.isna(obs):
        return None, None
    obs_ts = pd.Timestamp(obs)
    date_str = str(obs_ts.date())
    if (pd.Timestamp(ref_ts) - obs_ts).days > max_age_years * 365.25:
        return None, date_str
    return float(value), date_str


def fresh_first_avail(by_ind: dict, ids, ref_ts, max_age_years: float = 4.0
                      ) -> Tuple[Optional[float], Optional[str]]:
    """fresh_latest() over a fallback list of indicator ids: return the first
    candidate whose LATEST print is fresh. dalio._first_avail picks the first
    NON-EMPTY series before any freshness check, so a stale primary (e.g. an
    old gdp_growth_weo) would shadow a perfectly current fallback
    (real_gdp_growth) and needlessly lower coverage. Single ids behave like
    fresh_latest(_latest(...)). The first stale candidate's date is returned
    when nothing is fresh, so the audit trail can still show why."""
    from lazyray.dalio import _latest
    if isinstance(ids, str):
        ids = [ids]
    stale_date = None
    for i in ids:
        s = by_ind.get(i)
        if s is None or s.empty:
            continue
        value, obs = fresh_latest(_latest(s), ref_ts, max_age_years)
        if value is not None:
            return value, obs
        stale_date = stale_date or obs
    return None, stale_date


@lru_cache(maxsize=1)
def git_short_sha() -> str:
    """Short git SHA of the running checkout, for the components_json
    model_version field. 'unknown' outside a git checkout (never raises)."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def prev_label(con, country_iso3: str, engine: str, before_date) -> Optional[str]:
    """Last non-NULL label assigned to (country, engine) strictly before
    `before_date`, for bucket_with_hysteresis(). NULL-label rows (periods of
    insufficient coverage) are skipped, so hysteresis survives a data outage
    instead of silently resetting to plain assignment. None if the pair has
    never carried a label."""
    row = con.execute(
        "SELECT label FROM engine_scores WHERE country_iso3 = ? AND engine = ? "
        "AND ref_date < ? AND label IS NOT NULL "
        "ORDER BY ref_date DESC LIMIT 1",
        [country_iso3, engine, before_date]).fetchone()
    return row[0] if row else None

"""Pure pandas transformations used by the stress monitor."""
from __future__ import annotations

import numpy as np
import pandas as pd
from bisect import bisect_right, insort_right


STALENESS_DAYS = {"daily": 10, "weekly": 21, "monthly": 100}


def business_daily(series: pd.Series, end=None, staleness_days: int | None = None, *, frequency: str = "daily") -> pd.Series:
    """Forward-fill observations on business days, rejecting stale values."""
    if staleness_days is None:
        try:
            staleness_days = STALENESS_DAYS[frequency]
        except KeyError as exc:
            raise ValueError(f"unknown frequency {frequency}") from exc
    s = pd.Series(series).copy()
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty:
        return s
    dates = pd.date_range(s.index.min(), pd.Timestamp(end) if end else s.index.max(), freq="B")
    values = s.reindex(dates).ffill()
    observed = pd.Series(s.index, index=s.index).reindex(dates).ffill()
    return values.where((pd.Series(dates, index=dates) - observed).dt.days <= staleness_days)


def expanding_cdf(series: pd.Series, min_obs: int = 756) -> pd.Series:
    """No-look-ahead percentile rank; the current observation is included."""
    s = pd.Series(series, dtype=float)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    seen: list[float] = []
    for date, value in s.items():
        if pd.notna(value):
            insort_right(seen, float(value))
            if len(seen) >= min_obs:
                out.loc[date] = bisect_right(seen, float(value)) / len(seen)
    return out


def ewma_correlation_index(segments: pd.DataFrame, weights: pd.Series, lam: float = .93) -> pd.Series:
    """CISS-style EWMA correlation aggregation, bounded by zero and one."""
    frame = segments.astype(float).copy()
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    n = frame.shape[1]
    # The state carried between iterations must be an actual (unnormalized)
    # covariance matrix, not the correlation derived from it -- feeding
    # yesterday's *normalized* corr back into today's EWMA re-normalizes
    # variances toward 1 on every step, so correlations never relax away
    # from perfect comovement (Holló-Kremer-Lo Duca 2012 keeps covariance,
    # not correlation, as the EWMA state; corr is derived from it only at
    # the point of use).
    # Initialization choice: with no observed covariance yet, start from the
    # all-ones matrix (unit variances, unit covariances). This is the
    # (rank-1) covariance whose implied correlation is itself all-ones, so
    # equal simultaneous segment shocks are still read as fully comoving
    # before any real co-movement has been observed -- the same property the
    # previous code intended, now expressed as a real covariance rather than
    # smuggling the normalized correlation matrix through the recursion.
    cov = np.ones((n, n))
    mean = np.zeros(n)
    for date, row in frame.iterrows():
        values = row.to_numpy(float)
        present = np.isfinite(values)
        if not present.any():
            continue
        # Update a stable EWMA covariance with neutral values for unavailable segments.
        x = np.where(present, values, mean)
        mean = lam * mean + (1 - lam) * x
        centered = x - mean
        cov = lam * cov + (1 - lam) * np.outer(centered, centered)
        scale = np.sqrt(np.maximum(np.diag(cov), 1e-12))
        corr = np.clip(cov / np.outer(scale, scale), -1, 1)
        np.fill_diagonal(corr, 1)
        idx = np.flatnonzero(present)
        # CISS weights are the fixed segment weights times sub-index levels;
        # absent segments carry no contribution rather than being reweighted.
        w = weights.reindex(frame.columns).to_numpy(float)[idx] * values[idx]
        c = corr[np.ix_(idx, idx)]
        result.loc[date] = float(np.clip(np.sqrt(max(w @ c @ w, 0.0)), 0, 1))
    return result

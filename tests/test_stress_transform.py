import numpy as np
import pandas as pd

from lazyray.stress.transform import business_daily, expanding_cdf, ewma_correlation_index


def test_expanding_cdf_waits_for_minimum_and_has_no_lookahead():
    s = pd.Series([1., 3., 2., 4.], index=pd.date_range("2020-01-01", periods=4))
    got = expanding_cdf(s, min_obs=3)
    assert got.iloc[:2].isna().all()
    assert got.iloc[2] == 2 / 3
    assert got.iloc[3] == 1


def test_business_daily_staleness_cap():
    s = pd.Series([1., 2.], index=pd.to_datetime(["2020-01-01", "2020-03-01"]))
    got = business_daily(s, end="2020-02-28", staleness_days=45)
    assert got.loc["2020-02-14"] == 1
    assert pd.isna(got.loc["2020-02-20"])


def test_monthly_staleness_allows_80_days_but_blanks_110_days():
    s = pd.Series([1.], index=pd.to_datetime(["2020-01-01"]))
    got = business_daily(s, end="2020-04-20", frequency="monthly")
    assert got.loc["2020-03-20"] == 1.
    assert pd.isna(got.loc["2020-04-20"])


def test_aggregation_is_bounded_and_rewards_comovement():
    dates = pd.date_range("2020-01-01", periods=100, freq="B")
    weights = pd.Series({"a": .5, "b": .5})
    together = pd.DataFrame({"a": np.linspace(.1, 1, 100), "b": np.linspace(.1, 1, 100)}, index=dates)
    apart = pd.DataFrame({"a": np.linspace(.1, 1, 100), "b": np.linspace(1, .1, 100)}, index=dates)
    co = ewma_correlation_index(together, weights)
    anti = ewma_correlation_index(apart, weights)
    assert co.dropna().between(0, 1).all()
    assert co.iloc[-1] > anti.iloc[-1]


def test_aggregation_state_is_covariance_not_recycled_correlation():
    # F2 regression: the EWMA state carried between iterations must be an
    # actual covariance, not last period's *normalized* correlation fed back
    # in (the bug -- see transform.ewma_correlation_index's docstring/
    # comments). Reusing corr as state re-anchors variance toward 1 every
    # step and never lets correlation relax away from the initial
    # full-comovement assumption, so the index systematically overstates
    # comovement once two segments that moved together during a crisis
    # decouple into genuinely independent noise afterward.
    rng = np.random.default_rng(1)
    crisis = np.linspace(.1, .9, 60)
    noise_a = np.clip(.3 + rng.normal(0, .15, 300), 0, 1)
    noise_b = np.clip(.3 + rng.normal(0, .15, 300), 0, 1)
    a = np.r_[crisis, noise_a]
    b = np.r_[crisis, noise_b]
    dates = pd.date_range("2020-01-01", periods=len(a), freq="B")
    frame = pd.DataFrame({"a": a, "b": b}, index=dates)
    weights = pd.Series({"a": .5, "b": .5})
    result = ewma_correlation_index(frame, weights)
    # With the bug, the recycled-correlation state stayed anchored near the
    # crisis's full comovement (measured directly: implied corr(a,b) = 0.71,
    # last-90-day mean index = 0.265). A real covariance recursion lets both
    # relax toward the segments' true near-zero correlation once they
    # decouple (measured: implied corr = 0.32, last-90-day mean = 0.217).
    assert result.iloc[-90:].mean() < 0.25


def test_aggregation_requires_a_segment_and_handles_full_stress():
    dates = pd.date_range("2020-01-01", periods=2, freq="B")
    weights = pd.Series({"a": .5, "b": .5})
    segments = pd.DataFrame({"a": [np.nan, 1.0], "b": [np.nan, 1.0]}, index=dates)
    result = ewma_correlation_index(segments, weights)
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 1.0

"""Data-hub-only orchestration for the stress monitor."""
from __future__ import annotations

from datetime import date

import pandas as pd

from .config import NET_LIQUIDITY_UNIT_SCALE_TO_MILLIONS, REGIONS, SPREAD_LEGS
from .regimes import fit_regimes
from .transform import business_daily, expanding_cdf, ewma_correlation_index


def _reader():
    from market_data_hub.reader import read_macro, read_prices
    return read_macro, read_prices


def _raw(indicator, macro, prices) -> pd.Series:
    name, kind = indicator.name, indicator.source
    if kind == "composite":
        if indicator.transform == "net_liquidity_chg_20":
            return macro.loc[:, list(indicator.inputs)]
        raise ValueError(f"unknown composite transform {indicator.transform}")
    if kind == "spread":
        a, b = SPREAD_LEGS[name]
        return macro[a] - macro[b]
    if kind == "pair":
        a, b = name.split("-")
        return -(prices[a].pct_change(20) - prices[b].pct_change(20))
    s = (macro if kind == "macro" else prices)[name]
    return s


def _transform(daily: pd.Series, transform: str) -> pd.Series:
    if transform == "abs_change_60":
        return daily.diff(60).abs()
    if transform == "return_diff_20":
        return daily.pct_change(20)
    if transform == "drawdown_60":
        return -(daily / daily.rolling(60, min_periods=20).max() - 1)
    return daily


def _load(region, hub_db, as_of):
    read_macro, read_prices = _reader()
    macros, prices = set(), set()
    for segment in region.segments:
        for ind in segment.indicators:
            if ind.source == "macro":
                macros.add(ind.name)
            elif ind.source == "composite":
                macros.update(ind.inputs)
            elif ind.source == "spread":
                macros.update(SPREAD_LEGS[ind.name])
            elif ind.source == "pair":
                prices.update(ind.name.split("-"))
            else:
                prices.add(ind.name)
    def read(fn, names, **kwargs):
        if not names:
            return pd.DataFrame()
        try:
            return fn(sorted(names), end=str(as_of), wide=True, db_path=hub_db, **kwargs)
        except Exception:
            # Some hub versions reject a whole mixed/absent request. Retry individually.
            frames = []
            for name in sorted(names):
                try:
                    frames.append(fn(name, end=str(as_of), wide=True, db_path=hub_db, **kwargs))
                except Exception:
                    pass
            return pd.concat(frames, axis=1) if frames else pd.DataFrame()
    return read(read_macro, macros, asof=str(as_of)), read(read_prices, prices)


def compute_region(region_name: str, *, hub_db=None, as_of=None, min_obs: int = 756):
    """Return index, component records, missing indicator names, and segments."""
    as_of = pd.Timestamp(as_of or date.today()).date()
    region = REGIONS[region_name]
    macro, prices = _load(region, hub_db, as_of)
    # Reader's wide output uses a date index; defensive normalization helps test doubles.
    for frame in (macro, prices):
        if not frame.empty:
            frame.index = pd.to_datetime(frame.index)
    pct_by_segment, components, missing = {}, [], []
    for segment in region.segments:
        cols = []
        for ind in segment.indicators:
            try:
                raw = _raw(ind, macro, prices)
                if isinstance(raw, pd.DataFrame):
                    daily_inputs = raw.apply(lambda series: business_daily(series, end=as_of, frequency=ind.frequency))
                    if daily_inputs.empty or daily_inputs.isna().all().any():
                        raise KeyError(ind.name)
                    if ind.transform == "net_liquidity_chg_20":
                        # Bring every leg to a common unit (millions of USD)
                        # before combining -- see config.NET_LIQUIDITY_UNIT_SCALE_TO_MILLIONS
                        # for why RRPONTSYD alone needs the x1000.
                        scaled = daily_inputs.mul(pd.Series(NET_LIQUIDITY_UNIT_SCALE_TO_MILLIONS))
                        daily = -(scaled["WALCL"] - scaled["WTREGEN"] - scaled["RRPONTSYD"]).diff(20)
                    else:
                        raise ValueError(f"unknown composite transform {ind.transform}")
                else:
                    raw = raw.dropna()
                    if raw.empty:
                        raise KeyError(ind.name)
                    daily = business_daily(raw, end=as_of, frequency=ind.frequency)
                    # Pair return differentials are formed from both price
                    # legs in _raw; applying return_diff_20 again would turn
                    # that already-transformed spread into a second return.
                    if ind.source != "pair":
                        daily = _transform(daily, ind.transform)
                pct = expanding_cdf(daily, min_obs=min_obs)
                cols.append(pct.rename(ind.name))
                for dt, value in daily.items():
                    p = pct.loc[dt]
                    if pd.notna(p):
                        components.append((dt.date(), region_name, segment.name, ind.name, float(value), float(p)))
            except (KeyError, ValueError):
                missing.append(ind.name)
        if cols:
            pct_by_segment[segment.name] = pd.concat(cols, axis=1).mean(axis=1, skipna=True)
    segments = pd.DataFrame(pct_by_segment)
    if segments.empty:
        return pd.DataFrame(), components, missing, segments
    weights = pd.Series({s.name: s.weight for s in region.segments})
    index = ewma_correlation_index(segments, weights)
    regimes = fit_regimes(index)
    out = pd.DataFrame({"index_value": index, "p_stress": regimes.p_stress, "regime": regimes.regime, "regime_hmm": regimes.regime_hmm})
    out["n_segments"] = segments.notna().sum(axis=1)
    return out, components, missing, segments


def run_monitor(regions=("us", "ea", "em"), *, hub_db=None, db_path=None, as_of=None, write=True, min_obs: int = 756):
    """Recompute every requested region's history, optionally persisting it."""
    all_index: list[tuple[date, str, float, float, str, str, int]] = []
    all_components: list[tuple[date, str, str, str, float, float]] = []
    missing: dict[str, list[str]] = {}
    for region in regions:
        output, components, absent, _ = compute_region(region, hub_db=hub_db, as_of=as_of, min_obs=min_obs)
        missing[region] = absent
        if not output.empty:
            all_index.extend((dt.date(), region, float(r.index_value), float(r.p_stress), str(r.regime), str(r.regime_hmm), int(r.n_segments)) for dt, r in output.dropna(subset=["index_value"]).iterrows())
        all_components.extend(all for all in components)
    if write:
        from .persist import write_results
        write_results(all_index, all_components, db_path)
    return {"index_rows": all_index, "components": all_components, "missing": missing}

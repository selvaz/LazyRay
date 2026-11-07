"""Static, deliberately small specification for the stress monitor."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Indicator:
    name: str
    source: str  # macro or price
    transform: str = "level"  # level, abs_change_60, return_diff_20, drawdown_60, net_liquidity_chg_20
    inputs: tuple[str, ...] = ()
    frequency: str = "daily"  # daily, weekly, monthly


@dataclass(frozen=True)
class Segment:
    name: str
    weight: float
    indicators: tuple[Indicator, ...]


@dataclass(frozen=True)
class Region:
    name: str
    segments: tuple[Segment, ...]


REGIONS = {
    "us": Region(
        "us",
        (
            Segment(
                "credit",
                0.25,
                (
                    Indicator("BAMLH0A0HYM2", "macro"),
                    Indicator("BAMLC0A0CM", "macro"),
                    Indicator("BAMLH0A3HYC", "macro"),
                ),
            ),
            Segment(
                "rates",
                0.20,
                (
                    Indicator("DFII10", "macro"),
                    Indicator("DGS10", "resolved", "abs_change_60"),
                    Indicator("T10Y2Y", "spread", "neg_level"),
                ),
            ),
            Segment("volatility", 0.20, (Indicator("VIXCLS", "macro"),)),
            Segment(
                "conditions",
                0.20,
                (
                    Indicator("NFCI", "macro", frequency="weekly"),
                    Indicator("STLFSI4", "macro", frequency="weekly"),
                ),
            ),
            Segment(
                "liquidity",
                0.15,
                (
                    Indicator(
                        "net_liquidity_chg_20",
                        "composite",
                        "net_liquidity_chg_20",
                        ("WALCL", "WTREGEN", "RRPONTSYD"),
                        "weekly",
                    ),
                ),
            ),
        ),
    ),
    "ea": Region(
        "ea",
        (
            Segment("rates", 0.25, (Indicator("ECBDFR", "macro", "abs_change_60"),)),
            Segment("bond", 0.30, (Indicator("DE_10Y", "resolved", "abs_change_60"),)),
            Segment(
                "periphery",
                0.45,
                (
                    Indicator("IT-DE", "spread"),
                    Indicator("ES-DE", "spread"),
                    Indicator("FR-DE", "spread"),
                ),
            ),
        ),
    ),
    "em": Region(
        "em",
        (
            Segment(
                "hard_currency",
                0.40,
                (
                    Indicator("EMB-IEF", "pair", "return_diff_20"),
                    Indicator("EMB", "price", "drawdown_60"),
                ),
            ),
            Segment(
                "local_currency",
                0.35,
                (
                    Indicator("EMLC-IEF", "pair", "return_diff_20"),
                    Indicator("EMLC", "price", "drawdown_60"),
                ),
            ),
            Segment(
                "fx",
                0.25,
                tuple(
                    Indicator(x, "price", "drawdown_60") for x in ("UUP", "FXE", "FXY")
                ),
            ),
        ),
    ),
}

SPREAD_LEGS = {
    "IT-DE": ("IT_10Y", "DE_10Y"),
    "ES-DE": ("ES_10Y", "DE_10Y"),
    "FR-DE": ("FR_10Y", "DE_10Y"),
    "T10Y2Y": ("US_10Y", "US_2Y"),
}
RESOLVED_CONCEPTS = {"DGS10": "US_10Y", "DE_10Y": "DE_10Y"}

# FRED publishes these three net-liquidity legs on different scales: WALCL
# and WTREGEN are levels in millions of USD, but RRPONTSYD is published in
# BILLIONS of USD. Scale every leg to millions before combining them (the
# net_liquidity_chg_20 composite in stress/pipeline.py) -- without this the
# RRP leg is understated by 1000x, which quietly erases the 2021-2023 RRP
# swings (multi-trillion-dollar moves) from the 20-day-change series the
# expansive percentiles are built on. Add new liquidity legs here, not as an
# inline magic number at the call site.
NET_LIQUIDITY_UNIT_SCALE_TO_MILLIONS: dict[str, float] = {
    "WALCL": 1.0,
    "WTREGEN": 1.0,
    "RRPONTSYD": 1000.0,
}

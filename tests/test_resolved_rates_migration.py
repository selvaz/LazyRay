from datetime import date

import pandas as pd
import pytest

from lazyray.dalio_v2 import panel
from lazyray.stress import pipeline


def test_monthly_resolved_replaces_both_live_and_historical_panels(monkeypatch):
    monkeypatch.setattr(panel, "load_resolved_rates", lambda: ({}, {"US_10Y": {}}))
    monkeypatch.setattr(panel, "get_countries", lambda: [{"iso2": "US", "iso3": "USA"}])
    calls = []

    def history(concept, **kwargs):
        calls.append((concept, kwargs))
        return pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-29", "2024-02-10"],
                "value": [1.0, 2.0, 3.0],
            }
        )

    monkeypatch.setattr(panel, "read_resolved_history", history)
    old = pd.DataFrame(
        [
            {
                **{c: None for c in panel._EXT_COLUMNS},
                "indicator_id": "bond_yield_10y",
                "value": 99,
            }
        ]
    )
    monkeypatch.setattr(panel, "read_macro_panel_ext", lambda **kwargs: old)
    monkeypatch.setattr(panel, "_native_panel_asof", lambda *args: old.iloc[0:0])
    for ref in (date(2024, 2, 15), date.today()):
        result = panel.load_panel_ext(ref, "fake")
        assert result.value.tolist() == [2.0, 3.0]
        assert result.date.iloc[0] == pd.Timestamp("2024-01-31")
        assert result.provider_code.tolist() == ["US_10Y", "US_10Y"]
    assert calls[0] == ("US_10Y", {"end": "2024-02-15", "db_path": "fake"})
    assert panel.replace_bond_yields(old, date(2024, 2, 15), "fake").value.tolist() == [
        2.0,
        3.0,
    ]


@pytest.mark.parametrize(
    "region,expected",
    [("us", {"US_10Y", "US_2Y"}), ("ea", {"DE_10Y", "IT_10Y", "ES_10Y", "FR_10Y"})],
)
def test_stress_reads_concepts_and_surfaces_quality(monkeypatch, region, expected):
    import market_data_hub.reader as reader

    dates = pd.date_range("2024-01-01", periods=80, freq="B")
    requested = []

    def rates(concepts, **kwargs):
        requested.extend(concepts)
        return [
            {
                "concept": c,
                "value": 4.0,
                "status": "primary",
                "observation_date": dates[-1].date(),
                "series_id": c,
            }
            for c in concepts
        ]

    def history(concept, **kwargs):
        return pd.DataFrame({"date": dates, "value": range(len(dates))})

    monkeypatch.setattr(reader, "read_resolved_rates", rates)
    monkeypatch.setattr(reader, "read_resolved_history", history)
    monkeypatch.setattr(
        pipeline,
        "_reader",
        lambda: (
            lambda ids, **kw: pd.DataFrame(index=dates, columns=ids),
            lambda ids, **kw: pd.DataFrame(index=dates, columns=ids),
        ),
    )
    result = pipeline.run_monitor((region,), as_of=dates[-1], min_obs=1, write=False)
    assert set(requested) == expected
    quality = result["resolved_quality"][region]
    assert quality["T10Y2Y" if region == "us" else "IT-DE"]["legs_mismatch"]
    assert all(
        row["status"] == "primary"
        for row in quality["DGS10" if region == "us" else "DE_10Y"]["legs"].values()
    )


def test_mismatched_or_disputed_current_spread_is_not_scored(monkeypatch):
    region = pipeline.REGIONS["ea"]
    dates = pd.date_range("2024-01-01", periods=80, freq="B")
    current = {
        c: {
            "concept": c,
            "value": 4.0,
            "status": "primary",
            "observation_date": dates[-1].date(),
            "series_id": c,
        }
        for c in ("DE_10Y", "IT_10Y", "ES_10Y", "FR_10Y")
    }
    current["IT_10Y"]["observation_date"] = dates[-2].date()
    current["ES_10Y"]["status"] = "disputed"
    resolved = pd.DataFrame({c: range(len(dates)) for c in current}, index=dates)
    monkeypatch.setattr(
        pipeline,
        "_load",
        lambda *args: (pd.DataFrame(), pd.DataFrame(), resolved, current),
    )
    _, _, missing, segments = pipeline.compute_region(
        region.name, as_of=dates[-1], min_obs=1
    )
    assert "IT-DE" in missing and "ES-DE" in missing
    assert segments.attrs["resolved_quality"]["IT-DE"]["legs_mismatch"]
    assert segments.attrs["resolved_quality"]["ES-DE"]["status"][0] == "disputed"


def test_v1_routes_extended_panel_through_replacement(monkeypatch):
    import lazyray.dalio as v1

    original = pd.DataFrame({"indicator_id": ["bond_yield_10y"]})
    monkeypatch.setattr(v1, "read_macro_panel_ext", lambda **kwargs: original)
    monkeypatch.setattr(v1, "get_countries", lambda: [])
    calls = []

    def replacement(frame, ref_date, db_path):
        calls.append((frame, ref_date, db_path))
        return pd.DataFrame()

    monkeypatch.setattr(v1, "replace_bond_yields", replacement)
    with pytest.raises(RuntimeError, match="macro_panel empty"):
        v1.run_dalio(hub_db_path="fake")
    assert calls[0][0] is original and calls[0][2] == "fake"


def test_funding_uses_shared_panel_and_marks_vintage_limit(monkeypatch):
    import json
    from lazyray.dalio_v2 import funding_liquidity as funding

    ref = date(2024, 2, 15)
    monkeypatch.setattr(panel, "load_resolved_rates", lambda: ({}, {"US_10Y": {}}))
    monkeypatch.setattr(panel, "get_countries", lambda: [{"iso2": "US", "iso3": "USA"}])
    monkeypatch.setattr(
        panel,
        "read_resolved_history",
        lambda *args, **kwargs: pd.DataFrame(
            {"date": ["2023-02-10", "2024-02-10"], "value": [2.0, 4.0]}
        ),
    )
    monkeypatch.setattr(panel, "_native_panel_asof", lambda *args: panel._empty_ext())
    monkeypatch.setattr(funding, "prev_label", lambda *args: None)
    result = funding.compute(None, ref, hub_db_path="fake")
    audit = json.loads(result.iloc[0].components_json)
    assert audit["branch"] == "market"
    assert audit["vintage_safe"] is False
    assert audit["bond_yield_history"] == "not point-in-time; observation-date history"

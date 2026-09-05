import duckdb
import numpy as np
import pandas as pd
import run_stress_monitor as command
import sys
from pathlib import Path
from uuid import uuid4

from lazyray.stress.digest import build_digest, should_notify
from lazyray.stress.persist import apply_schema
from lazyray.stress.config import Indicator, Region, Segment
from lazyray.stress.pipeline import compute_region, run_monitor
from lazyray.stress.regimes import fit_regimes, hysteresis_labels


def test_pipeline_writes_digest_and_notification(monkeypatch):
    dates = pd.date_range("2019-01-01", periods=950, freq="B")
    values = {"BAMLH0A0HYM2": np.linspace(3, 8, len(dates)), "BAMLC0A0CM": np.linspace(1, 3, len(dates)), "BAMLH0A3HYC": np.linspace(5, 12, len(dates)), "DFII10": np.linspace(-1, 2, len(dates)), "DGS10": np.linspace(1, 5, len(dates)), "T10Y2Y": np.linspace(2, -1, len(dates)), "VIXCLS": np.linspace(12, 40, len(dates)), "NFCI": np.linspace(-1, 1, len(dates)), "STLFSI4": np.linspace(-1, 2, len(dates)), "WALCL": np.linspace(4500, 4000, len(dates)), "RRPONTSYD": np.linspace(2000, 100, len(dates)), "WTREGEN": np.linspace(100, 900, len(dates))}
    macro = pd.DataFrame(values, index=dates)
    def macro_reader(ids, **kwargs): return macro.reindex(columns=ids)
    def prices_reader(ids, **kwargs): return pd.DataFrame(index=dates, columns=ids, data=100.)
    monkeypatch.setattr("lazyray.stress.pipeline._reader", lambda: (macro_reader, prices_reader))
    monkeypatch.setattr(
        "lazyray.stress.pipeline.fit_regimes",
        lambda index: pd.DataFrame({"p_stress": 0.1, "regime": "calm", "regime_hmm": "calm"}, index=index.index),
    )
    db = Path(__file__).parent / f".stress-{uuid4().hex}.duckdb"
    try:
        result = run_monitor(("us",), db_path=str(db), as_of=dates[-1].date())
        assert result["index_rows"]
        con = duckdb.connect(str(db))
        assert con.execute("select count(*) from stress_index").fetchone()[0] > 100
        text = build_digest(con, ["us"], dates[-1].date())
        assert "US" in text and "top segments" in text
        # Explicit constructed transition validates the notification gate.
        apply_schema(con)
        con.execute("insert or replace into stress_index (date,region,index_value,p_stress,regime,regime_hmm,n_segments,computed_at) values ('2030-01-01','test',.9,.1,'calm','calm',2,current_timestamp)")
        con.execute("insert or replace into stress_index (date,region,index_value,p_stress,regime,regime_hmm,n_segments,computed_at) values ('2030-01-02','test',.8,.9,'stress','stress',2,current_timestamp)")
        assert should_notify(con, "2030-01-02")
        con.close()
    finally:
        for path in (db, Path(f"{db}.lock")):
            if path.exists():
                path.unlink()


def test_net_liquidity_composite_scales_rrp_to_millions_before_combining(monkeypatch):
    # F1 regression: FRED publishes RRPONTSYD in BILLIONS of USD while WALCL
    # and WTREGEN are in MILLIONS (config.NET_LIQUIDITY_UNIT_SCALE_TO_MILLIONS).
    # A raw subtraction understates a real RRP move 1000x. Here RRPONTSYD
    # jumps from 0 to 2500 (i.e. a $2.5 TRILLION build-up, in billions); the
    # resulting 20-business-day net-liquidity change must show up scaled to
    # millions (~2,500,000), not the raw billions figure (~2,500) a
    # unit-blind subtraction would produce.
    dates = pd.date_range("2020-01-01", periods=30, freq="W-MON")
    rrp = pd.Series(0.0, index=dates)
    rrp.iloc[15:] = 2500.0
    macro = pd.DataFrame({
        "WALCL": pd.Series(7_000_000.0, index=dates),
        "WTREGEN": pd.Series(500_000.0, index=dates),
        "RRPONTSYD": rrp,
    })
    region = Region("test_liq", (Segment("liquidity", 1.0, (Indicator(
        "net_liquidity_chg_20", "composite", "net_liquidity_chg_20",
        ("WALCL", "WTREGEN", "RRPONTSYD"), "weekly"),)),))
    monkeypatch.setitem(__import__("lazyray.stress.pipeline", fromlist=["REGIONS"]).REGIONS, "test_liq", region)
    monkeypatch.setattr("lazyray.stress.pipeline._reader", lambda: (
        lambda ids, **kwargs: macro.reindex(columns=ids), lambda ids, **kwargs: pd.DataFrame(index=dates)))
    _, components, missing, _ = compute_region("test_liq", as_of=dates[-1].date(), min_obs=1)
    assert missing == []
    values = [v for _, _, _, _, v, _ in components if v != 0]
    assert values and all(abs(v) == 2_500_000.0 for v in values)


def test_missing_indicator_degrades_and_monthly_staleness_blanks(monkeypatch):
    dates = pd.date_range("2020-01-01", "2020-03-31", freq="B")
    macro = pd.DataFrame({"present": np.arange(len(dates), dtype=float), "monthly": np.nan}, index=dates)
    macro.loc["2020-01-01", "monthly"] = 1.0
    region = Region("test", (Segment("usable", .5, (Indicator("present", "macro"), Indicator("missing", "macro"))), Segment("bond", .5, (Indicator("monthly", "macro", frequency="monthly"),))))
    monkeypatch.setitem(__import__("lazyray.stress.pipeline", fromlist=["REGIONS"]).REGIONS, "test", region)
    monkeypatch.setattr("lazyray.stress.pipeline._reader", lambda: (lambda ids, **kwargs: macro.reindex(columns=ids), lambda ids, **kwargs: pd.DataFrame(index=dates)))
    output, _, missing, segments = compute_region("test", as_of="2020-03-31", min_obs=1)
    assert not output.empty and missing == ["missing"]
    assert segments.loc["2020-03-20", "bond"] > 0  # 79 calendar days is valid for monthly data


def test_percentile_regime_ordering_and_hysteresis():
    baseline = np.linspace(.05, .55, 756)
    index = pd.Series(np.r_[baseline, .38, .42, .48, .56, .52], index=pd.date_range("2020-01-01", periods=761, freq="B"))
    result = fit_regimes(index)
    assert (result.regime.iloc[:755] == "calm").all()
    assert result.regime.iloc[756] == "elevated"
    assert result.regime.iloc[759] == "stress"
    assert result.regime.iloc[760] == "stress"  # small dip remains above the 75th percentile
    assert hysteresis_labels([.7, .55, .35]) == ["stress", "stress", "calm"]


def test_digest_changes_regime_and_notification_crossing():
    db = Path(__file__).parent / f".stress-digest-{uuid4().hex}.duckdb"
    con = duckdb.connect(str(db))
    apply_schema(con)
    dates = pd.date_range("2030-01-01", periods=22, freq="B")
    for i, dt in enumerate(dates):
        regime = "stress" if i == 21 else "calm"
        value = .1 + i * .01 if i < 21 else .9
        con.execute("insert into stress_index (date,region,index_value,p_stress,regime,regime_hmm,n_segments,computed_at) values (?,?,?,?,?,?,?,current_timestamp)", [dt.date(), "us", value, .8, regime, regime, 2])
    con.execute("insert into stress_components values (?,?,?,?,?,?,current_timestamp)", [dates[-1].date(), "us", "credit", "x", 1., .9])
    text = build_digest(con, ["us"], dates[-1].date())
    assert "pct " in text and "1w" in text and "1m" in text and "NEW REGIME: calm->stress" in text
    assert should_notify(con, dates[-1].date())
    con.execute("update stress_index set index_value=.2, regime='calm' where region='us'")
    assert not should_notify(con, dates[-1].date())
    con.close()
    db.unlink()


def test_digest_partial_coverage_and_elevated_transition_does_not_notify():
    db = Path(__file__).parent / f".stress-partial-{uuid4().hex}.duckdb"
    con = duckdb.connect(str(db))
    apply_schema(con)
    con.execute("insert into stress_index (date,region,index_value,p_stress,regime,regime_hmm,n_segments,computed_at) values ('2030-01-01','us',.9,.1,'calm','calm',2,current_timestamp)")
    con.execute("insert into stress_index (date,region,index_value,p_stress,regime,regime_hmm,n_segments,computed_at) values ('2030-01-02','us',.8,.2,'elevated','calm',2,current_timestamp)")
    con.execute("insert into stress_components values ('2030-01-02','us','credit','x',1,.8,current_timestamp)")
    text = build_digest(con, ["us"], "2030-01-02")
    assert "partial: 2/5 segments" in text and "missing: rates, volatility, conditions, liquidity" in text
    assert not should_notify(con, "2030-01-02")
    con.execute("update stress_index set regime='stress', n_segments=1 where date='2030-01-02'")
    assert not should_notify(con, "2030-01-02")
    con.close()
    db.unlink()


def test_notify_exit_code_and_force_send(monkeypatch):
    db = Path(__file__).parent / f".stress-command-{uuid4().hex}.duckdb"
    empty = {"index_rows": [], "components": [], "missing": {"us": []}}
    monkeypatch.setattr(command, "run_monitor", lambda *args, **kwargs: empty)
    monkeypatch.setattr(sys, "argv", ["run_stress_monitor.py", "--region", "us", "--db", str(db), "--notify"])
    assert command.main() == 3
    sent = []
    monkeypatch.setattr(command, "_send", sent.append)
    monkeypatch.setattr(sys, "argv", ["run_stress_monitor.py", "--region", "us", "--db", str(db), "--force-notify"])
    assert command.main() == 0
    assert sent == [""]
    db.unlink()

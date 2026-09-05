# -*- coding: utf-8 -*-
"""
test_dalio_v2_cadence.py — Fase 1 deliverables A and D
(docs/DALIO_PROD_ASSESSMENT_2026-09.md §3.1.1/§3.1.8):

  A. ref_date = the run date (not Dec 31 of some year): two runs on
     consecutive dates accumulate separate history, and prev_label() (and
     therefore bucket_with_hysteresis()) sees the earlier run's label --
     the hysteresis dead-band actually holds a label across real, distinct
     calendar days, not just within one hand-built engine_scores table.

  D. --if-changed: a SHA-256 hash of the input panel is stored per ref_date
     in run_meta; a run with an unchanged hash is skipped (no engines
     computed, nothing written) and reports which earlier ref_date the hash
     was last seen at; a run with changed inputs proceeds normally.
"""
from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone

import pandas as pd
from market_data_hub.db.connection import get_conn as get_hub_conn
from market_data_hub.db.upsert import upsert

from lazyray.dalio_v2 import runner
from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn as get_lazyray_conn

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

# ref_date >= today throughout this file (load_panel_ext's live path,
# read_macro_panel_ext) -- deliberately, so plain upsert() seeding (no
# vintage tables) is visible; the point-in-time asof path itself is covered
# separately in test_dalio_v2_panel.py.
_DAY1 = dt.date.today()
_DAY2 = _DAY1 + dt.timedelta(days=1)

# score-by-ref_date for the mocked engine: 41 on day 1 (plain assignment ->
# "watch", since bucket thresholds [20,40,60,80] put 41 in bucket index 2),
# 39 on day 2 (plain assignment alone would be "stable" -- bucket index 1 --
# but 39 sits inside the hysteresis dead-band around the watch/stable
# boundary: margin = 10% * (60-20) = 4, so it takes <= 36 to actually fall
# back to "stable"; prev_label() must find day 1's "watch" for this to hold).
_SCORE_BY_DATE = {_DAY1: 41.0, _DAY2: 39.0}


def _mock_compute(con, ref_date, cfg=None, hub_db_path=None):
    from lazyray.dalio_v2.scoring import bucket_with_hysteresis, prev_label
    score = _SCORE_BY_DATE[ref_date]
    prev = prev_label(con, "ZZZ", "sovereign_solvency", ref_date)
    label = bucket_with_hysteresis(
        score, [20, 40, 60, 80],
        ["strong", "stable", "watch", "stressed", "critical"], prev, 0.10)
    row = ("ZZZ", ref_date, "sovereign_solvency", score, label, "full", "high",
          7, 7, "{}", datetime.now(timezone.utc), None, None)
    return pd.DataFrame([row], columns=_COLUMNS)


def test_two_runs_on_consecutive_dates_accumulate_history_and_hysteresis_holds(tmp_db, monkeypatch):
    monkeypatch.setitem(runner._ENGINES, "sovereign_solvency", _mock_compute)

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1)
    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY2)

    con = get_lazyray_conn(read_only=True)
    rows = con.execute(
        "SELECT ref_date, score, label FROM engine_scores WHERE country_iso3 = 'ZZZ' "
        "ORDER BY ref_date").fetchall()
    con.close()

    # distinct ref_date values accumulate -- NOT one snapshot overwritten
    # daily (the old ref_date=Dec-31-current-year bug)
    assert [str(r[0]) for r in rows] == [str(_DAY1), str(_DAY2)]
    assert rows[0][2] == "watch"     # day 1: plain assignment, no prior label
    # day 2: prev_label() found day 1's "watch" (strictly earlier ref_date);
    # the score alone (39) would plainly land on "stable", but the
    # dead-band holds it at "watch" -- this is hysteresis actually working
    # across real runs, which was impossible before (ref_date was always
    # the same future Dec-31 date, so prev_label() never found anything).
    assert rows[1][2] == "watch"


def test_prev_label_finds_the_previous_run_days_label_directly(tmp_db, monkeypatch):
    # Narrower version of the above, isolating exactly the claim the prod
    # assessment made: "prev_label() now returns the previous run day's
    # label" -- queried directly, not just observed as a side effect.
    from lazyray.dalio_v2.scoring import prev_label

    monkeypatch.setitem(runner._ENGINES, "sovereign_solvency", _mock_compute)
    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1)

    con = get_lazyray_conn(read_only=True)
    got = prev_label(con, "ZZZ", "sovereign_solvency", _DAY2)
    con.close()
    assert got == "watch"


def test_same_day_rerun_replaces_only_that_days_batch(tmp_db, monkeypatch):
    # The runner's DELETE-then-INSERT per (ref_date, engine) is unchanged --
    # a rerun on the SAME day replaces that day's row, it does not create a
    # second one and does not touch the other day's history.
    monkeypatch.setitem(runner._ENGINES, "sovereign_solvency", _mock_compute)
    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1)
    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1)   # same-day rerun

    con = get_lazyray_conn(read_only=True)
    n = con.execute(
        "SELECT count(*) FROM engine_scores WHERE country_iso3 = 'ZZZ' "
        "AND ref_date = ?", [_DAY1]).fetchone()[0]
    con.close()
    assert n == 1


# ---------------------------------------------------------------------------
# D. --if-changed change detection (run_meta)
# ---------------------------------------------------------------------------

def _row(date_, iso3, ind, val):
    return {"date": date_, "country_iso3": iso3, "indicator_id": ind, "value": val,
            "indicator_name": ind, "pillar": "sovereign", "orientation": -1,
            "source": "test", "provider_dataset": "X", "provider_code": "Y",
            "unit": "pct", "frequency": "A"}


def test_if_changed_skips_on_identical_inputs_and_runs_on_changed_inputs(tmp_db):
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(dt.date(2025, 12, 31), "USA", "public_debt_gdp", 50.0),
    ]))
    con.commit()
    con.close()

    summary1 = run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1, if_changed=True)
    assert not summary1.get("skipped")
    assert summary1["sovereign_solvency"] == 1

    # a run_meta row now exists for DAY1
    con = get_lazyray_conn(read_only=True)
    meta = con.execute(
        "SELECT ref_date, input_hash, n_rows FROM run_meta WHERE ref_date = ?",
        [_DAY1]).fetchone()
    con.close()
    assert meta is not None and meta[1] and meta[2] > 0

    # unchanged hub data, next day: --if-changed must skip entirely
    summary2 = run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY2, if_changed=True)
    assert summary2 == {"skipped": True, "since": _DAY1}

    # no engine_scores row was written for DAY2 -- the skip really skipped
    con = get_lazyray_conn(read_only=True)
    n_day2 = con.execute(
        "SELECT count(*) FROM engine_scores WHERE ref_date = ?", [_DAY2]).fetchone()[0]
    con.close()
    assert n_day2 == 0

    # now change the hub data and rerun with --if-changed: must NOT skip
    con = get_hub_conn()
    con.execute("UPDATE macro_panel SET value = 65.0 WHERE country_iso3 = 'USA' "
               "AND indicator_id = 'public_debt_gdp'")
    con.commit()
    con.close()
    summary3 = run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY2, if_changed=True)
    assert not summary3.get("skipped")
    assert summary3["sovereign_solvency"] == 1


def test_if_changed_false_always_runs_regardless_of_hash(tmp_db):
    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(dt.date(2025, 12, 31), "USA", "public_debt_gdp", 50.0),
    ]))
    con.commit()
    con.close()

    run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY1)
    summary2 = run_dalio_v2(engines=["sovereign_solvency"], ref_date=_DAY2)  # if_changed defaults False
    assert not summary2.get("skipped")
    assert summary2["sovereign_solvency"] == 1


def test_cli_if_changed_exits_3_on_identical_inputs(tmp_db, monkeypatch, capsys):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib
    import run_dalio_v2 as cli
    importlib.reload(cli)

    con = get_hub_conn()
    upsert(con, "macro_panel", pd.DataFrame([
        _row(dt.date(2025, 12, 31), "USA", "public_debt_gdp", 50.0),
    ]))
    con.commit()
    con.close()

    monkeypatch.setattr(sys, "argv", ["run_dalio_v2.py", "--as-of", _DAY1.isoformat(),
                                      "--engines", "sovereign_solvency"])
    assert cli.main() == 0

    monkeypatch.setattr(sys, "argv", ["run_dalio_v2.py", "--as-of", _DAY2.isoformat(),
                                      "--engines", "sovereign_solvency", "--if-changed"])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 3
    assert "inputs unchanged since" in out
    assert _DAY1.isoformat() in out


def test_cli_rejects_invalid_as_of():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib
    import run_dalio_v2 as cli
    importlib.reload(cli)

    old_argv = sys.argv
    try:
        sys.argv = ["run_dalio_v2.py", "--as-of", "not-a-date"]
        assert cli.main() == 2
    finally:
        sys.argv = old_argv

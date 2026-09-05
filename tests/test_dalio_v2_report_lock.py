# -*- coding: utf-8 -*-
"""
test_dalio_v2_report_lock.py — regression for the writer-vs-reader IOException
collision between run_dalio_v2.py's report/Brief phase and the stress
monitor's writer (Codex review on e7bb1d4; see lazyray/lock.py's docstring).

DuckDB allows a single writer OR many readers per file, never a mix, and
rejects the wrong combination in EITHER direction with a raw IOException at
open time -- not lock.py's DBLockTimeout. run_dalio_v2.py's report/Brief
phase reopens its own DB read_only=True to render output; it used to do
this outside db_write_lock, after run_dalio_v2()'s own write phase had
already released it. The stress monitor's writer (persist.write_results,
inside run_monitor) could then collide with that unlocked reader, and in
the realistic overlap scenario the monitor is released from waiting on the
lock at exactly the moment this report phase starts reading -- so the
collision was frequent, not rare.

The fix makes the report/Brief phase take db_write_lock too, AFTER the
write phase's own `with db_write_lock(...)` block (inside run_dalio_v2())
has already exited -- db_write_lock is not reentrant, so nesting it inside
that block would deadlock a real FileLock against itself rather than raise
anything. These tests replace db_write_lock with a depth-counting double
(no real DuckDB/file locking needed) to prove: (a) the report phase's own
DB reopen happens while the lock is held, and (b) the lock is never held
twice at once, i.e. the two phases take it in sequence, not nested.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

_COLUMNS = ["country_iso3", "ref_date", "engine", "score", "label", "coverage_tier",
           "confidence", "n_components", "n_expected", "components_json", "computed_at",
           "relative_score", "relative_label"]

_DAY = dt.date.today()


def _mock_compute(con, ref_date, cfg=None, hub_db_path=None):
    row = ("ZZZ", ref_date, "sovereign_solvency", 41.0, "watch", "full", "high",
          7, 7, "{}", datetime.now(timezone.utc), None, None)
    return pd.DataFrame([row], columns=_COLUMNS)


class _LockSpy:
    """Stand-in for db_write_lock: counts concurrent holds instead of really
    locking a file. A would-be reentrant nesting shows up here as depth > 1
    (assertable) rather than hanging forever like a real, non-reentrant
    FileLock would."""

    def __init__(self):
        self.depth = 0
        self.max_depth = 0
        self.acquisitions = 0

    @contextmanager
    def __call__(self, *args, **kwargs):
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.acquisitions += 1
        try:
            yield
        finally:
            self.depth -= 1


def test_report_phase_reopen_is_locked_and_never_nests_the_write_lock(tmp_db, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib
    import run_dalio_v2 as cli
    importlib.reload(cli)

    from lazyray.dalio_v2 import runner
    monkeypatch.setitem(runner._ENGINES, "sovereign_solvency", _mock_compute)

    spy = _LockSpy()
    # Both modules imported db_write_lock by name (`from lazyray.lock import
    # db_write_lock`), so each holds its own reference -- patch both to the
    # same spy to observe the write phase (inside runner.run_dalio_v2) and
    # the report phase (inside cli.main) sharing one counter.
    monkeypatch.setattr(runner, "db_write_lock", spy)
    monkeypatch.setattr(cli, "db_write_lock", spy)

    real_get_conn = cli.get_conn
    depths_at_report_open = []

    def spying_get_conn(*args, **kwargs):
        if kwargs.get("read_only"):
            depths_at_report_open.append(spy.depth)
        return real_get_conn(*args, **kwargs)

    monkeypatch.setattr(cli, "get_conn", spying_get_conn)

    monkeypatch.setattr(sys, "argv", ["run_dalio_v2.py", "--as-of", _DAY.isoformat(),
                                      "--engines", "sovereign_solvency"])
    rc = cli.main()
    assert rc == 0

    # The report phase's read_only=True reopen happened with the lock held
    # (depth 1) -- not unlocked (0, the old bug) and never doubly-held (>1,
    # which would mean it nested inside the write phase's own hold instead
    # of taking a fresh one after that phase released it).
    assert depths_at_report_open == [1]
    assert spy.max_depth == 1
    # Two distinct, sequential acquisitions: the write phase inside
    # run_dalio_v2(), then this report phase -- never one shared hold.
    assert spy.acquisitions >= 2


def test_report_phase_still_runs_under_a_real_lock_object(tmp_db, monkeypatch):
    # Same scenario, but with the real db_write_lock (backed by filelock on
    # a throwaway path) instead of the counting double -- confirms the
    # actual production lock object tolerates being acquired, released, and
    # reacquired in sequence within one process without deadlocking, which
    # the depth-counting test above cannot demonstrate on its own.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib
    import run_dalio_v2 as cli
    importlib.reload(cli)

    from lazyray.dalio_v2 import runner
    monkeypatch.setitem(runner._ENGINES, "sovereign_solvency", _mock_compute)

    monkeypatch.setattr(sys, "argv", ["run_dalio_v2.py", "--as-of", _DAY.isoformat(),
                                      "--engines", "sovereign_solvency"])
    assert cli.main() == 0

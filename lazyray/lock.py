# -*- coding: utf-8 -*-
"""
lock.py — cross-process advisory lock for LazyRay's own DuckDB file.

DuckDB's real rule, measured across processes on the same file: a single
writer, OR any number of readers -- never a mix. Writer-vs-reader is
rejected in BOTH directions with a raw IOException at open time, which is
NOT DBLockTimeout and so is not caught by any of this module's handling.

This is stricter than a plain "only writers need to coordinate" model, so
every process that opens this file inside these jobs -- including a
read_only=True reopen purely to render a report -- must take this lock,
not just the ones that write. A previous version of this docstring claimed
the opposite ("readers never take this lock"); that contract does not
hold against DuckDB and was the root cause of repeated writer-vs-reader
IOException collisions between run_dalio_v2.py's report phase and
run_stress_monitor.py's writer. Do not reopen this DB file, in any mode,
outside db_write_lock().
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from lazyray.db.connection import _resolve_db_path

DEFAULT_TIMEOUT = 30.0


def db_lock_path(db_path: Optional[str] = None) -> str:
    """Path of the advisory lock file for a given (or resolved) DB path."""
    return str(Path(_resolve_db_path(db_path)).with_suffix(".lock"))


class DBLockTimeout(RuntimeError):
    """Raised when the writer lock cannot be acquired within the timeout."""


@contextmanager
def db_write_lock(db_path: Optional[str] = None,
                  timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Hold the writer lock for LazyRay's DB file for the duration of the
    block. Raises DBLockTimeout if another writer holds it past ``timeout``.
    If ``filelock`` is not installed the lock is a no-op (best effort)."""
    try:
        from filelock import FileLock, Timeout
    except ImportError:  # pragma: no cover - filelock listed in requirements
        yield
        return

    lock_path = db_lock_path(db_path)
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path, timeout=timeout)
    try:
        lock.acquire()
    except Timeout as exc:  # pragma: no cover - timing dependent
        raise DBLockTimeout(
            f"Another writer holds the DB lock ({db_lock_path(db_path)}); "
            f"skipping this run.") from exc
    try:
        yield
    finally:
        lock.release()

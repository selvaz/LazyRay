# -*- coding: utf-8 -*-
"""
lock.py — cross-process write lock for LazyRay's own DuckDB file.

DuckDB allows only a single writer per database file. Mirrors
market_data_hub.lock's contract: readers (read_only=True) never take this
lock.
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

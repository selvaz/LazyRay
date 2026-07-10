# -*- coding: utf-8 -*-
"""
connection.py — centralized access to LazyRay's own DuckDB database.

This is LazyRay's OWN storage (dalio_signals, pillar_scores, regime_state,
engine_scores, dalio_cycle_v2, country_classification) — fully decoupled
from market-data-hub's DuckDB file. Input data is read separately via
market_data_hub.reader's public API (read_macro_panel / read_macro_panel_ext).

The DB path is configurable via settings.yaml or the LAZYRAY_DB environment
variable. The schema is applied (idempotently) on first open.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Current schema version. Bump this whenever schema.sql changes shape and add a
# matching `if current < N:` branch in migrate() below.
SCHEMA_VERSION = 1


def _default_db() -> str:
    """Last-resort DB path when neither db_path, LAZYRAY_DB nor settings.yaml
    provide one. Kept repo-local so a clone is self-contained."""
    return str(_REPO_ROOT / "lazyray.duckdb")


_DEFAULT_DB = _default_db()


def _repo_local(path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(_REPO_ROOT / path)


def _resolve_db_path(db_path: Optional[str] = None) -> str:
    if db_path:
        return _repo_local(db_path)
    env = os.environ.get("LAZYRAY_DB")
    if env:
        return _repo_local(env)
    # settings.yaml takes precedence over the hard-coded default
    try:
        from lazyray.config_loader import get_settings
        s = get_settings()
        if s.get("db_path"):
            return _repo_local(s["db_path"])
    except Exception:
        pass
    return _DEFAULT_DB


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """True if a base table ``name`` already exists in the database."""
    try:
        row = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()
    except duckdb.Error:
        return False
    return row is not None


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Apply the SQL schema (idempotent) and record schema metadata.

    Mirrors market_data_hub.db.connection.apply_schema()'s freshness
    contract: only a genuinely new database (no dalio_signals yet, no
    recorded version) gets stamped with the current SCHEMA_VERSION here.
    """
    fresh = not _table_exists(con, "dalio_signals")
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    con.execute(sql)
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
        "('schema_applied_at', ?)",
        [now],
    )
    if fresh and get_schema_version(con) is None:
        con.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
            "('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )


def get_schema_version(con: duckdb.DuckDBPyConnection) -> Optional[int]:
    try:
        row = con.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except duckdb.Error:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def migrate(con: duckdb.DuckDBPyConnection) -> int:
    """Idempotent forward-migration entry point. Returns the resulting
    version. No migration steps exist yet (LazyRay's schema starts at v1) --
    this exists so a future schema change has a ladder to append to, the
    same shape as market_data_hub.db.connection.migrate()."""
    recorded = get_schema_version(con)
    apply_schema(con)

    if recorded is None:
        stamped = get_schema_version(con)
        if stamped is not None:
            return stamped
        recorded = 1

    current = max(recorded, SCHEMA_VERSION)
    con.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
        "('schema_version', ?)",
        [str(current)],
    )
    return current


def get_conn(db_path: Optional[str] = None, *, read_only: bool = False
             ) -> duckdb.DuckDBPyConnection:
    """
    Open (creating if absent) LazyRay's own DuckDB database and ensure the
    schema. read_only=True for readers so multiple processes can read in
    parallel without locking.
    """
    path = _resolve_db_path(db_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    if read_only and not os.path.exists(path):
        tmp = duckdb.connect(path)
        apply_schema(tmp)
        tmp.close()

    con = duckdb.connect(path, read_only=read_only)
    if not read_only:
        apply_schema(con)
    return con

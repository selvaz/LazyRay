# -*- coding: utf-8 -*-
"""Shared pytest fixtures and import-path setup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the hub's DB resolution (MARKET_DATA_DB) and LazyRay's own DB
    resolution (LAZYRAY_DB) at two separate throwaway DuckDB files -- the
    two are fully decoupled storage, exactly as they are outside tests."""
    hub_db = tmp_path / "hub.duckdb"
    own_db = tmp_path / "lazyray.duckdb"
    monkeypatch.setenv("MARKET_DATA_DB", str(hub_db))
    monkeypatch.setenv("LAZYRAY_DB", str(own_db))
    return str(own_db)

"""Stress-specific schema application and locked output writes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from lazyray.db.connection import get_conn
from lazyray.lock import db_write_lock

_SQL = Path(__file__).resolve().parents[1] / "db" / "schema_stress.sql"


def apply_schema(con) -> None:
    con.execute(_SQL.read_text(encoding="utf-8"))


def write_results(index_rows, component_rows, db_path=None) -> None:
    with db_write_lock(db_path):
        con = get_conn(db_path)
        try:
            apply_schema(con)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if index_rows:
                index_frame = pd.DataFrame(
                    [(*row, now) for row in index_rows],
                    columns=["date", "region", "index_value", "p_stress", "regime", "regime_hmm", "n_segments", "computed_at"],
                )
                con.register("_stress_index_write", index_frame)
                con.execute("INSERT OR REPLACE INTO stress_index (date, region, index_value, p_stress, regime, regime_hmm, n_segments, computed_at) SELECT date, region, index_value, p_stress, regime, regime_hmm, n_segments, computed_at FROM _stress_index_write")
                con.unregister("_stress_index_write")
            if component_rows:
                component_frame = pd.DataFrame(
                    [(*row, now) for row in component_rows],
                    columns=["date", "region", "segment", "indicator", "raw_value", "pct", "computed_at"],
                )
                con.register("_stress_component_write", component_frame)
                con.execute("INSERT OR REPLACE INTO stress_components SELECT * FROM _stress_component_write")
                con.unregister("_stress_component_write")
        finally:
            con.close()

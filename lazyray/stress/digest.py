"""Concise operator-facing summaries and notification gates."""
from __future__ import annotations

from .config import REGIONS

def _row(con, sql, params):
    return con.execute(sql, params).fetchone()


def build_digest(con, regions, as_of) -> str:
    lines = []
    for region in regions:
        row = _row(con, "SELECT date,index_value,p_stress,regime,regime_hmm,n_segments FROM stress_index WHERE region=? AND date<=? ORDER BY date DESC LIMIT 1", [region, as_of])
        if not row:
            continue
        dt, value, p, regime, regime_hmm, n_segments = row
        prior = con.execute("SELECT date,index_value FROM stress_index WHERE region=? AND date<? ORDER BY date DESC LIMIT 22", [region, dt]).fetchall()
        week = prior[4][1] if len(prior) > 4 else None
        month = prior[20][1] if len(prior) > 20 else None
        def change(previous):
            return "n/a" if previous is None else f"{(value-previous)*100:+.1f}pp"
        history = con.execute("SELECT index_value FROM stress_index WHERE region=? AND date<=?", [region, dt]).fetch_df().iloc[:, 0]
        pct = round(100 * (history <= value).mean())
        line = f"{region.upper()} {dt}: {value:.3f} (pct {pct}, 1w {change(week)}, 1m {change(month)}); {regime}, hmm {regime_hmm}, p_stress {p:.2f}"
        expected = len(REGIONS[region].segments)
        if n_segments < expected:
            present = {x[0] for x in con.execute("SELECT DISTINCT segment FROM stress_components WHERE region=? AND date=?", [region, dt]).fetchall()}
            absent = [segment.name for segment in REGIONS[region].segments if segment.name not in present]
            line += f" (partial: {n_segments}/{expected} segments; missing: {', '.join(absent)})"
        lines.append(line)
        comps = con.execute("SELECT segment, AVG(pct) p FROM stress_components WHERE region=? AND date=? GROUP BY segment ORDER BY p DESC LIMIT 2", [region, dt]).fetchall()
        if comps:
            lines.append("top segments: " + ", ".join(f"{x} {y:.2f}" for x, y in comps))
        previous = _row(con, "SELECT date,regime FROM stress_index WHERE region=? AND date<? ORDER BY date DESC LIMIT 1", [region, dt])
        if previous and previous[1] != regime:
            lines.append(f"NEW REGIME: {previous[1]}->{regime} since {dt}")
    return "\n".join(lines)


def should_notify(con, as_of) -> bool:
    regions = [x[0] for x in con.execute("SELECT DISTINCT region FROM stress_index WHERE date<=?", [as_of]).fetchall()]
    for region in regions:
        rows = con.execute("SELECT date,index_value,regime,n_segments FROM stress_index WHERE region=? AND date<=? ORDER BY date DESC LIMIT 2", [region, as_of]).fetchall()
        if not rows or rows[0][3] < 2:
            continue
        if len(rows) == 2 and rows[0][2] == "stress" and rows[1][2] != "stress":
            return True
        if rows:
            values = con.execute("SELECT index_value FROM stress_index WHERE region=? AND date<=?", [region, rows[0][0]]).fetch_df().iloc[:, 0]
            if len(values) >= 20 and rows[0][1] >= values.quantile(.9) and rows[1][1] < values.quantile(.9):
                return True
    return False

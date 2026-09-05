# -*- coding: utf-8 -*-
"""
test_brief.py — WP5 "LazyRay Brief" (docs/DALIO_PROD_ASSESSMENT_2026-09.md):
the one output a human reads. Seeds engine_scores/dalio_cycle_v2/run_meta/
stress_index/stress_components directly (LazyRay's own output tables), same
pattern as test_dalio_v2_digest.py -- no need to drive the real engines
through macro_panel for this.

Covers: the ASCII/<=14-line Telegram text, the six-section self-contained
HTML (well under 150KB even with 64 seeded countries), the FX-gate tag, the
DSA fan-chart line, the "no material movement" sentence when deltas are
tiny, the empty-stress-tables case, and the first-run (no prior ref_date)
case.
"""
from __future__ import annotations

import datetime as dt
import json

from market_data_hub.config_loader import get_countries

from lazyray.brief import build_brief_data, build_brief_text, render_brief_html
from lazyray.db.connection import get_conn as get_lazyray_conn
from lazyray.stress.persist import apply_schema as apply_stress_schema

DAY1 = dt.date(2026, 8, 15)
DAY2 = dt.date(2026, 9, 4)

_ENGINE_LABELS = {
    "sovereign_solvency": ["low", "moderate", "elevated", "high", "critical"],
    "external_constraint": ["low", "moderate", "elevated", "high", "severe"],
    "private_credit": ["low", "moderate", "elevated", "high", "bubble"],
    "funding_liquidity": ["easy", "normal", "watch", "stress", "severe"],
    "political_execution": ["strong", "adequate", "watch", "weak", "impaired"],
}
_ISO3 = [c["iso3"] for c in get_countries()]
assert len(_ISO3) == 64


def _audit(engine: str, score: float, *, gate=None, dsa=None, branch=None) -> dict:
    a = {
        "model_version": "abc1234", "data_through": "2025-12-31",
        "components": {"main": {"raw_value": round(score, 1), "score": score, "weight": 1}},
        "missing_components": [], "coverage_tier": "full", "vintage_safe": True,
    }
    if engine == "sovereign_solvency":
        a["gate"] = gate
        a["dsa"] = dsa
        if dsa:
            a["components"]["debt_p_up_5y"] = {"raw_value": dsa.get("p_up"), "score": 50.0, "weight": 1}
    if engine == "funding_liquidity":
        a["branch"] = branch or "market"
    return a


_UNSET = object()


def _label_for(engine: str, score: float) -> str:
    labels = _ENGINE_LABELS[engine]
    idx = min(int(score // 20), 4)
    return labels[idx]


def _insert_engine(con, iso3, ref_date, engine, score, audit, *, label=_UNSET,
                   relative_score=None, relative_label=None, coverage_tier="full"):
    label = _label_for(engine, score) if label is _UNSET else label
    con.execute(
        "INSERT INTO engine_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?, ?)",
        [iso3, ref_date, engine, score, label, coverage_tier, "high", 1, 1,
         json.dumps(audit), relative_score, relative_label])


def _insert_cycle(con, iso3, ref_date, stage, deleveraging="none"):
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
        [iso3, ref_date, stage, deleveraging, "high", "[]", "[]", "{}"])


def _insert_run_meta(con, ref_date, model_version="abc1234"):
    con.execute(
        "INSERT INTO run_meta VALUES (?, ?, ?, ?, ?, now())",
        [ref_date, "deadbeef", model_version,
         "external_constraint,funding_liquidity,political_execution,private_credit,"
         "sovereign_solvency", 64 * 5])


def _seed_full_universe(con, ref_date, *, arg_funding_score, arg_funding_label,
                        include_gate=True, include_dsa=True):
    """64 countries x 5 engines. ARG (index 0) carries the gate/DSA/mover
    story; NGA (index 1) has funding branch='none' (no data)."""
    for i, iso3 in enumerate(_ISO3):
        base = float(10 + (i % 5) * 15)  # deterministic spread across buckets
        for engine in _ENGINE_LABELS:
            if iso3 == _ISO3[0] and engine == "sovereign_solvency":
                gate = {"reason": "fx_debt_share+inflation", "fx_debt_share": 88.0,
                       "inflation": 35.0} if include_gate else None
                dsa = {"p_up": 0.85, "p10": 10.0, "p50": 50.0, "p90": 90.0} if include_dsa else None
                _insert_engine(con, iso3, ref_date, engine, 5.0,
                              _audit(engine, 5.0, gate=gate, dsa=dsa),
                              label="fx_constrained" if include_gate else None)
                continue
            if iso3 == _ISO3[0] and engine == "funding_liquidity":
                _insert_engine(con, iso3, ref_date, engine, arg_funding_score,
                              _audit(engine, arg_funding_score, branch="market"),
                              label=arg_funding_label)
                continue
            if iso3 == _ISO3[1] and engine == "funding_liquidity":
                _insert_engine(con, iso3, ref_date, engine, None,
                              _audit(engine, 0.0, branch="none"),
                              label=None, coverage_tier="no_data")
                continue
            if iso3 == _ISO3[0]:
                # ARG's other three engines score high too, so its gate/DSA/
                # mover story is guaranteed to land inside the "top 10 by
                # risk" section (rank_score is a mean across all 5 engines --
                # a country with a lone standout engine but four mild ones
                # would rank too low to reach section c's cards).
                _insert_engine(con, iso3, ref_date, engine, 90.0, _audit(engine, 90.0))
                continue
            _insert_engine(con, iso3, ref_date, engine, base, _audit(engine, base))
        _insert_cycle(con, iso3, ref_date,
                     "unclassified_no_funding_data" if iso3 == _ISO3[1] else "early_or_mid_cycle")
    _insert_run_meta(con, ref_date)


def _seed_stress(con, *, ea_partial=True):
    apply_stress_schema(con)
    dates = [DAY2 - dt.timedelta(days=d) for d in range(120, -1, -1)]
    for region, base, n_seg_full, top_segments in (
        ("us", 0.30, 5, [("credit", 0.7), ("rates", 0.6)]),
        ("ea", 0.20, 3, [("periphery", 0.5), ("bond", 0.3)]),
        ("em", 0.40, 3, [("hard_currency", 0.6), ("local_currency", 0.4)]),
    ):
        n_seg = 2 if (region == "ea" and ea_partial) else n_seg_full
        for i, d in enumerate(dates):
            value = base + 0.05 * (i / len(dates))
            regime = "elevated" if value > base + 0.03 else "calm"
            con.execute(
                "INSERT OR REPLACE INTO stress_index VALUES (?, ?, ?, ?, ?, ?, ?, now())",
                [d, region, value, 0.1, regime, "calm", n_seg])
            for seg_name, seg_pct in top_segments:
                con.execute(
                    "INSERT OR REPLACE INTO stress_components VALUES (?, ?, ?, ?, ?, ?, now())",
                    [d, region, seg_name, "indicator", value, seg_pct])


def test_full_brief_all_sections_size_and_gate_dsa(tmp_db):
    con = get_lazyray_conn()
    _seed_full_universe(con, DAY1, arg_funding_score=10.0, arg_funding_label="easy")
    con.commit()
    _seed_full_universe(con, DAY2, arg_funding_score=95.0, arg_funding_label="severe")
    con.commit()
    _seed_stress(con, ea_partial=True)
    con.commit()

    data = build_brief_data(con, DAY2)
    con.close()

    assert data.n_countries == 64
    assert len(data.countries) == 64
    top = data.countries[0]

    text = build_brief_text(data)
    text.encode("ascii")  # raises on any non-ASCII byte
    assert len(text.splitlines()) <= 14
    assert top.iso3 in text

    html = render_brief_html(data)
    html_bytes = html.encode("utf-8")
    assert len(html_bytes) < 150_000
    for title in ("Stress di mercato", "Rischio paese", "cambiato dall",
                 "Watchlist automatica", "Tabella completa", "Limiti"):
        assert title in html, f"missing section: {title}"
    # percentile marker for every region with data (3/3 here)
    assert html.count('aria-label="percentile') == 3
    # the FX gate tag for ARG (index 0)
    assert 'class="tag-fx"' in html
    assert "fx debt share" in html or "fx_debt_share" in html.replace(" + ", "_")
    # the DSA fan-chart line
    assert "p(debito in salita a 5 anni)" in html
    assert "DSA debito in salita a 5a" in text
    # 64 rows in the full table (+1 header row)
    assert html.count("<tr>") == 65
    # no leaked Python None/NaN anywhere in the page
    assert "NaN" not in html
    import re
    assert re.search(r"\bNone\b", html) is None
    # EA stress marked partial
    assert data.stress["ea"].partial is True
    assert "parziale" in text


def test_no_material_movement_sentence(tmp_db):
    con = get_lazyray_conn()
    _seed_full_universe(con, DAY1, arg_funding_score=40.0, arg_funding_label="watch")
    con.commit()
    # DAY2: every score shifts by well under the 2.0-point mover threshold
    _seed_full_universe(con, DAY2, arg_funding_score=40.5, arg_funding_label="watch")
    con.commit()

    data = build_brief_data(con, DAY2)
    con.close()

    assert not data.changes.movers
    text = build_brief_text(data)
    assert "nessun movimento materiale" in text
    html = render_brief_html(data)
    assert "Nessun movimento materiale" in html
    assert "revisione WEO/WDI" in html


def test_first_run_no_comparison(tmp_db):
    con = get_lazyray_conn()
    _seed_full_universe(con, DAY1, arg_funding_score=10.0, arg_funding_label="easy")
    con.commit()

    data = build_brief_data(con, DAY1)
    con.close()

    assert data.changes.first_run is True
    text = build_brief_text(data)
    assert "Prima corsa: nessun confronto disponibile" in text
    html = render_brief_html(data)
    assert "Prima corsa: nessun confronto disponibile" in html


def test_empty_stress_tables_do_not_raise(tmp_db):
    """stress_index/stress_components are created by
    lazyray.stress.persist.apply_schema(), never by lazyray.db.connection's
    own schema.sql -- a DB that has only ever run the 5-engine job (never
    run_stress_monitor.py) must still produce a usable Brief."""
    con = get_lazyray_conn()
    _insert_engine(con, "USA", DAY1, "sovereign_solvency", 20.0, _audit("sovereign_solvency", 20.0))
    _insert_cycle(con, "USA", DAY1, "early_or_mid_cycle")
    con.commit()

    data = build_brief_data(con, DAY1)
    con.close()

    assert all(not s.has_data for s in data.stress.values())
    text = build_brief_text(data)
    assert "Stress: nessun dato" in text
    html = render_brief_html(data)
    assert "nessun dato" in html


def test_empty_database_first_run(tmp_db):
    """A brand-new DB (schema applied, zero rows) is a legitimate "first
    run" -- never an exception."""
    con = get_lazyray_conn()
    data = build_brief_data(con, DAY1)
    con.close()

    assert data.n_countries == 0
    assert data.changes.first_run is True
    text = build_brief_text(data)
    text.encode("ascii")
    html = render_brief_html(data)
    assert len(html.encode("utf-8")) < 150_000

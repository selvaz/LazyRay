# -*- coding: utf-8 -*-
"""
test_dalio_v2_digest.py — Fase 1 deliverable E
(docs/DALIO_PROD_ASSESSMENT_2026-09.md §3.1.8): lazyray.dalio_v2.digest.
build_digest() produces a short plain-text Telegram digest -- label-change
counts per engine, top movers by |score delta| with the driving component,
cycle-stage changes, and a "Limits" line -- diffed against the previous
ref_date that exists in engine_scores, or "first run, no diff" when none
does.

Seeds engine_scores/dalio_cycle_v2 directly (LazyRay's own output tables),
same pattern as test_dalio_v2_cycle_classifier.py's compute()-focused
tests -- no need to drive the real engines through macro_panel for this.
"""
from __future__ import annotations

import datetime as dt
import json

from lazyray.dalio_v2.digest import build_digest
from lazyray.db.connection import get_conn as get_lazyray_conn

DAY1 = dt.date(2026, 8, 1)
DAY2 = dt.date(2026, 8, 2)


def _insert(con, iso3, ref_date, engine, score, label, audit, relative_label=None):
    con.execute(
        "INSERT INTO engine_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?, ?)",
        [iso3, ref_date, engine, score, label, "full", "high",
         len(audit.get("components", {})), len(audit.get("components", {})),
         json.dumps(audit), None, relative_label])


def test_first_run_has_no_earlier_ref_date(tmp_db):
    con = get_lazyray_conn()
    _insert(con, "USA", DAY1, "sovereign_solvency", 20.0, "stable", {"components": {}})
    con.commit()

    text = build_digest(con, DAY1)
    con.close()

    assert "first run, no diff" in text
    assert str(DAY1) in text
    assert "1 countries" in text
    assert "Limits:" in text


def test_digest_reports_label_changes_top_movers_and_limits(tmp_db):
    con = get_lazyray_conn()
    # DAY1: baseline
    _insert(con, "ARG", DAY1, "sovereign_solvency", 40.0, "watch", {
        "components": {"r_minus_g": {"score": 60.0, "raw_value": -5.0, "weight": 1}}})
    _insert(con, "ARG", DAY1, "funding_liquidity", 30.0, "watch", {"components": {}})
    _insert(con, "USA", DAY1, "sovereign_solvency", 10.0, "strong", {
        "components": {"r_minus_g": {"score": 5.0, "raw_value": -2.0, "weight": 1}}})
    con.commit()

    # DAY2: ARG's sovereign score jumps a lot (biggest mover), label flips to
    # 'stressed'; USA barely moves, no label change. funding_liquidity has no
    # DAY2 row at all (still fine -- only shared (country, engine) pairs are
    # compared).
    _insert(con, "ARG", DAY2, "sovereign_solvency", 85.0, "stressed", {
        "components": {"r_minus_g": {"score": 95.0, "raw_value": -15.0, "weight": 1},
                       "debt_gdp": {"score": 40.0, "raw_value": 90.0, "weight": 1}}})
    _insert(con, "USA", DAY2, "sovereign_solvency", 11.0, "strong", {
        "components": {"r_minus_g": {"score": 6.0, "raw_value": -1.9, "weight": 1}}})
    con.commit()

    text = build_digest(con, DAY2)
    con.close()

    assert f"as of {DAY2}" in text
    assert "sovereign_solvency: 1 label change(s)" in text
    assert f"vs {DAY1}" in text
    # ARG/sovereign_solvency is the top mover (|85-40| = 45 vs USA's |11-10| = 1)
    assert "ARG/sovereign_solvency:" in text and "(watch -> stressed)" in text
    # the biggest COMPONENT delta for ARG is r_minus_g (|95-60|=35 > debt_gdp,
    # which didn't exist on DAY1 so it's not "shared" and can't be picked)
    assert "(r_minus_g)" in text
    assert "Limits:" in text
    assert "not validated" in text


def test_digest_reports_cycle_stage_changes(tmp_db):
    con = get_lazyray_conn()
    _insert(con, "ARG", DAY1, "sovereign_solvency", 40.0, "watch", {"components": {}})
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
        ["ARG", DAY1, "early_or_mid_cycle", "none", "high", "[]", "[]", "{}"])
    con.commit()

    _insert(con, "ARG", DAY2, "sovereign_solvency", 85.0, "stressed", {"components": {}})
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
        ["ARG", DAY2, "late_long_debt_cycle", "ugly", "high", "[]", "[]", "{}"])
    con.commit()

    text = build_digest(con, DAY2)
    con.close()

    assert "Cycle-stage changes: 1" in text
    assert "ARG: early_or_mid_cycle -> late_long_debt_cycle" in text


def test_digest_null_stage_both_days_is_not_a_change(tmp_db):
    # Regression (found live against production data): DuckDB's fetch_df()
    # can turn an all-NULL dalio_stage batch into a float64 NaN column, and
    # a plain `!=` treats NaN != NaN as True -- so every still-
    # unclassifiable country was reported as a "cycle-stage change" on
    # every single run, even with zero actual state change.
    con = get_lazyray_conn()
    _insert(con, "XXX", DAY1, "sovereign_solvency", 40.0, "watch", {"components": {}})
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, now())",
        ["XXX", DAY1, "low", "[]", "[]", "{}"])
    con.commit()

    _insert(con, "XXX", DAY2, "sovereign_solvency", 41.0, "watch", {"components": {}})
    con.execute(
        "INSERT INTO dalio_cycle_v2 VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, now())",
        ["XXX", DAY2, "low", "[]", "[]", "{}"])
    con.commit()

    text = build_digest(con, DAY2)
    con.close()

    # zero cycle-stage changes reported, and no per-country line under that
    # section (XXX may still legitimately appear as a top mover by score
    # delta -- that's a separate, label-based section, not stage-based)
    assert "Cycle-stage changes: 0" in text
    assert "XXX: " not in text


def test_digest_no_label_changes_still_reports_zero_per_engine(tmp_db):
    con = get_lazyray_conn()
    _insert(con, "USA", DAY1, "sovereign_solvency", 10.0, "strong", {"components": {}})
    con.commit()
    _insert(con, "USA", DAY2, "sovereign_solvency", 10.5, "strong", {"components": {}})
    con.commit()

    text = build_digest(con, DAY2)
    con.close()

    assert "sovereign_solvency: 0 label change(s)" in text


def test_digest_is_ascii_safe_for_windows_logs(tmp_db):
    """The production job's stdout is cp1252 (Windows redirect): a single
    non-ASCII glyph in the digest crashed print() live on 2026-09-04."""
    from datetime import date

    from lazyray.dalio_v2.digest import build_digest
    from lazyray.db.connection import get_conn

    con = get_conn(tmp_db)
    try:
        text = build_digest(con, date(2026, 9, 4))
    finally:
        con.close()
    text.encode("ascii")

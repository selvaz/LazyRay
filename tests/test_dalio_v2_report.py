# -*- coding: utf-8 -*-
"""
test_dalio_v2_report.py — smoke coverage for dalio_v2/report.py (previously
completely untested: its only exercise was via run_dalio_v2.py in production)
plus a node syntax check of make_dalio_report.py's embedded JS, so a template
typo can't ship undetected.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from market_data_hub.db.connection import get_conn as get_hub_conn

from lazyray.dalio_v2 import report
from lazyray.dalio_v2.runner import run_dalio_v2
from lazyray.db.connection import get_conn as get_lazyray_conn
from tests.test_dalio_v2 import _seed

REF = dt.date(2026, 12, 31)


def _run_and_collect(tmp_db):
    con = get_hub_conn()
    _seed(con)
    con.commit()
    con.close()
    run_dalio_v2(engines=["sovereign_solvency", "political_execution"], ref_year=2026)
    con = get_lazyray_conn(read_only=True)
    df = report.collect(con, REF)
    return con, df


def test_generate_html_report_smoke(tmp_db, tmp_path):
    con, df = _run_and_collect(tmp_db)
    out = report.generate_html_report(con, REF, tmp_path)
    con.close()
    html = out.read_text(encoding="utf-8")
    assert "Sovereign Solvency" in html and "Political Execution" in html
    assert "max-width:640px" in html          # mobile rules present
    # the worst-bucket KPI counts the engines' TERMINAL labels: only ARG's
    # sovereign 'critical' row qualifies in this seed (ARG political is
    # 'weak', not the terminal 'impaired')
    kpis = dict(re.findall(r'<div class="kpi"><b>([^<]+)</b><span>([^<]+)', html))
    n_worst = [v for v, lbl in kpis.items() if "worst" in lbl]
    assert n_worst == ["1"]
    # obs_date shown in the components table
    assert "(2026-12-31)" in html


def test_to_csv_smoke(tmp_db, tmp_path):
    con, df = _run_and_collect(tmp_db)
    con.close()
    out = report.to_csv(df, tmp_path / "v2.csv")
    text = out.read_text(encoding="utf-8")
    assert "sovereign_solvency_score" in text.splitlines()[0]
    assert text.count("\n") >= 3              # header + 3 countries


def embedded_script(html: str) -> str:
    """The contents of the report's single ``<script>`` block.

    Read by locating the literal delimiters rather than by matching a tag with
    a regular expression. ``<script>(.*)</script>`` is a tag filter, and a
    regular expression cannot decide where a tag ends: CodeQL flags the shape
    (py/bad-tag-filter) because in a sanitiser it is a hole, and even here it
    would quietly return the wrong text the day the renderer emits
    ``<script type="module">`` -- the test would then check the syntax of
    something that is not the script.

    The renderer emits exactly one block, which is asserted rather than
    assumed: two would make "the embedded JS" an ambiguous phrase, and this
    test would silently start checking only the first.
    """
    apertura, chiusura = "<script>", "</script>"
    assert html.count(apertura) == 1, (
        f"attesa una sola apertura {apertura}, trovate {html.count(apertura)}"
    )
    assert html.count(chiusura) == 1, (
        f"attesa una sola chiusura {chiusura}, trovate {html.count(chiusura)}"
    )
    inizio = html.index(apertura) + len(apertura)
    fine = html.index(chiusura)
    assert fine > inizio, "la chiusura </script> precede l'apertura"
    return html[inizio:fine]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_embedded_js_is_syntactically_valid(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import make_dalio_report as mdr
    html = mdr.render_html({
        "now": "x", "cur_year": 2026, "weo_horizon": 2031, "phase_counts": {},
        "quad_counts": {}, "countries": {}, "chart_indicators": [], "has_v2": False,
    })
    js = embedded_script(html)
    p = tmp_path / "embedded.js"
    p.write_text(js, encoding="utf-8")
    res = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_cli_empty_panel_exits_nonzero(tmp_db, monkeypatch, capsys):
    # Codex review: cycle_classifier reports None (not 0) in the summary
    # when its gate is skipped, which used to make
    # `all(n == 0 for n in summary.values())` evaluate False even when every
    # requested engine scored zero countries (None == 0 is False) -- the CLI
    # then "succeeded" with a blank report instead of taking the error path.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import run_dalio_v2 as cli

    con = get_hub_conn()   # bootstraps an empty, schema'd hub DB
    con.close()

    monkeypatch.setattr(sys, "argv", ["run_dalio_v2.py", "--ref-year", "2026"])
    assert cli.main() == 1
    assert "No scores written" in capsys.readouterr().err

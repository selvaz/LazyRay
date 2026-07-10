# -*- coding: utf-8 -*-
"""
report.py — self-contained HTML + optional CSV snapshot of engine_scores for
a given ref_date.

Visual language matches the rest of the repo's reports (make_report.py,
make_dalio_report.py): light theme, blue accent, `.kpi`/`.card`/`.badge`
classes. Structurally simpler than make_dalio_report.py's dashboard — one
comparison table + one country-card section, sourced directly from
engine_scores rather than recomputing anything. Grows automatically as more
engines land in later phases (see
docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md) — no changes needed here.

Usage: see run_dalio_v2.py (repo root) for the CLI entry point.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import duckdb
import pandas as pd

# (score < this value) -> hex color; last bucket catches the rest. Applied
# via inline style="background:{color}" (never a CSS class) so it always
# renders regardless of what stylesheet classes exist or don't.
_COLOR_BUCKETS = [(20, "#16a34a"), (40, "#84cc16"), (60, "#d97706"),
                  (80, "#ea580c"), (101, "#b91c1c")]
_NA_COLOR = "#94a3b8"

_ENGINE_NAMES = {
    "sovereign_solvency": "Sovereign Solvency",
    "political_execution": "Political Execution",
    "private_credit": "Private Credit Cycle",
    "external_constraint": "External Currency Constraint",
    "funding_liquidity": "Funding Liquidity",
}


def _bucket_color(score: Optional[float]) -> str:
    """Hex color for a 0-100 score (gray if missing)."""
    if score is None or pd.isna(score):
        return _NA_COLOR
    for limit, color in _COLOR_BUCKETS:
        if score < limit:
            return color
    return _COLOR_BUCKETS[-1][1]


def _country_names() -> dict:
    """iso3 -> display name, from market-data-hub's static countries.yaml
    (package data of the market_data_hub dependency, not duplicated here)."""
    try:
        from market_data_hub.config_loader import get_countries
        return {c["iso3"]: c.get("name", c["iso3"]) for c in get_countries()}
    except Exception:
        return {}


def collect(con: duckdb.DuckDBPyConnection, ref_date,
           engines: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Long-format engine_scores for ref_date, one row per (country, engine),
    optionally filtered to a subset of engines."""
    q = ("SELECT country_iso3, engine, score, label, coverage_tier, confidence, "
        "n_components, n_expected, components_json, computed_at "
        "FROM engine_scores WHERE ref_date = ?")
    params = [ref_date]
    if engines:
        q += " AND engine IN (" + ",".join("?" * len(engines)) + ")"
        params += list(engines)
    return con.execute(q, params).fetch_df()


def to_csv(df: pd.DataFrame, out_path: Path) -> Path:
    """Wide CSV: one row per country, {engine}_{field} columns."""
    if df.empty:
        pd.DataFrame().to_csv(out_path)
        return out_path
    wide = df.pivot(index="country_iso3", columns="engine",
                    values=["score", "label", "coverage_tier", "confidence"])
    wide.columns = [f"{engine}_{field}" for field, engine in wide.columns]
    wide.sort_index().to_csv(out_path)
    return out_path


_STYLE = """
html{background:#f8fafc}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1a1a2e;margin:0;background:#f8fafc;font-size:14px}
header{background:#1d4ed8;color:#ffffff;padding:18px 24px}
header h1{margin:0;font-size:20px;color:#ffffff}
header p{margin:4px 0 0;font-size:12.5px;color:#ffffff}
main{max-width:1080px;margin:0 auto;padding:20px;background:#f8fafc;color:#1a1a2e}
h2{font-size:16px;color:#1d4ed8;border-bottom:1px solid #e2e8f0;padding-bottom:6px;margin-top:26px}
h3{color:#1a1a2e}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0;background:#ffffff;color:#1a1a2e}
th{background:#eef2ff;color:#1a1a2e;text-align:left;padding:7px 8px;border-bottom:2px solid #e2e8f0}
td{padding:5px 8px;border-bottom:1px solid #f1f5f9;color:#1a1a2e} td.n{text-align:right;font-variant-numeric:tabular-nums}
.kpi{display:inline-block;background:#eff6ff;border-radius:8px;padding:10px 16px;margin:4px 8px 4px 0}
.kpi b{display:block;font-size:20px;color:#1d4ed8} .kpi span{font-size:11px;color:#3f4b57}
select{font-size:15px;padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;min-width:280px;background:#ffffff;color:#1a1a2e}
.card{background:#ffffff;color:#1a1a2e;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-top:14px}
.badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;color:#ffffff}
.pbar{display:flex;align-items:center;gap:8px;margin:8px 0;font-size:12px;flex-wrap:wrap}
.pbar .pl{width:210px;flex-shrink:0;color:#1a1a2e;font-weight:600}
.pbar .pt{flex:1;min-width:80px;background:#eef2ff;border-radius:4px;height:16px;position:relative}
.pbar .pf{position:absolute;top:0;left:0;height:16px;border-radius:4px}
.pbar .pv{width:150px;text-align:right;flex-shrink:0;color:#3f4b57}
.tier-note{font-size:11px;color:#3f4b57;margin:-4px 0 10px 218px}
details{margin:4px 0 14px 218px;color:#1a1a2e} details summary{cursor:pointer;font-size:11px;color:#3f4b57}
.comp-table{margin:4px 0 0;max-width:100%;overflow-x:auto;display:block}
.comp-table td, .comp-table th{font-size:11.5px;padding:3px 8px;color:#1a1a2e;white-space:nowrap}
@media (max-width:640px){
 .pbar .pl{width:100%} .pbar .pv{width:100%;text-align:left}
 details,.tier-note{margin-left:4px}
}
.note{background:#fffbeb;color:#1a1a2e;border-left:4px solid #f59e0b;padding:10px 12px;border-radius:4px;margin:10px 0;font-size:13px}
.muted{color:#3f4b57;font-size:12px}
.country-card{display:none} .country-card.active{display:block}
"""

_HEADER_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Dalio v2 - engine scores ({ref_date})</title>
<style>{style}</style></head><body>
<header><h1>Dalio v2 &mdash; country risk engines</h1>
<p>As of {ref_date} &middot; generated {generated_at} UTC &middot; model_version {model_version}</p></header>
<main>
<div>
 <div class="kpi"><b>{n_countries}</b><span>countries scored</span></div>
 <div class="kpi"><b>{n_engines}</b><span>engines live</span></div>
 <div class="kpi"><b>{avg_score:.1f}</b><span>average risk (0-100)</span></div>
 <div class="kpi"><b>{n_worst}</b><span>country&middot;engine pairs in the worst bucket</span></div>
</div>
<p class="note"><strong>Not yet vintage-aware</strong> (live read of current values, not
point-in-time as of {ref_date} for historical dates) &mdash; see
docs/DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md. Higher score = worse. Badge
opacity in the comparison table and the coverage note on each bar signal
partial (proxy) or thin (insufficient) data coverage &mdash; never treat a
proxy-tier score as equivalent to a full one.</p>
"""

_COMPARE_TMPL = """
<h2>Comparison &mdash; all countries</h2>
<table>
<caption class="muted">Rows sorted by average risk across the engines shown (desc).</caption>
<thead><tr>{header_row}</tr></thead>
<tbody>{body_rows}</tbody>
</table>
"""

_CARDS_TMPL = """
<h2>Country sheet</h2>
<select id="csel" onchange="showCountry(this.value)">{options}</select>
{cards}
<script>
function showCountry(v){{
  document.querySelectorAll('.country-card').forEach(function(e){{e.classList.remove('active')}});
  document.getElementById('c-'+v).classList.add('active');
}}
</script>
"""

_FOOTER = """
</main>
<p class="muted" style="text-align:center;padding:20px">LazyRay &middot; Dalio v2 &middot; automatic report</p>
</body></html>
"""


def _comparison_table(pivot_score, pivot_label, pivot_tier, engines_present, names,
                      avg_score) -> str:
    # Plain dark-gray text, no colored badge/pill here (that bug is what made
    # this table unreadable: the badge relied on a CSS class (s0..s4) that
    # was never defined, so white badge text sat on no background at all).
    # td{color:#1a1a2e} from the stylesheet applies directly -- simplest and
    # most robust option, no per-score styling to get wrong.
    header_row = "<th>Country</th>" + "".join(
        f"<th>{_ENGINE_NAMES.get(e, e)}</th>" for e in engines_present)
    rows = []
    for iso3 in avg_score.index:
        cells = [f"<td>{names.get(iso3, iso3)} ({iso3})</td>"]
        for e in engines_present:
            score = pivot_score.loc[iso3, e] if e in pivot_score.columns else None
            label = pivot_label.loc[iso3, e] if e in pivot_label.columns else None
            tier = pivot_tier.loc[iso3, e] if e in pivot_tier.columns else None
            score_txt = "n/a" if score is None or pd.isna(score) else f"{score:.1f}"
            label_txt = "" if label is None or pd.isna(label) else str(label)
            tier_txt = tier if isinstance(tier, str) else "n/a"
            tier_suffix = f" [{tier_txt}]" if tier_txt not in ("full", "n/a") else ""
            sep = " &middot; " if label_txt else ""
            cells.append(f"<td>{score_txt}{sep}{label_txt}{tier_suffix}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return _COMPARE_TMPL.format(header_row=header_row, body_rows="".join(rows))


def _component_rows(components: dict) -> str:
    rows = []
    for name, c in components.items():
        raw = c.get("raw_value")
        # one malformed (non-numeric) raw_value must degrade to text, not
        # abort the whole report with an unhandled format error
        raw_txt = ("n/a" if raw is None
                   else f"{raw:g}" if isinstance(raw, (int, float)) else str(raw))
        score = c.get("score")
        score_txt = "n/a" if score is None or not isinstance(score, (int, float)) \
            else f"{score:.1f}"
        obs = c.get("obs_date")
        obs_txt = f' <span class="muted">({obs})</span>' if obs else ""
        rows.append(f"<tr><td>{name}{obs_txt}</td><td class=n>{raw_txt}</td>"
                   f"<td class=n>{score_txt}</td><td class=n>{c.get('weight', 0)}</td></tr>")
    return "".join(rows)


def _country_card(iso3: str, name: str, rows: pd.DataFrame, engines_present, active: bool) -> str:
    bars = []
    for e in engines_present:
        r = rows[rows["engine"] == e]
        if r.empty:
            continue
        r = r.iloc[0]
        score = r["score"]
        color = _bucket_color(score)
        pct = 0 if score is None or pd.isna(score) else max(0, min(100, score))
        score_txt = "n/a" if score is None or pd.isna(score) else f"{score:.1f}/100"
        label_txt = "" if pd.isna(r["label"]) else str(r["label"])
        sep = " &middot; " if label_txt else ""
        tier_txt = r["coverage_tier"] if isinstance(r["coverage_tier"], str) else "n/a"
        conf_txt = r["confidence"] if isinstance(r["confidence"], str) else "n/a"
        n_comp = "?" if pd.isna(r["n_components"]) else int(r["n_components"])
        n_exp = "?" if pd.isna(r["n_expected"]) else int(r["n_expected"])
        bars.append(
            f'<div class="pbar"><div class="pl">{_ENGINE_NAMES.get(e, e)}</div>'
            f'<div class="pt"><div class="pf" style="width:{pct}%;background:{color}"></div></div>'
            f'<div class="pv">{score_txt}{sep}{label_txt}</div></div>'
            f'<div class="tier-note">coverage: {tier_txt} &middot; '
            f'confidence: {conf_txt} &middot; {n_comp}/{n_exp} inputs</div>')
        try:
            components = json.loads(r["components_json"]).get("components", {})
        except Exception:
            components = {}
        if components:
            bars.append(
                '<details><summary>components</summary>'
                '<table class="comp-table"><thead><tr><th>input</th><th class=n>raw value</th>'
                f'<th class=n>risk score</th><th class=n>weight</th></tr></thead>'
                f'<tbody>{_component_rows(components)}</tbody></table></details>')
    cls = "country-card active" if active else "country-card"
    return (f'<div class="card {cls}" id="c-{iso3}"><h3>{name} ({iso3})</h3>{"".join(bars)}</div>')


def generate_html_report(con: duckdb.DuckDBPyConnection, ref_date, out_dir: Path,
                         engines: Optional[Sequence[str]] = None) -> Path:
    df = collect(con, ref_date, engines)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"dalio_v2_{ref_date}.html"
    if df.empty:
        out_path.write_text(
            f"<p>No engine_scores rows for ref_date={ref_date}. "
            f"Run run_dalio_v2.py first.</p>", encoding="utf-8")
        return out_path

    names = _country_names()
    engines_present = sorted(df["engine"].unique())
    model_version = "unknown"
    try:
        model_version = json.loads(df["components_json"].iloc[0]).get("model_version", "unknown")
    except Exception:
        pass

    pivot_score = df.pivot(index="country_iso3", columns="engine", values="score")
    pivot_label = df.pivot(index="country_iso3", columns="engine", values="label")
    pivot_tier = df.pivot(index="country_iso3", columns="engine", values="coverage_tier")
    avg_score = pivot_score.mean(axis=1, skipna=True).sort_values(ascending=False)

    # count rows sitting in each engine's TERMINAL bucket (from config), not
    # rows sharing whatever label the max-score row happens to carry -- when
    # nobody is in the terminal bucket the KPI must read 0, not count the
    # mildest achieved label as "worst"
    from lazyray.config_loader import get_settings
    v2cfg = get_settings().get("dalio_v2", {})
    n_worst = 0
    for e in engines_present:
        terminal = (v2cfg.get(e, {}).get("bucket_labels") or [None])[-1]
        if terminal is not None:
            n_worst += int((df.loc[df["engine"] == e, "label"] == terminal).sum())

    header_html = _HEADER_TMPL.format(
        ref_date=ref_date, generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        model_version=model_version, n_countries=len(avg_score), n_engines=len(engines_present),
        avg_score=float(avg_score.mean()) if len(avg_score) else 0.0, n_worst=n_worst,
        style=_STYLE)

    compare_html = _comparison_table(pivot_score, pivot_label, pivot_tier, engines_present,
                                     names, avg_score)

    options = "".join(
        f'<option value="{iso3}">{names.get(iso3, iso3)} ({iso3})</option>'
        for iso3 in avg_score.index)
    cards = "".join(
        _country_card(iso3, names.get(iso3, iso3), df[df["country_iso3"] == iso3],
                     engines_present, active=(i == 0))
        for i, iso3 in enumerate(avg_score.index))
    cards_html = _CARDS_TMPL.format(options=options, cards=cards)

    out_path.write_text(header_html + compare_html + cards_html + _FOOTER, encoding="utf-8")
    return out_path

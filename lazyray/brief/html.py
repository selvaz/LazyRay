# -*- coding: utf-8 -*-
"""
html.py — self-contained HTML render of the LazyRay Brief
(docs/DALIO_PROD_ASSESSMENT_2026-09.md: the report the owner actually reads;
the old dalio_v2/report.py 64-row-table HTML stays as the untouched monthly
detail artifact).

No external assets, no JS libraries (none needed -- <details> is native).
Inline CSS + inline SVG (percentile bars, sparklines). Mobile-first
(max-width 720px), system font stack, tabular numerals, light/dark via
prefers-color-scheme. render_brief_html(data) is pure: string in, string
out, no DB/filesystem access.
"""
from __future__ import annotations

from html import escape as _esc
from typing import Optional

from .data import ENGINE_ORDER, REGION_NAME_IT, REGIONS, BriefData, _bucket_index

_ENGINE_LABEL_IT = {
    "sovereign_solvency": "Sovrano", "external_constraint": "Esterno",
    "private_credit": "Privato", "funding_liquidity": "Funding",
    "political_execution": "Politico",
}
_REGIME_LABEL_IT = {"calm": "calmo", "elevated": "elevato", "stress": "stress"}
_REGIME_VAR = {"calm": "--accent", "elevated": "--ochre", "stress": "--oxblood"}
_TOP_N_COUNTRIES = 10


def _humanize(s: Optional[str]) -> str:
    return s.replace("_", " ") if s else "n/d"


def _num(v, fmt="{:.1f}") -> str:
    if v is None or (isinstance(v, float) and v != v):  # NaN != NaN
        return "n/d"
    return fmt.format(v)


_STYLE = """
:root{
 --bg:#F6F7F4;--ink:#1B2229;--muted:#5A6470;--rule:#D9DED6;
 --accent:#0F6E74;--ochre:#B8741A;--ochre-soft-bg:#F1DFC4;--ochre-soft-fg:#6B4A12;
 --oxblood:#8A2E2E;--good:#3D7A4A;--card-bg:#FFFFFF;
}
@media (prefers-color-scheme: dark){
 :root:not([data-theme="light"]){
  --bg:#12171B;--ink:#E6EAEE;--muted:#98A3AE;--rule:#2C353D;
  --accent:#4FB3B9;--ochre:#D99A3E;--ochre-soft-bg:#4A3B1E;--ochre-soft-fg:#F0D9A8;
  --oxblood:#D46A6A;--good:#5FAE72;--card-bg:#1A2126;
 }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
 font-size:15px;line-height:1.45}
main{max-width:720px;margin:0 auto;padding:16px 16px 40px}
h1{font-size:19px;margin:0 0 2px}
h2{font-size:15px;margin:28px 0 2px;color:var(--ink);border-top:1px solid var(--rule);padding-top:16px}
.section-note{font-size:12.5px;color:var(--muted);margin:0 0 12px}
.hdr-meta{font-size:12.5px;color:var(--muted);margin:2px 0}
.hdr-diff{font-size:13px;margin-top:8px;color:var(--ink)}
.num{font-variant-numeric:tabular-nums}
.region-row{border:1px solid var(--rule);border-radius:10px;padding:10px 12px;margin:8px 0;
 background:var(--card-bg)}
.region-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px}
.region-name{font-weight:600;min-width:110px}
.chip{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600;
 white-space:nowrap}
.chip-neutral{background:var(--rule);color:var(--ink)}
.chip-ochre-soft{background:var(--ochre-soft-bg);color:var(--ochre-soft-fg)}
.chip-ochre{background:var(--ochre);color:#fff}
.chip-oxblood{background:var(--oxblood);color:#fff}
.chip-fx{background:transparent;border:2px solid var(--oxblood);color:var(--oxblood)}
.chip-nodata{background:transparent;border:1px dashed var(--muted);color:var(--muted)}
.region-detail{font-size:12px;color:var(--muted);margin-top:4px}
.region-partial{color:var(--oxblood);font-weight:600}
.spark{display:block;margin-top:6px}
.ccard{border:1px solid var(--rule);border-radius:10px;padding:10px 12px;margin:8px 0;
 background:var(--card-bg)}
.ccard-head{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.ccard-head .name{font-weight:600;font-size:14.5px}
.ccard-head .iso{color:var(--muted);font-size:12px}
.ccard-head .grp{font-size:11px;color:var(--muted);border:1px solid var(--rule);
 border-radius:8px;padding:0 6px}
.echips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.echip{border:1px solid var(--rule);border-radius:8px;padding:4px 8px;font-size:11.5px;
 min-width:92px}
.echip .elabel{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;
 letter-spacing:.02em;margin-bottom:2px}
.echip .erel{display:block;color:var(--muted);font-size:10.5px;margin-top:2px}
.tag-fx{display:inline-block;margin-top:6px;font-size:11.5px;color:var(--oxblood);
 border:1px solid var(--oxblood);border-radius:6px;padding:1px 6px}
.tag-branch,.tag-stage{display:block;margin-top:5px;font-size:12px;color:var(--muted)}
.tag-dsa{display:block;margin-top:5px;font-size:12px;color:var(--ink)}
ul.plain{margin:6px 0;padding-left:18px;font-size:13px}
ul.plain li{margin:3px 0}
.muted{color:var(--muted)}
details{margin:10px 0}
summary{cursor:pointer;font-size:13px;color:var(--accent);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:11.5px;margin-top:8px}
th,td{padding:4px 6px;border-bottom:1px solid var(--rule);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
td.num{text-align:right}
footer{margin-top:30px;font-size:11px;color:var(--muted);text-align:center}
"""

_SECTION_NOTES = {
    "stress": "Quanto e' teso il finanziamento sui mercati oggi, aggiornato ogni giorno "
             "(non e' una revisione del rischio-paese).",
    "risk": "I dieci paesi con il punteggio di rischio medio piu' alto rispetto ai loro pari "
           "(non in assoluto).",
    "changes": "Che cosa e' cambiato rispetto all'ultima corsa e perche', componente per "
              "componente.",
    "watch": "Paesi che meritano attenzione ora, per almeno un motivo esplicito qui sotto.",
    "table": "Tutti i paesi, tutti i motori: la tabella completa, ripiegata di default.",
    "limits": "Che cosa questo numero NON dice: dove i dati sono deboli o mancanti.",
}


def _chip_class(engine: str, label: Optional[str]) -> str:
    if not label:
        return "chip chip-nodata"
    if label == "fx_constrained":
        return "chip chip-fx"
    idx = _bucket_index(engine, label)
    if idx is None:
        return "chip chip-neutral"
    return "chip " + ["chip-neutral", "chip-neutral", "chip-ochre-soft",
                      "chip-ochre", "chip-oxblood"][min(idx, 4)]


def _percentile_bar_svg(pct: float, regime: Optional[str], w: int = 260, h: int = 20) -> str:
    color_var = _REGIME_VAR.get(regime or "", "--muted")
    x = max(0.0, min(100.0, pct)) / 100.0 * w
    return (
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        f'aria-label="percentile {pct:.0f} su 100">'
        f'<rect x="0" y="{h / 2 - 3:.1f}" width="{w}" height="6" rx="3" style="fill:var(--rule)"/>'
        f'<line x1="{0.6 * w:.1f}" y1="2" x2="{0.6 * w:.1f}" y2="{h - 2}" '
        f'style="stroke:var(--muted);stroke-width:1"/>'
        f'<line x1="{0.85 * w:.1f}" y1="2" x2="{0.85 * w:.1f}" y2="{h - 2}" '
        f'style="stroke:var(--muted);stroke-width:1"/>'
        f'<circle cx="{x:.1f}" cy="{h / 2:.1f}" r="5" '
        f'style="fill:var({color_var});stroke:var(--ink);stroke-width:1"/></svg>'
    )


def _sparkline_svg(history, w: int = 240, h: int = 40) -> str:
    if not history:
        return ""
    vals = [p.index_value for p in history]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(history)
    bar_w = w / n
    step = w / max(n - 1, 1)
    bands = []
    for i, p in enumerate(history):
        color_var = _REGIME_VAR.get(p.regime or "", "--muted")
        bands.append(
            f'<rect x="{i * bar_w:.1f}" y="0" width="{bar_w + 0.6:.1f}" height="{h}" '
            f'style="fill:var({color_var});fill-opacity:.14"/>')
    points = []
    for i, p in enumerate(history):
        x = i * step
        y = h - 4 - (p.index_value - lo) / span * (h - 8)
        points.append(f"{x:.1f},{y:.1f}")
    polyline = (f'<polyline points="{" ".join(points)}" fill="none" '
               f'style="stroke:var(--accent);stroke-width:1.6"/>')
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        f'aria-label="andamento indice di stress, ultimi {n} giorni di borsa">'
        f'{"".join(bands)}{polyline}</svg>'
    )


def _header_html(data: BriefData) -> str:
    it_months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
                "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    as_of_it = f"{data.as_of.day} {it_months[data.as_of.month - 1]} {data.as_of.year}"
    if data.changes.first_run:
        diff = "prima corsa"
    else:
        diff = (f"Rispetto al {data.changes.prev_ref_date}: "
               f"etichette cambiate {len(data.changes.label_changes)}, "
               f"stadi cambiati {len(data.changes.stage_changes)}")
    return (
        f'<h1>LazyRay Brief</h1>'
        f'<p class="hdr-meta">{_esc(as_of_it)} &middot; dati fino al {data.data_through_year} '
        f'&middot; modello {_esc(data.model_version)} &middot; {data.n_countries} paesi</p>'
        f'<p class="hdr-diff">{_esc(diff)}</p>'
    )


def _stress_section_html(data: BriefData) -> str:
    rows = []
    any_data = False
    for region in REGIONS:
        s = data.stress.get(region)
        if not s or not s.has_data:
            continue
        any_data = True
        regime_it = _REGIME_LABEL_IT.get(s.regime or "", s.regime or "n/d")
        chip_cls = {"calm": "chip-neutral", "elevated": "chip-ochre",
                   "stress": "chip-oxblood"}.get(s.regime or "", "chip-neutral")
        partial_txt = ""
        if s.partial:
            partial_txt = (f' <span class="region-partial">parziale '
                           f'({s.n_segments}/{s.expected_segments} segmenti')
            if s.missing_segments:
                partial_txt += f'; mancano: {_esc(", ".join(s.missing_segments))}'
            partial_txt += ")</span>"
        top_segs = ", ".join(f"{_esc(name)} {pct:.2f}" for name, pct in s.top_segments) or "n/d"
        week = "n/d" if s.week_change_pp is None else f"{s.week_change_pp:+.1f}pp"
        month = "n/d" if s.month_change_pp is None else f"{s.month_change_pp:+.1f}pp"
        rows.append(
            f'<div class="region-row">'
            f'<div class="region-head">'
            f'<span class="region-name">{_esc(REGION_NAME_IT[region])}</span>'
            f'{_percentile_bar_svg(s.pct or 0.0, s.regime)}'
            f'<span class="num">{s.pct:.0f} pct</span>'
            f'<span class="chip {chip_cls}">{_esc(regime_it)}</span>{partial_txt}'
            f'</div>'
            f'<div class="region-detail">indice {s.index_value:.3f} &middot; 1s {week} '
            f'&middot; 1m {month} &middot; segmenti principali: {top_segs}</div>'
            f'{_sparkline_svg(s.history)}'
            f'</div>'
        )
    if not any_data:
        rows.append('<p class="muted">Stress monitor: nessun dato ancora.</p>')
    return (
        f'<h2>Stress di mercato (giornaliero)</h2>'
        f'<p class="section-note">{_SECTION_NOTES["stress"]}</p>'
        + "".join(rows)
    )


def _dsa_line_html(country) -> str:
    if not country.dsa:
        return ""
    d = country.dsa
    p_up_txt = "n/d" if d.get("p_up") is None else f"{d['p_up'] * 100:.0f}%"
    return (f'<span class="tag-dsa">p(debito in salita a 5 anni) = {p_up_txt} '
           f'(p10 {d["p10"]:.0f} &middot; p50 {d["p50"]:.0f} &middot; p90 {d["p90"]:.0f})</span>')


def _country_card_html(country) -> str:
    chips = []
    for engine in ENGINE_ORDER:
        cell = country.engines.get(engine)
        label = cell.label if cell else None
        rel = cell.relative_score if cell else None
        cls = _chip_class(engine, label)
        # percentile among peers (DM/EM/Frontier), higher = riskier: a number,
        # not a reused bucket word ("peer: bubble" read as a verdict)
        rel_html = f'<span class="erel">vs pari {rel:.0f}</span>' if rel is not None else ""
        chips.append(
            f'<div class="echip"><span class="elabel">{_ENGINE_LABEL_IT[engine]}</span>'
            f'<span class="{cls}">{_esc(label) if label else "n/d"}</span>{rel_html}</div>')
    fx_html = ""
    if country.gate:
        reason = (country.gate.get("reason") or "").replace("_", " ").replace("+", " + ")
        fx_html = f'<span class="tag-fx">FX: {_esc(reason)}</span>'
    branch_html = ""
    if country.funding_branch:
        branch_it = {"market": "mercato", "external": "estero",
                    "none": "nessun dato"}.get(country.funding_branch, country.funding_branch)
        branch_html = f'<span class="tag-branch">funding: {_esc(branch_it)}</span>'
    stage_html = f'<span class="tag-stage">fase ciclo: {_esc(_humanize(country.cycle_stage))}</span>'
    return (
        f'<div class="ccard">'
        f'<div class="ccard-head"><span class="name">{_esc(country.name)}</span>'
        f'<span class="iso">{country.iso3}</span><span class="grp">{country.group}</span></div>'
        f'<div class="echips">{"".join(chips)}</div>'
        f'{fx_html}{branch_html}{_dsa_line_html(country)}{stage_html}'
        f'</div>'
    )


def _risk_section_html(data: BriefData) -> str:
    top = data.countries[:_TOP_N_COUNTRIES]
    body = "".join(_country_card_html(c) for c in top) if top else \
        '<p class="muted">Nessun paese con punteggio disponibile.</p>'
    return (
        f'<h2>Rischio paese &mdash; i {_TOP_N_COUNTRIES} da guardare</h2>'
        f'<p class="section-note">{_SECTION_NOTES["risk"]}</p>{body}'
    )


def _changes_section_html(data: BriefData) -> str:
    ch = data.changes
    if ch.first_run:
        body = '<p>Prima corsa: nessun confronto disponibile.</p>'
    else:
        parts = []
        if ch.label_changes:
            items = []
            for c in ch.label_changes:
                comp_txt = ""
                if c.component:
                    old_v = _num(c.old_component_score)
                    new_v = _num(c.new_component_score)
                    comp_txt = f' (componente: {_esc(c.component)} {old_v} -&gt; {new_v})'
                items.append(
                    f'<li>{c.country} {_ENGINE_LABEL_IT.get(c.engine, c.engine)}: '
                    f'{_esc(_humanize(c.old_label))} -&gt; {_esc(_humanize(c.new_label))}'
                    f'{comp_txt}</li>')
            parts.append(f'<ul class="plain">{"".join(items)}</ul>')
        else:
            parts.append('<p class="muted">Nessuna etichetta cambiata.</p>')
        if ch.stage_changes:
            items = "".join(
                f'<li>{c.country}: {_esc(_humanize(c.old_stage))} -&gt; '
                f'{_esc(_humanize(c.new_stage))}</li>' for c in ch.stage_changes)
            parts.append(f'<p><strong>Cambi di fase</strong></p><ul class="plain">{items}</ul>')
        if ch.movers:
            items = "".join(
                f'<li>{m.country} {_ENGINE_LABEL_IT.get(m.engine, m.engine)}: '
                f'{_num(m.old_score)} -&gt; {_num(m.new_score)} ({m.delta:+.1f})'
                f'{" (" + _esc(m.component) + ")" if m.component else ""}</li>'
                for m in ch.movers)
            parts.append(f'<p><strong>Movimenti maggiori</strong></p><ul class="plain">{items}</ul>')
        else:
            parts.append(
                '<p><strong>Movimenti maggiori</strong><br>Nessun movimento materiale: i dati '
                'di input sono annuali; il prossimo aggiornamento sostanziale arriva con la '
                'revisione WEO/WDI.</p>')
        body = "".join(parts)
    return (
        f'<h2>Cosa e\' cambiato dall\'ultima corsa</h2>'
        f'<p class="section-note">{_SECTION_NOTES["changes"]}</p>{body}'
    )


def _watch_section_html(data: BriefData) -> str:
    if not data.watchlist:
        body = '<p class="muted">Nessuna voce in watchlist.</p>'
    else:
        items = "".join(
            f'<li>{_esc(w.name)} ({w.iso3}): {_esc(", ".join(w.reasons))}</li>'
            for w in data.watchlist)
        body = f'<ul class="plain">{items}</ul>'
    return (
        f'<h2>Watchlist automatica</h2>'
        f'<p class="section-note">{_SECTION_NOTES["watch"]}</p>{body}'
    )


def _full_table_html(data: BriefData) -> str:
    header = "<th>Paese</th>" + "".join(f"<th>{_ENGINE_LABEL_IT[e]}</th>" for e in ENGINE_ORDER)
    rows = []
    for c in data.countries:
        cells = [f"<td>{_esc(c.name)} ({c.iso3})</td>"]
        for engine in ENGINE_ORDER:
            cell = c.engines.get(engine)
            label = cell.label if cell else None
            score = cell.score if cell else None
            txt = "n/d" if label is None else f"{_esc(label)} &middot; {_num(score)}"
            cells.append(f'<td class="num">{txt}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    table = f'<table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    return (
        f'<h2>Tabella completa</h2>'
        f'<p class="section-note">{_SECTION_NOTES["table"]}</p>'
        f'<details><summary>{len(data.countries)} paesi &times; {len(ENGINE_ORDER)} motori '
        f'&mdash; espandi</summary>{table}</details>'
    )


def _limits_section_html(data: BriefData) -> str:
    lim = data.limits
    items = [
        "Non validato contro episodi di crisi storici (nessun backtest ancora eseguito).",
        "Molti punteggi sono a livello proxy: guardare sempre il tier di copertura, mai solo "
        "il numero.",
        f"Livelli correnti da actual fino al {lim.data_through_year} (WEO/WDI/WGI sono annuali "
        f"con ritardo di pubblicazione).",
        "Lo stress dei mercati emergenti e' ricostruito via ETF (EMB/EMLC), non paese per "
        "paese.",
    ]
    if lim.stress_partial_regions:
        names = ", ".join(REGION_NAME_IT[r] for r in lim.stress_partial_regions)
        items.append(f"Stress {names}: copertura parziale (rendimenti mensili non aggiornati).")
    items.append(
        f"Copertura dati: {lim.coverage_counts.get('proxy', 0)} righe proxy, "
        f"{lim.coverage_counts.get('insufficient', 0)} insufficienti, "
        f"{lim.coverage_counts.get('no_data', 0)} senza dati (su "
        f"{lim.coverage_counts.get('proxy', 0) + lim.coverage_counts.get('insufficient', 0) + lim.coverage_counts.get('no_data', 0)} "
        f"totale non-full).")
    body = "".join(f"<li>{i}</li>" for i in items)
    return (
        f'<h2>Limiti</h2>'
        f'<p class="section-note">{_SECTION_NOTES["limits"]}</p>'
        f'<ul class="plain">{body}</ul>'
    )


def render_brief_html(data: BriefData) -> str:
    """Self-contained HTML (no external assets, no JS libraries) for
    reports/brief/lazyray_brief_<ref_date>.html. Pure: data in, string out."""
    head = (
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="color-scheme" content="light dark">'
        f'<title>LazyRay Brief &mdash; {data.as_of}</title>'
        f'<style>{_STYLE}</style>'
    )
    body = (
        f'<main>'
        f'{_header_html(data)}'
        f'{_stress_section_html(data)}'
        f'{_risk_section_html(data)}'
        f'{_changes_section_html(data)}'
        f'{_watch_section_html(data)}'
        f'{_full_table_html(data)}'
        f'{_limits_section_html(data)}'
        f'<footer>LazyRay Brief &middot; generato automaticamente, non e\' consulenza '
        f'finanziaria</footer>'
        f'</main>'
    )
    return f'<!doctype html><html lang="it"><head>{head}</head><body>{body}</body></html>'

# -*- coding: utf-8 -*-
"""
text.py — the ASCII, <=14-line Telegram message for the LazyRay Brief
(docs/DALIO_PROD_ASSESSMENT_2026-09.md: replaces both the old raw digest
text and the 398KB HTML-with-fixed-caption as what actually gets sent).

build_brief_text(data) is pure: it renders lazyray.brief.data.BriefData,
never touches the DB or the filesystem. The "HTML allegato: <name>" line is
deliberately NOT produced here -- which file (if any) gets attached is a
send_telegram_report.py runtime decision (the Brief HTML by default, the old
full report only with --attach-full), appended by the caller once it knows.
"""
from __future__ import annotations

from .data import (
    dsa_is_material,
    ENGINE_SHORT, REGION_CODE, REGIONS, BriefData,
)

_MONTHS_IT = ["gen", "feb", "mar", "apr", "mag", "giu",
             "lug", "ago", "set", "ott", "nov", "dic"]

_MAX_WATCH = 6


def _short_date(d) -> str:
    return f"{d.day} {_MONTHS_IT[d.month - 1]} {d.year}"


def _ddmm(d) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def _stress_line(data: BriefData) -> str:
    parts = []
    for region in REGIONS:
        s = data.stress.get(region)
        if not s or not s.has_data:
            continue
        bits = [f"{s.pct}pct", s.regime or "n/d"]
        if s.partial:
            bits.append("parziale")
        week = "n/d" if s.week_change_pp is None else f"{s.week_change_pp:+.1f}pp/1s"
        month = "n/d" if s.month_change_pp is None else f"{s.month_change_pp:+.1f}pp/1m"
        parts.append(f"{REGION_CODE[region]} {' '.join(bits)} ({week}, {month})")
    if not parts:
        return "Stress: nessun dato"
    return "Stress: " + " | ".join(parts)


def _watchlist_line(data: BriefData) -> str:
    if not data.watchlist:
        return "Da guardare: nessuno"
    items = []
    for w in data.watchlist[:_MAX_WATCH]:
        items.append(f"{w.iso3} ({', '.join(w.reasons)})" if w.reasons else w.iso3)
    return "Da guardare: " + ", ".join(items)


_MAX_DSA = 6


def _dsa_line(data: BriefData) -> "str | None":
    hits: list[tuple[str, float]] = []
    for c in data.countries:
        if not dsa_is_material(c, 0.6):
            continue
        dsa = c.dsa
        assert dsa is not None  # dsa_is_material(c, 0.6) already proved c.dsa is truthy
        hits.append((c.iso3, dsa["p_up"]))
    if not hits:
        return None
    hits.sort(key=lambda x: -x[1])
    shown = hits[:_MAX_DSA]
    line = "DSA debito in salita a 5a (p>=0.6, debito non basso): " + ", ".join(f"{iso3} {p:.2f}" for iso3, p in shown)
    if len(hits) > _MAX_DSA:
        line += f" (+{len(hits) - _MAX_DSA} altri)"
    return line


def _changes_line(data: BriefData) -> str:
    ch = data.changes
    if ch.first_run:
        return "Prima corsa: nessun confronto disponibile"
    movement = "nessun movimento materiale" if not ch.movers else (
        "movimenti: " + ", ".join(
            f"{m.country}/{ENGINE_SHORT[m.engine]} {m.delta:+.1f}" for m in ch.movers))
    return (f"Dal {_ddmm(ch.prev_ref_date)}: etichette cambiate {len(ch.label_changes)}, "
           f"{len(ch.stage_changes)} stadi; {movement}")


def _limits_line(data: BriefData) -> str:
    parts = ["non validato"]
    for region in data.limits.stress_partial_regions:
        parts.append(f"{REGION_CODE[region]} stress parziale")
    parts.append(f"livelli da actual {data.limits.data_through_year}")
    return "Limiti: " + "; ".join(parts)


def build_brief_text(data: BriefData) -> str:
    """<=14-line ASCII Telegram message. See module docstring: the caller
    appends the attachment line once the send decision is known."""
    lines = [
        f"LazyRay Brief | {_short_date(data.as_of)} | dati {data.data_through_year} | "
        f"{data.n_countries} paesi | modello {data.model_version}",
        _stress_line(data),
        _watchlist_line(data),
    ]
    dsa = _dsa_line(data)
    if dsa:
        lines.append(dsa)
    lines.append(_changes_line(data))
    lines.append(_limits_line(data))
    text = "\n".join(lines)
    return text.encode("ascii", errors="replace").decode("ascii")

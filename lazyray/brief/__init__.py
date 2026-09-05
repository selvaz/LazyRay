# -*- coding: utf-8 -*-
"""
lazyray.brief — the "LazyRay Brief": the single human-facing output
(docs/DALIO_PROD_ASSESSMENT_2026-09.md). Replaces both the old plain-text
digest and the 398KB dalio_v2/report.py HTML as what actually gets sent --
report.py stays as the untouched, monthly detail artifact.

build_brief(con, ref_date) -> (text, html) is the one entry point callers
need; data.py/text.py/html.py are separated for testability (each layer
takes the previous one's pure output, no DB access below build_brief_data).
"""
from __future__ import annotations

from .data import BriefData, build_brief_data
from .html import render_brief_html
from .text import build_brief_text

__all__ = ["BriefData", "build_brief_data", "build_brief_text", "render_brief_html",
          "build_brief"]


def build_brief(con, ref_date):
    """(text, html) for `ref_date`, reading only LazyRay's own DuckDB."""
    data = build_brief_data(con, ref_date)
    return build_brief_text(data), render_brief_html(data)

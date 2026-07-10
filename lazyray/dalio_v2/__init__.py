# -*- coding: utf-8 -*-
"""
dalio_v2 — 5-engine architecture (additive layer on top of dalio.py).

See docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md for the design. Writes
to engine_scores only; never touches dalio_signals/pillar_scores/regime_state.
"""

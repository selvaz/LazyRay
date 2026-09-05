# -*- coding: utf-8 -*-
"""
validate_config.py — consistency checks for lazyray/config/settings.yaml.

Moved from market-data-hub's validate_config.py along with the dalio/
dalio_v2 settings.yaml blocks themselves when the Dalio analytical layer
was extracted into this repo. Returns a non-zero exit code on any error, so
it can gate CI / a pre-run check.

    python validate_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

CFG = Path(__file__).parent / "lazyray" / "config"

# Dalio thresholds that dalio.classify_cycle_phase compares against directly.
# A missing key would become None and raise TypeError at runtime, so require them.
_REQUIRED_DALIO_THRESHOLDS = [
    "credit_gap_bubble", "credit_gap_late", "rate_near_zero", "weak_growth",
    "dsr_high", "dsr_peak_pct", "debt_high_level", "debt_crisis_level",
    "deficit_large", "debt_trend_high", "debt_trend_moderate",
]


def _y(name: str) -> dict:
    return yaml.safe_load((CFG / name).read_text(encoding="utf-8")) or {}


def validate() -> list[str]:
    """Return a list of human-readable errors (empty list == config is valid)."""
    errors: list[str] = []

    settings = _y("settings.yaml")

    # 1. Dalio thresholds referenced by the cycle classifier must all be present.
    dalio = settings.get("dalio", {})
    for key in _REQUIRED_DALIO_THRESHOLDS:
        if dalio.get(key) is None:
            errors.append(f"settings.yaml: dalio.{key} is missing")

    # 2. pillar_weights must cover every pillar dalio.py's z-score groups by
    # (kept as a fixed list here rather than cross-checked against the hub's
    # macro_panel.yaml, since that file lives in a different repo now).
    weights = set((dalio.get("pillar_weights") or {}).keys())
    expected_pillars = {"growth", "debt_cycle", "liquidity", "external", "sovereign",
                        "banking", "governance", "geopolitical", "social", "markets"}
    missing_w = sorted(expected_pillars - weights)
    if missing_w:
        errors.append(f"settings.yaml: dalio.pillar_weights missing pillars: {missing_w}")

    # 3. dalio_v2: a typo'd weight key silently falls back to the code
    # default -- invisible today precisely because defaults equal the yaml
    # values, which is exactly when Phase-6 threshold tuning would edit yaml
    # and change nothing. Validate key sets and shapes.
    v2 = settings.get("dalio_v2", {})
    v2_components = {
        "sovereign_solvency": {"debt_gdp", "net_debt_gdp", "interest_revenue",
                               "interest_gdp", "primary_deficit_gdp", "r_minus_g",
                               "debt_trend_5y", "debt_p_up_5y"},
        "political_execution": {"government_effectiveness", "rule_of_law",
                                "control_corruption", "political_stability",
                                "regulatory_quality"},
        "private_credit": {"credit_gap", "private_dsr", "real_credit_growth",
                           "real_house_price_gap", "npl_ratio"},
        "external_constraint": {"current_account_deficit_gdp", "net_external_liability_gdp",
                                "short_term_debt_reserves", "debt_service_exports",
                                "fx_debt_share", "inflation", "fx_overvaluation_pct",
                                "reserves_months"},
        # WP2/C (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.7): split into
        # a market branch (yield_change_12m_pp/term_spread_pp/
        # reer_change_12m_pct) and an external branch (short_term_debt_
        # reserves/debt_service_exports) -- see funding_liquidity.py.
        "funding_liquidity": {"short_term_debt_reserves", "yield_change_12m_pp",
                              "term_spread_pp", "reer_change_12m_pct", "debt_service_exports"},
    }
    for engine, expected in v2_components.items():
        cfg = v2.get(engine) or {}
        w = cfg.get("weights") or {}
        unknown = sorted(set(w) - expected)
        if unknown:
            errors.append(f"settings.yaml: dalio_v2.{engine}.weights has unknown "
                          f"component keys (typo?): {unknown}")
        bad = sorted(k for k, v in w.items()
                     if not isinstance(v, (int, float)) or v <= 0)
        if bad:
            errors.append(f"settings.yaml: dalio_v2.{engine}.weights must be "
                          f"positive numbers: {bad}")
        labels = cfg.get("bucket_labels") or []
        cuts = cfg.get("bucket_thresholds") or []
        if labels and cuts and len(labels) != len(cuts) + 1:
            errors.append(f"settings.yaml: dalio_v2.{engine}: expected "
                          f"len(bucket_labels) == len(bucket_thresholds)+1, "
                          f"got {len(labels)} vs {len(cuts)}")
        for name, t in (cfg.get("thresholds") or {}).items():
            vals = t if isinstance(t, list) else []
            monotonic = len(vals) == 3 and (
                all(a < b for a, b in zip(vals, vals[1:]))
                or all(a > b for a, b in zip(vals, vals[1:])))
            if not monotonic:
                errors.append(f"settings.yaml: dalio_v2.{engine}.thresholds.{name} "
                              f"must be 3 strictly monotonic values, got {t}")

    # 4. dalio_v2.cycle_classifier (Fase 5): every label it references must
    # actually be a member of the corresponding engine's configured
    # bucket_labels -- same typo/silent-fallback risk as the weight keys
    # above, but for label SETS instead of numeric weights.
    v2_bucket_label_defaults = {
        "sovereign_solvency": ["low", "moderate", "elevated", "high", "critical"],
        "funding_liquidity": ["easy", "normal", "watch", "stress", "severe"],
        "private_credit": ["low", "moderate", "elevated", "high", "bubble"],
        "external_constraint": ["low", "moderate", "elevated", "high", "severe"],
    }
    cc = v2.get("cycle_classifier") or {}
    stage_labels = cc.get("stage_labels") or {}
    label_refs = {
        "stage_labels.crisis_funding_labels": ("funding_liquidity", stage_labels.get("crisis_funding_labels")),
        "stage_labels.crisis_external_labels": ("external_constraint", stage_labels.get("crisis_external_labels")),
        "stage_labels.late_long_debt_cycle_sovereign_labels":
            ("sovereign_solvency", stage_labels.get("late_long_debt_cycle_sovereign_labels")),
        "stage_labels.late_long_debt_cycle_funding_labels":
            ("funding_liquidity", stage_labels.get("late_long_debt_cycle_funding_labels")),
        "stage_labels.private_bubble_labels": ("private_credit", stage_labels.get("private_bubble_labels")),
        "stage_labels.late_leveraging_labels": ("private_credit", stage_labels.get("late_leveraging_labels")),
        "beautiful_funding_labels": ("funding_liquidity", cc.get("beautiful_funding_labels")),
    }
    for key, (engine, refs) in label_refs.items():
        if not refs:
            continue
        allowed = set((v2.get(engine) or {}).get("bucket_labels")
                     or v2_bucket_label_defaults.get(engine, []))
        unknown = sorted(set(refs) - allowed)
        if unknown:
            errors.append(f"settings.yaml: dalio_v2.cycle_classifier.{key} references "
                          f"labels not in dalio_v2.{engine}.bucket_labels (typo?): {unknown}")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        print(f"CONFIG INVALID — {len(errors)} error(s):")
        for e in errors:
            print("  -", e)
        return 1
    print("CONFIG OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
config_loader.py — (cached) loading of LazyRay's own settings.yaml.

Static reference data (countries.yaml, macro_panel.yaml, ...) stays in
market-data-hub and is imported directly from there
(``market_data_hub.config_loader.get_countries``) rather than duplicated
here — this module only owns the dalio/dalio_v2 tuning knobs and LazyRay's
own db_path.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

_CONFIG_DIR = Path(__file__).parent / "config"


def _load_yaml(name: str) -> Dict[str, Any]:
    path = _CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def get_settings() -> Dict[str, Any]:
    return _load_yaml("settings.yaml")

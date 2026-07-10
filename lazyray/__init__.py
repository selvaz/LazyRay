# -*- coding: utf-8 -*-
"""
lazyray — Ray Dalio-style analytical layer (debt-cycle / growth-inflation
regime classifier + 5-engine country risk architecture) for market-data-hub.

Input is read from market-data-hub's public API (market_data_hub.reader /
market_data_hub.config_loader); output is written to LazyRay's own DuckDB
file, fully decoupled from the hub's storage.
"""

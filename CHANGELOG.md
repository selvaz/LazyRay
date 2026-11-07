# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed
- Sovereign rates use hub resolved concepts in the stress monitor and Dalio panels. Resolved history is not point-in-time; observation-date history is used. Stress output exposes current leg status and mismatches.
- Updated the market-data-hub pin from v0.3.22 to v0.3.26.

## [0.2.10] — 2026-11-04

### Changed
- market-data-hub pin moved from v0.3.17 to v0.3.22 (release train ecosystem-2026.11.2).

## [0.2.9] — 2026-11-02

### Changed
- market-data-hub pin v0.3.11 → v0.3.17 (2026-11-02 release set).

## [0.2.8] — 2026-10-30

### Changed
- Sibling pins moved to the 2026-10-30 release set: market-data-hub v0.3.11, LazyTools v0.10.5.

## [0.2.7] — 2026-10-25

Release set `ecosystem-2026.10.9`.

### Changed
- `market-data-hub` pinned at `v0.3.9` (was `v0.3.4`), inheriting its `duckdb>=1.0,<2` bound so fresh installs avoid DuckDB 2.0.0's `ResourceInUseException`.

## [0.2.6] — 2026-10-20

Release set `ecosystem-2026.10.7`: sibling pins moved (market-data-hub v0.3.4 and the coupled refs).

## [0.2.5] — 2026-10-19

Release set `ecosystem-2026.10.6`. Only sibling pins move; no code changes.

### Changed
- `market-data-hub` pinned at `v0.3.3` (was `v0.3.2`); the `telegram` extra's `lazytoolkit` at `v0.10.4` (was `v0.10.3`).

## [0.2.4] — 2026-10-18

Release set `ecosystem-2026.10.5`. Only sibling pins move; no code changes.

### Changed
- `market-data-hub` pinned at `v0.3.2` (was `v0.3.1`). The `telegram` extra's `lazytoolkit` stays at `v0.10.3`.

## [0.2.3] — 2026-10-18

Release set `ecosystem-2026.10.4`. Only sibling pins move; no code changes.

### Changed
- `market-data-hub` pinned at `v0.3.1` (was `v0.3.0`). The `telegram` extra's `lazytoolkit` stays at `v0.10.3`.

## [0.2.2] — 2026-10-18

Release set `ecosystem-2026.10.3`. Only sibling pins move; no code changes.

### Changed
- `market-data-hub` pinned at `v0.3.0` (was `v0.2.1`) and the `telegram` extra's `lazytoolkit` at `v0.10.3` (was `v0.10.2`); the two pins move together.

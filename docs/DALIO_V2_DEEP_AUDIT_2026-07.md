# Deep audit — dalio_v2, unified report, regime & sources (2026-07)

Scope: everything in `8bdbd36..609e4f0` (~7,200 lines) — the `dalio_v2`
package, the unified `make_dalio_report.py` dashboard, `run_dalio_v2.py`,
config/schema/tests/docs, plus the `regime/` module and the new ECB /
IMF-SDMX sources. Five independent review passes (scoring math, data/DB
layer, report layer, config/tests/docs consistency, regime/sources), every
finding below verified against the actual code — several with live
DuckDB / pandas / node repros. Findings that multiple independent passes
converged on are marked **[×2]/[×3]**.

Overall verdict: the architecture is sound — coverage-tier discipline,
`suppress_insufficient`, `INSERT OR REPLACE` idempotency, `prev_label`
strict `<`, yaml↔code default equality, indicator-id reachability (all 27
referenced ids resolve), and the JS/Python color-threshold consistency all
**checked out clean**. The defects are concentrated in (a) a handful of
genuine correctness bugs, (b) three mis-measured components whose numbers
don't mean what their names claim, and (c) report/plumbing edge cases.

---

## P0 — Correctness bugs (fix first; small, surgical)

| # | Where | Defect |
|---|-------|--------|
| P0.1 **[×2]** | `external_constraint.py:153` | **Look-ahead**: `fx_overvaluation_pct` computed from `cdf_full` (unfiltered) — with `--ref-year 2023` the REER trend uses data through today. Every other component correctly uses `cdf` (`date <= ref_ts`). Fix: use `cdf`. |
| P0.2 | `make_dalio_report.py:139` | `WHERE ref_date = (SELECT max(ref_date)…)` is **global, not per-engine** — a partial rerun in a new year makes the other engines vanish from the whole dashboard (repro'd). Fix: latest ref_date per engine. |
| P0.3 | `make_dalio_report.py:154` | Blanket `except Exception: pass` swallows real errors mid-loop, leaving `v2_by` **partially populated** with `has_v2=True` (repro'd via NULL `n_components` → `TypeError`). Fix: catch only missing-table; guard NULL ints. |
| P0.4 **[×2]** | `runner.py` / `run_dalio_v2.py`; also `import_investor_base.py:122` | Writers run **without `db_write_lock()`** — concurrent with the scheduled EOD run, one side crashes on DuckDB's single-writer file lock. Fix: wrap writes in the lock like every other entry point. |
| P0.5 | `runner.py:69-73` | No `DELETE` of prior `(ref_date, engine)` rows → **stale rows survive** when a country drops out of coverage; and no explicit transaction (DuckDB autocommits per statement; `con.commit()` is a no-op) → a failure at engine 3/5 leaves mixed-vintage state. Fix: `BEGIN; DELETE …; INSERT …; COMMIT` per engine. |
| P0.6 | `scoring.py:169-176` | `prev_label()` returns the label of the most recent row **even when NULL** → hysteresis silently resets after any insufficient-coverage period. Fix: `AND label IS NOT NULL`. |
| P0.7 | `scoring.py:44-65` + `tests/test_dalio_v2.py:36` | `score_threshold` silently degenerates to a 0/100 cliff on thresholds mis-ordered after the orientation flip — and the test enshrines the broken orientation=-1 convention (asserts only the accidental extremes). Fix: validate `w < s < c` post-flip (raise), fix the test. |
| P0.8 | `scoring.py:74-84` | `weighted_average` counts weight-0/unknown-key components in `n_available` → a yaml weight-key typo silently inflates coverage; all-zero weights yields the contradictory `score=NULL, tier='full', confidence='high'`. Fix: skip weight-0 components in the count. |

## P1 — Mis-measured components (methodological; need care, not just a patch)

| # | Where | Defect |
|---|-------|--------|
| P1.1 **[×3]** | `private_credit.py:126` | `real_credit_growth` is the **nominal YoY change of the credit/GDP ratio** — neither real nor credit growth. A 10% real boom with matching GDP growth scores 0; a GDP collapse reads as a boom. Thresholds [5,8,12] are calibrated for real credit growth. Fix: `%Δratio + real_gdp_growth ≈ real credit growth` (both inputs available), or rename + recalibrate honestly. |
| P1.2 **[×2]** | `private_credit.py:122` vs `settings.yaml:169` | `private_dsr` is min–max **range position** (`_pct_in_range`) but named, documented and thresholded (75/90/95) as a **historical percentile**. One outlier year permanently rescales it. Fix: true percentile rank of latest vs own history. |
| P1.3 | all 5 engines | **No staleness guard**: `_latest()` returns a value at any age (a 2016 NPL scores as the 2026 condition, at full weight, counted toward `full` coverage), and `components_json` doesn't record the observation date, so it isn't auditable. Fix: per-component max-age (e.g. 3y for annual) + store `obs_date` in the audit trail. |
| P1.4 | `sovereign_solvency.py:94` | `debt_trend_5y` on a historical `ref_year` uses **realized actuals as if they were the vintage forecast** (the plan's own actuals-only companion slope — Fase 1 item — was never built). Also the name: the window is 9y (−3..+5), not 5. Fix: add the actuals-only slope + forecast-dependence flag per the plan; document. |
| P1.5 | `imf_sdmx.py:107-120` | Connector discards SDMX **UNIT_MULT/scale metadata**; if the IIP feed is in millions, `net_external_liability_gdp` ≈ 0 for every country and the component silently never fires. Couldn't verify live scale offline. Fix: one live-DB sanity query (USA NIIP/GDP should be ≈ −80%, not ≈ 0) + handle UNIT_MULT in the connector. |
| P1.6 | `funding_liquidity.py:71`, `private_credit.py:77` | "12m"/"YoY" changes take the closest observation at-or-before the target — on gappy series the window silently spans years, scored against 12-month-calibrated thresholds. Fix: max-gap tolerance, else None. |

## P2 — Report layer

| # | Where | Defect |
|---|-------|--------|
| P2.1 | `make_dalio_report.py:365,523,553` | `v2AvgRisk` averages the 5 engines into one number — **contradicting the report's own methodology text** ("never combined into a single number") and mixing coverage tiers (funding_liquidity is structurally proxy; insufficient engines drop out, so each country averages a different subset). Fix: drop it or label it explicitly as "mean of available engines (n/5)" with tier marker. |
| P2.2 **[×2]** | `dalio_v2/report.py:264-270` | "Worst bucket" KPI counts rows sharing the **label of the max-score row** — when nobody is in the terminal bucket it counts a milder label while claiming "worst". |
| P2.3 | `make_dalio_report.py:177-196` | Countries in `engine_scores` but absent from `regime_state` are **silently dropped** from the dashboard. |
| P2.4 | `political_execution.py:96`; `make_dalio_report.py:484-487` | Unrounded floats in components_json (WGI `92.45283018867924`) and raw `weight` rendering. Round at write; format at render. |
| P2.5 | `dalio_v2/report.py:199` | `f"{raw:g}"` on a non-numeric raw_value **crashes the whole report** (latent). Guard. |
| P2.6 | `dalio_v2/report.py` | No mobile CSS (the fix went only into make_dalio_report.py); `margin-left:218px` off-screen on phones; unconditional `&middot;`; `nan` KPI when all averages NaN. |
| P2.7 | `make_dalio_report.py:252/264` | `.v2note` font-size dead (equal specificity, `.muted` later wins). |
| P2.8 | `make_dalio_report.py:487,778` | Hardening: escape the `title="…"` tooltip attribute; `json.dumps(...).replace("</", "<\\/")` against `</script>` breakage. Low risk today (all strings code-controlled, verified), cheap to close. |

## P3 — Regime module & sources (outside dalio_v2)

| # | Where | Defect |
|---|-------|--------|
| P3.1 | `regime/estimate.py:117` | Retro window is `tail(30)` **rows**: a >6-week pause leaves permanent never-backfilled gaps; a BIC model flip (2→3 states) rewrites only the window, leaving older dates labeled under the old state indexation — mixed, incompatible "latest vintage" series. |
| P3.2 | `dalio.py:260` | Orientation-0 indicators (bond_yield_10y, implied_interest_rate, fx_debt_share…) **coerced to +1** in the cross-country z → highest-yield country written as a POS strength signal in `dalio_signals` (composite unaffected only because `markets` weight is 0). |
| P3.3 | `dalio.py:41,189` | `nom_rate` now prefers `implied_interest_rate` (stock rate) over the policy rate **everywhere**, not just r-vs-g — PUSHING_ON_STRING (`rate_near_zero`) can effectively never trigger for the 60/64 countries with the IMF series. |
| P3.4 | `regime/report.py:71` | Charts claim 5 years but show ~1 (daily fit, `points_per_year=52` default → `tail(260)`). Pass 252. |
| P3.5 | `import_investor_base.py` | No writer lock, no `record_vintage` → point-in-time guarantee broken for `nonresident_debt_share`. |
| P3.6 | lows | Error rerun wipes same-day success (`estimate.py:185`); empty universe `KeyError` (`run_regime_daily.py:72`); ECB sub-monthly period mis-binning (latent); imf_sdmx hardcoded agency/COUNTRY dim; fetch failures indistinguishable from no-data (no logging). |

## P4 — Config / docs / tests hygiene

- `validate_config.py` doesn't validate the `dalio_v2` block — a typo'd yaml key silently falls back to code defaults (today identical, so invisible; the trap fires exactly when Phase-6 tuning edits yaml).
- Plan doc ✅ boxes overstate: Fase 0 (countries.yaml flags, `dalio_cycle_v2` table) never built — reserve-currency set is hardcoded in `external_constraint.py:84` against settings.yaml's own "never hardcoded" promise; method deviations under ✅ (OLS vs moving-average REER trend, npl absolute vs percentile, fx-depreciation component replaced) not noted in the plan text.
- Dead: `real_house_price_gap` threshold in yaml (component hardcoded None — and note `n_expected=5` puts 'full' exactly on the 4/5=0.80 float boundary); `robust_z` in scoring.py (design decision #5, used by nothing).
- Stale docs: `run_dalio_v2.py:13`/`runner.py:11` "both Phase-1 engines" (default is all 5); `ARCHITECTURE.md:156` still says IDS; `settings.yaml:118` hysteresis comment (2pt) vs code behavior (4pt).
- Test gaps: hysteresis end-to-end through the DB (`prev_label` never returns non-None in any test); re-run idempotency; euro-member reserve branch; `dalio_v2/report.py` entirely untested; embedded-JS syntax unchecked (node-verified manually this audit).

---

## Remediation status (2026-07-09, same branch)

All five phases below were implemented and committed on this branch the
same day (commits "Fase A" … "Fase E"). Every P0/P1/P2/P3 item is fixed,
with regression tests, EXCEPT:

- **P1.5 (NIIP scale)**: the code-side fix is in (imf_sdmx now applies
  UNIT_MULT), but the one-query sanity check against the live
  `market_data.duckdb` (USA `net_external_liability_gdp` should read
  ≈ +80, not ≈ 0) still needs to run on the machine that has the real DB.
  If existing `iip_net_position` rows were fetched pre-fix and the feed
  carries a non-zero multiplier, re-fetch that indicator.
- **P4 Fase-0 backlog**: the reserve-currency/commodity/financial-center
  flags in countries.yaml and the `dalio_cycle_v2` table remain unbuilt
  (documented in the plan doc header note); the hardcoded reserve-currency
  set stays for now.

## Remediation plan (branch `claude/datahub-market-order-check-7gmh1z`)

**Fase A — P0 hotfixes** (one commit-series; small diffs, each with a
regression test): P0.1–P0.8. Highest value/effort ratio; nothing here
changes methodology, only makes the code do what it already claims.

**Fase B — measurement fixes**: P1.1 (real credit growth via
`%Δratio + real_gdp_growth`), P1.2 (true percentile), P1.3 (staleness
guard + `obs_date` in components_json — touches all 5 engines and the
audit-trail schema, so one coherent commit), P1.6 (max-gap tolerance).
P1.4 (actuals-only slope + flag) per the original plan. P1.5 needs a
one-query check against the live DB first — flagged for the next run on
the machine that has `market_data.duckdb`.

**Fase C — report**: P2.1–P2.8. Mostly independent small fixes; P2.1 is
a product decision (recommend: keep the column but label "media motori
disponibili (n/5)" and show the count).

**Fase D — regime & sources**: P3.1 (backfill gap + full-rewrite on model
flip), P3.2/P3.3 (orientation-0 exclusion; policy-rate vs implied-rate
split), P3.4, P3.5, then the P3.6 lows.

**Fase E — hygiene**: dalio_v2 block in validate_config.py, dead
config/code removal, doc corrections (plan ✅ statuses, ARCHITECTURE,
docstrings, yaml comments), and the missing tests (hysteresis-via-DB,
idempotency, report smoke test, JS syntax check in the test suite).

Sequencing rationale: A before B because B's diffs sit on top of the same
lines; C independent (can parallel A/B); D independent; E last since it
documents the final state.

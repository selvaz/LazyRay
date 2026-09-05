-- ============================================================================
-- lazyray — DuckDB schema (own storage, decoupled from market-data-hub)
-- 5 tables. Idempotent (CREATE IF NOT EXISTS). Input data (macro_panel /
-- v_macro_panel_ext) is read from market-data-hub's public reader API, not
-- from this file's tables.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. schema_meta — schema version + bookkeeping (one row per key)
--    Populated by connection.apply_schema(): schema_version, schema_applied_at.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- ----------------------------------------------------------------------------
-- 1. "Ray Dalio" analytical layer v1 (computed by dalio.py on top of the
--    hub's macro_panel, read via reader.read_macro_panel_ext)
--    - dalio_signals : z-score (x direction) per (country, indicator)
--    - pillar_scores : score per pillar + composite + 3 categorical labels
--    - regime_state  : four-box growth/inflation + debt-cycle phase + deleveraging
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dalio_signals (
    country_iso3  VARCHAR NOT NULL,
    ref_date      DATE    NOT NULL,
    indicator_id  VARCHAR NOT NULL,
    pillar        VARCHAR,
    value         DOUBLE,                 -- last value
    z_score       DOUBLE,                 -- (x-mean)/std over 10y window, already x direction
    z_window_n    INTEGER,                -- n observations used
    signal        VARCHAR,                -- POS / NEG / NEUTRAL
    computed_at   TIMESTAMP,
    PRIMARY KEY (country_iso3, ref_date, indicator_id)
);

CREATE TABLE IF NOT EXISTS pillar_scores (
    country_iso3     VARCHAR NOT NULL,
    ref_date         DATE    NOT NULL,
    pillar           VARCHAR NOT NULL,    -- 'COMPOSITE' for the aggregate row
    score            DOUBLE,              -- mean z x direction of the pillar
    n_indicators     INTEGER,
    debt_cycle_phase VARCHAR,             -- only on the COMPOSITE row
    short_cycle_pos  VARCHAR,
    gi_regime        VARCHAR,             -- Q1..Q4
    computed_at      TIMESTAMP,
    PRIMARY KEY (country_iso3, ref_date, pillar)
);

CREATE TABLE IF NOT EXISTS regime_state (
    country_iso3         VARCHAR NOT NULL,
    ref_date             DATE    NOT NULL,
    growth_delta         DOUBLE,          -- growth vs trend/expected
    infl_delta           DOUBLE,          -- inflation vs trend/expected
    quadrant             VARCHAR,         -- Q1/Q2/Q3/Q4
    debt_cycle_phase     VARCHAR,         -- from classify_debt_cycle_phase()
    nom_growth           DOUBLE,
    nom_rate             DOUBLE,
    deleveraging_quality VARCHAR,         -- BEAUTIFUL / UGLY / NA
    credit_gap           DOUBLE,
    dsr                  DOUBLE,
    debt_income_gap      DOUBLE,
    debt_trend           DOUBLE,          -- debt/GDP trajectory (pp/year, incl. forecast)
    computed_at          TIMESTAMP,
    PRIMARY KEY (country_iso3, ref_date)
);

-- ----------------------------------------------------------------------------
-- 2. engine_scores — Dalio v2, 5-engine architecture (additive: does NOT
--    replace dalio_signals/pillar_scores/regime_state above). One row per
--    (country, ref_date, engine). See
--    docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md for the full design.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engine_scores (
    country_iso3    VARCHAR NOT NULL,
    ref_date        DATE    NOT NULL,
    engine          VARCHAR NOT NULL,   -- 'sovereign_solvency' | 'funding_liquidity' |
                                        -- 'private_credit' | 'external_constraint' |
                                        -- 'political_execution'
    score           DOUBLE,             -- 0-100, higher = worse (risk)
    label           VARCHAR,            -- engine-specific categorical bucket
    coverage_tier   VARCHAR,            -- 'full' | 'proxy' | 'insufficient'
    confidence      VARCHAR,            -- 'high' | 'medium' | 'low'
    n_components    INTEGER,            -- available inputs used
    n_expected       INTEGER,            -- total inputs the engine looks for
    components_json VARCHAR,            -- per-component audit trail (see plan doc)
    computed_at     TIMESTAMP,
    PRIMARY KEY (country_iso3, ref_date, engine)
);

-- WP2 (docs/DALIO_PROD_ASSESSMENT_2026-09.md Sec.3.1.5): comparability scale
-- alongside the absolute `score` above -- mean of the engine's available
-- per-component peer-group percentiles, plus its bucket label under the same
-- cut points as `label`. ADD COLUMN IF NOT EXISTS: idempotent against an
-- existing engine_scores table (DuckDB supports the IF NOT EXISTS form).
ALTER TABLE engine_scores ADD COLUMN IF NOT EXISTS relative_score DOUBLE;
ALTER TABLE engine_scores ADD COLUMN IF NOT EXISTS relative_label VARCHAR;

-- ----------------------------------------------------------------------------
-- 3. dalio_cycle_v2 — Fase 5 cycle classifier: sits on top of engine_scores,
--    never collapses the 5 engines into one number before applying rules
--    (rules test each engine's own hysteresis-stable LABEL, not raw score).
--    One row per (country, ref_date). See
--    docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md Fase 5 and
--    lazyray/dalio_v2/cycle_classifier.py for the full design.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dalio_cycle_v2 (
    country_iso3          VARCHAR NOT NULL,
    ref_date               DATE    NOT NULL,
    dalio_stage             VARCHAR,   -- early_or_mid_cycle | late_long_debt_cycle | private_bubble |
                                        -- late_leveraging | contraction | crisis |
                                        -- unclassified_no_funding_data (funding_liquidity branch=none,
                                        -- WP2 Sec.3.1.7 -- never a bare NULL for that specific reason) |
                                        -- NULL (unclassifiable for any other gate-coverage reason)
    deleveraging_type       VARCHAR,   -- none | beautiful | inflationary | repressive | restructuring |
                                        -- ugly | NULL (unclassifiable). 'restructuring' is currently
                                        -- unreachable -- no restructuring-event data source exists, folded
                                        -- into 'ugly' by design, see cycle_classifier.py module docstring.
    overall_confidence      VARCHAR,   -- worst confidence_for(tier) across the engines used
    top_risk_drivers_json   VARCHAR,   -- top-3 worst-scoring components across all 5 engines
    caveats_json            VARCHAR,   -- active caveats (proxy usage, folded categories, etc.)
    audit_json               VARCHAR,  -- model_version, engines_used{engine:tier}, unclassifiable_reason
    computed_at               TIMESTAMP,
    PRIMARY KEY (country_iso3, ref_date)
);

-- ----------------------------------------------------------------------------
-- 4. run_meta — one row per Dalio v2 run (ref_date), for --if-changed change
--    detection: a daily-scheduled run whose input panel hash matches the
--    most recent row here can skip computing/writing anything at all. See
--    lazyray/dalio_v2/runner.py::run_dalio_v2(if_changed=...) and
--    docs/DALIO_PROD_ASSESSMENT_2026-09.md §3.1.1.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_meta (
    ref_date    DATE PRIMARY KEY,
    input_hash  VARCHAR,     -- SHA-256 of the sorted (date,country,indicator,value) rows used
    model_version VARCHAR,   -- git_short_sha() at run time
    engines     VARCHAR,     -- comma-joined, sorted engine names this run computed
    n_rows      INTEGER,     -- rows in the input panel used for the hash
    computed_at TIMESTAMP
);

-- Note: country_classification (written by classify.py) is not declared
-- here -- classify.py DROPs and re-CREATEs it wholesale on every run (see
-- lazyray/classify.py), exactly as it did in market-data-hub before the move.

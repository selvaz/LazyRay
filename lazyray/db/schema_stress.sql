CREATE TABLE IF NOT EXISTS stress_index (
    date DATE, region VARCHAR, index_value DOUBLE, p_stress DOUBLE,
    regime VARCHAR, regime_hmm VARCHAR, n_segments INTEGER, computed_at TIMESTAMP,
    PRIMARY KEY(date, region)
);
CREATE TABLE IF NOT EXISTS stress_components (
    date DATE, region VARCHAR, segment VARCHAR, indicator VARCHAR,
    raw_value DOUBLE, pct DOUBLE, computed_at TIMESTAMP,
    PRIMARY KEY(date, region, segment, indicator)
);
ALTER TABLE stress_components ADD COLUMN IF NOT EXISTS computed_at TIMESTAMP;
ALTER TABLE stress_index ADD COLUMN IF NOT EXISTS regime_hmm VARCHAR;

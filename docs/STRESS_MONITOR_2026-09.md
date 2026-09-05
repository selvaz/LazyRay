# Daily funding conditions and systemic stress monitor

This is LazyRay's daily market-based monitor of funding conditions and systemic stress, not a daily recomputation of annual country-risk data. It follows the ECB CISS construction: indicators are converted to their own expanding empirical CDF, averaged by segment, then combined using a time-varying correlation matrix.

## Inputs and signs

Higher transformed values mean more stress. US credit is HY OAS (`BAMLH0A0HYM2`), IG OAS (`BAMLC0A0CM`) and CCC OAS (`BAMLH0A3HYC`); rates is 10Y real yield (`DFII10`), absolute 60-business-day 10Y yield change (`DGS10`) and negative 10Y-2Y slope (`T10Y2Y`); volatility is VIX (`VIXCLS`); and conditions are NFCI and STLFSI4. Liquidity is one declared composite: negative 20-business-day change of `WALCL - WTREGEN - RRPONTSYD`, each leg first scaled to a common unit — FRED publishes the first two in millions of dollars and RRP in billions, so the legs are not comparable as reported. Falling net liquidity is stress. All are FRED from the hub.

EA rates is absolute 60-business-day change of ECB DFR (`ECBDFR`, 25%). Bond is absolute 60-business-day change of Germany's monthly 10Y yield (`IRLTLT01DEM156N`, 30%). Periphery is the positive BTP-Bund, Spain-Bund and France-Bund spreads from FRED monthly 10Y yields (45%).

Policy-rate levels, price-index levels, and RRP/TGA levels are deliberately excluded: their trends and cycle-driven drifts make an expanding percentile of a level persistently misleading. Changes and spreads retain the stress-relevant shock or relative-price signal.

EM hard/local segments use 20-day EMB/EMLC-minus-IEF return differentials (sign reversed so EM underperformance is stress) and 60-day EMB/EMLC drawdowns. The optional FX segment uses available UUP/FXE/FXY price drawdowns; absent ETFs are omitted and recorded by the run result.

Fixed weights are US: credit 25%, rates 20%, volatility 20%, conditions 20%, liquidity 15%; EA: rates 25%, bond 30%, periphery 45%; EM: hard currency 40%, local currency 35%, FX 25%. An unavailable segment carries no contribution rather than being reweighted.

## Method

For an indicator value `x_t`, `p_t = n^-1 sum(1[x_s <= x_t])` for `s <= t`, emitted only after 756 business days. A segment is the mean available indicator percentile. With segment vector `s`, fixed weights `a`, and EWMA correlation `C_t` (daily lambda .93), the monitor is `sqrt(w' C_t w)`, with `w = a*s`, clipped to [0,1].

| Source frequency | Staleness cap |
| --- | ---: |
| Daily | 10 calendar days |
| Weekly | 21 calendar days |
| Monthly | 100 calendar days |

The primary regime is causal and region-relative: after three years of index history, it enters `stress` at the expanding 85th percentile and leaves below the 75th; it enters `elevated` at the 60th and leaves below the 50th. Earlier observations are always `calm`. A local `hmmlearn` 0.3.3 three-state Gaussian HMM (two states if the three-state fit does not converge) is retained only as the auxiliary `p_stress` and `regime_hmm` field. Its top-mean state uses .60/.40 hysteresis. Because that posterior is smoothed over the full sample, it is relabelled at every refit and is not point-in-time.

## How to read it

The index level is a within-history percentile aggregate, and the regime is relative to that region's own past rather than an absolute crisis probability. EA is partial whenever its monthly yields are stale; EM is ETF-proxied. The digest prints the index percentile, HMM fields, and partial segment coverage so these qualifications are visible.

## Run

`C:\ProgramData\spyder-6\python.exe run_stress_monitor.py --region all --hub-db <hub.duckdb> --db <output.duckdb>`

`--dry-run` builds the same digest text as a real run using a disposable database, then prints `missing:`. `--notify` sends Telegram only when entering `stress`, or on an upward crossing of the region's own 90th percentile; `--force-notify` overrides the gate. One-segment days do not notify. Telegram uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Limits

This is not a validated crisis predictor. EA government-yield data are monthly, and EM coverage is ETF/FX-proxy based rather than country-level. CDFs are relative to each series' available history; missing hub series and frequency-specific staleness caps can reduce coverage.

# Note: framework Python per un layer di portfolio construction Dalio-like

> **Stato: DEFERRED — fuori scope per ora.** Salvato come materiale di
> riferimento per quando/se il layer di analisi paese a 5 motori
> ([DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md](DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md))
> alimenterà un modulo di allocazione/portfolio a valle. Fino a quel momento
> il lavoro attivo resta sulla **metodologia di analisi paese**, non su asset
> allocation — vedi
> [DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md](DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md)
> per il piano attivo che estrae dalla stessa fonte solo le parti applicabili
> all'analisi paese (point-in-time correctness, audit trail, validazione).
>
> Documento ricevuto da ChatGPT (upload utente, 2026-07-09), riassunto qui
> (non verbatim — l'originale è un PDF, questa è la trascrizione fornita
> dall'utente in chat) per riferimento futuro.

## Sintesi

Il documento originale analizza come costruire in Python un layer di
**portfolio construction risk-parity/All-Weather** ispirato a Dalio/Bridgewater
— non un classificatore di rischio paese, ma un allocatore di portafoglio
multi-asset guidato da regimi crescita/inflazione. Conclusione del documento:
nessun framework open source replica Bridgewater, ma uno stack composito può
avvicinarsi alla filosofia.

## Stack consigliato (per riferimento futuro, non attivato ora)

| Livello | Candidati | Note |
|---|---|---|
| Portfolio optimization / risk budgeting | **skfolio** (miglior fit complessivo, stile scikit-learn, walk-forward/CPCV/stress test nativi), **Riskfolio-Lib** (più profondo su risk budgeting/fattori/CVXPY), PyPortfolioOpt (prototipazione rapida, HRP) | BSD-3/MIT, tutti attivi nel 2026 |
| Backtest/execution | **bt** (semplice, ribilanciamenti periodici), **zipline-reloaded** (event-driven, commissioni/slippage/minute bars), **vectorbt** (ricerca massiva, sweep parametrici) | ⚠️ vectorbt: licenza "Apache 2.0 with Commons Clause" — vincolo per rivendita commerciale, da valutare con attenzione |
| Performance attribution / explainability | **pyfolio-reloaded** (tear sheets, capacity analysis), **empyrical-reloaded** (metriche standard, attribution), **alphalens-reloaded** (signal/factor research, IC) | Apache-2.0, layer diagnostico non costruttivo |
| Dati macro point-in-time | FRED + **ALFRED** (vintages), BLS, BEA, ECB Data Portal | Requisito dichiarato "decisivo": nessun backtest valido senza versionamento delle release |
| Dati di mercato | Databento (tick/intraday), Tiingo (EOD/intraday/fundamentals/FX), Nasdaq Data Link (tabellare) | Per execution research, non necessari finché non c'è un motore di esecuzione |

## Architettura a 5 blocchi proposta (portfolio-side)

```text
ingestion point-in-time → regime engine → portfolio construction →
backtest/execution → analytics + audit trail
```

## Perché è deferred

Il repo oggi (`market-data-hub`) non ha un modulo di portfolio construction
né un mandato a costruirne uno: il lavoro attivo è un **motore di
classificazione del rischio paese** (5 engine: Sovereign Solvency, Funding
Liquidity, Private Credit Cycle, External Currency Constraint, Political
Execution — vedi `DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md`), che produce
diagnosi per paese, non pesi di portafoglio. Introdurre skfolio/Riskfolio-Lib/
zipline ora sarebbe costruire uno strato senza un consumatore a valle.

**Quando questo materiale torna rilevante:** se in futuro si decide di
tradurre l'output dei 5 motori in un'allocazione tattica per paese/asset
class (es. "Dalio Cycle Stage = late long debt cycle" → tilt verso
inflation-linked/oro), questo documento è il punto di partenza per la scelta
dello stack. Fino ad allora, non installare né wire-are nessuna di queste
librerie.

## Un'idea trasferibile fin da subito (non-portfolio)

Il documento insiste su un punto che VALE anche per l'analisi paese e non
solo per il portfolio: **"i principi devono essere implementati in strumenti
e protocolli verificabili"** — cioè ogni decisione/classificazione deve
portare un audit trail (quali dati, quali soglie, quale versione del
modello). Questo principio è stato estratto e applicato al piano attivo in
[DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md](DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md)
— è l'unica parte di questo documento già in lavorazione.

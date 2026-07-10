# Point-in-time correctness e audit trail per il sistema Dalio a 5 motori (2026-07-09)

> **Come usare questo documento in un'altra sessione:** è un **addendum
> tecnico** a
> [DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md](DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md)
> (leggerlo prima — definisce le Fasi 0-6 e le tabelle `engine_scores` /
> `dalio_cycle_v2`). Questo documento estrae da un'analisi esterna dello
> stack Python "Dalio-like" (portfolio-focused, vedi
> [DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md](DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md),
> deferred) le **uniche due idee trasferibili all'analisi paese**:
> point-in-time correctness e audit trail/explainability. Definisce come e
> dove integrarle nel piano a 5 motori — principalmente dentro la Fase 6
> (validazione) del piano principale, con un impatto anche su Fase 0/1.
>
> **Scope esplicito: NIENTE portfolio construction qui.** Nessun
> skfolio/Riskfolio-Lib/zipline/vectorbt/risk-parity. Solo metodologia di
> analisi e classificazione paese.

---

## 1. Le due idee trasferibili (e perché contano)

Il documento sorgente (centrato su un allocatore di portafoglio risk-parity)
insiste su due principi che Bridgewater/Dalio dichiarano pubblicamente come
cardine del proprio processo, e che **non sono specifici al portfolio
construction** — si applicano a qualunque sistema che pretenda di
classificare "lo stato del mondo" in modo ripetibile:

1. **Point-in-time correctness.** Un backtest o una diagnosi storica che usa
   dati rivisti (non quelli disponibili all'epoca) produce risultati
   ingannevoli. Il documento lo chiama "il requisito decisivo".
2. **"I principi devono essere incorporati in strumenti e protocolli
   verificabili"** (citazione diretta da Principles/Dalio nel documento
   sorgente) — ogni classificazione deve portare con sé la traccia di quali
   dati, quali soglie, quale versione del modello l'hanno prodotta.

**Perché questo documento esiste:** la review metodologica già presente nel
repo (`DALIO_METHODOLOGY_REVIEW_2026-07.md`, §2.3 e §2.6) aveva già
identificato esattamente questi due problemi nel sistema attuale — look-ahead
da forecast WEO, e nessun backtest/nessuna audit trail. Il documento esterno
non porta un'idea nuova, ma **conferma indipendentemente la priorità** e, in
un caso, punta dritto a qualcosa che il repo ha già costruito ma non usa
(§2 sotto).

---

## 2. Scoperta chiave: l'infrastruttura vintage-aware esiste già, ma `dalio.py` non la usa

Verificato nel codice (2026-07-09), non teorico:

```text
market_data_hub/db/schema.sql:
  - macro_series_vintage (date, series_id, value, vintage_date, source)
  - macro_panel_vintage  (date, country_iso3, indicator_id, value, vintage_date, source)
    → popolate automaticamente da ogni upsert (market_data_hub/db/upsert.py:129-130,
      _VINTAGE_MAP), quindi ogni ingest storico ha già lasciato una traccia
      vintage-by-vintage, senza bisogno di costruire nulla di nuovo lato ingest.

market_data_hub/reader.py:
  - read_macro_panel(indicators, countries, asof="YYYY-MM-DD", ...)
    → legge da macro_panel_vintage il valore ESATTAMENTE come era noto a quella
      data, "avoiding revision look-ahead in backtests" (docstring originale).
  - stessa cosa per read_macro(..., asof=...) su macro_series_vintage.
```

**Questo è il punto centrale di tutto il documento:** il "requisito decisivo"
del framework Python esterno — vintage/point-in-time — **è già disponibile
come API pronta all'uso**. Il problema non è costruirlo, è che
**`market_data_hub/dalio.py` non lo chiama mai**: `run_dalio()` legge sempre
da `v_macro_panel_ext` (la vista "ultimo valore noto ora"), mai con `asof=`.
Di conseguenza:

- `classify_cycle_phase()` classifica "il 2026" usando dati WEO che oggi
  includono revisioni fatte dopo il 2026 (esattamente il difetto §2.3 della
  review metodologica, ora con la causa tecnica precisa: manca solo la
  chiamata `asof=`, l'infrastruttura c'è).
- Non esiste ancora nessun backtest storico del classificatore, perché senza
  `asof=` ogni tentativo di "rigiocare" una data storica userebbe dati con
  il senno di poi.

Questo cambia la priorità della Fase 6 del piano principale: **non è più
"costruire una validazione da zero", è "collegare `dalio.py`/i 5 motori
all'API `asof=` che già esiste"**. Il lavoro è più piccolo di quanto
sembrasse.

---

## 3. Piano dettagliato

### Fase A — Rendere i 5 motori vintage-aware (estende Fase 0/1 del piano principale)

**Obiettivo:** ogni funzione `compute()` dei motori (Fase 1-4 del piano
principale) deve poter essere invocata con una data storica e restituire
esattamente ciò che sarebbe stato calcolato quel giorno.

**Task:**
1. In `market_data_hub/dalio_v2/scoring.py` (definito in Fase 0 del piano
   principale), il punto di lettura dati non deve interrogare direttamente
   `v_macro_panel_ext` con SQL libero (come fa oggi `dalio.py:231-233`), ma
   passare da `reader.read_macro_panel(indicators=..., countries=...,
   asof=ref_date, db_path=...)`. Questo è un cambio di *fonte dei dati*, non
   di logica: ogni motore continua a ricevere lo stesso DataFrame long-format
   che già consuma oggi.
2. **Attenzione a un dettaglio non banale:** `v_macro_panel_ext` include
   colonne derivate (`implied_interest_rate`, `fx_debt_share`,
   `bond_yield_10y`) calcolate con `UNION ALL`/subquery **sopra**
   `macro_panel`, non sopra `macro_panel_vintage`. Queste derivate NON hanno
   oggi un equivalente vintage-aware. Prima di dichiarare un motore
   "vintage-aware", verificare quali dei suoi componenti passano da
   `v_macro_panel_ext` (non vintage-safe) vs da `read_macro_panel(asof=...)`
   (vintage-safe) — e marcare esplicitamente nel `components_json` (Fase B
   sotto) quali input erano storicamente corretti e quali no. Non fingere che
   tutto sia vintage-safe se non lo è: è lo stesso errore di disciplina già
   segnalato in Fase 4 del piano principale per Funding Liquidity
   (`coverage_tier`), applicato qui alla dimensione temporale invece che
   geografica.
3. `debt_trend` (lo slope che include le proiezioni WEO, `_slope()` in
   `dalio.py:94-106`) resta l'unico input strutturalmente non "point-in-time
   puro" per definizione — include forecast che *erano* i forecast noti a
   quella data se letti con `asof=`, quindi sono comunque legittimi (un
   backtest deve vedere le proiezioni CHE ESISTEVANO allora, non quelle di
   oggi). `read_macro_panel(asof=...)` gestisce già correttamente anche
   questo caso, perché la vintage table registra ogni valore (storico o
   forecast) così come pubblicato in quella data.

**Definition of done:** una chiamata tipo
`sovereign_solvency.compute(con, ref_date=date(2010,1,1), asof=date(2010,1,1))`
produce uno score usando solo dati che un analista avrebbe potuto vedere il
1° gennaio 2010 — verificabile confrontando manualmente 2-3 valori con
`read_macro_panel(asof="2010-01-01")` letto a mano.

### Fase B — Audit trail / decision log (estende Fase 0 del piano principale)

**Obiettivo:** ogni riga di `engine_scores` (tabella già definita in Fase 0
del piano principale) deve essere ricostruibile: quali indicatori, quali
valori, quali soglie, quale versione del codice.

Il campo `components_json` era già previsto nel piano principale ma senza
schema fisso. Fissarlo così (ispirato al pattern `decision_audit` del
documento sorgente, adattato da "decisione di portafoglio" a "score paese"):

```json
{
  "model_version": "<git short-sha al momento del run>",
  "ref_date": "2026-07-09",
  "asof": "2026-07-09",
  "components": {
    "debt_gdp": {"value": 121.3, "source_vintage": "2026-07-09", "score_contrib": 62.1, "weight": 0.1428},
    "r_minus_g": {"value": 1.8, "source_vintage": "2026-07-09", "score_contrib": 45.0, "weight": 0.1428},
    "...": "..."
  },
  "missing_components": ["net_debt_gdp"],
  "coverage_tier": "full",
  "vintage_safe": true
}
```

`model_version` = `git rev-parse --short HEAD` catturato a runtime (nessuna
nuova infrastruttura: è un `subprocess.run` di una riga in
`dalio_v2/scoring.py`, scritto una volta e riusato da tutti i motori).
`vintage_safe` = `false` se anche un solo componente è passato da
`v_macro_panel_ext` invece che da `read_macro_panel(asof=...)` (vedi Fase A,
punto 2) — questo è il flag che rende visibile il limite, invece di
nasconderlo dentro un JSON che sembra completo.

**Definition of done:** per qualunque riga di `engine_scores`, è possibile
rispondere senza guardare il codice a "perché questo paese ha questo score
oggi" — solo leggendo `components_json`.

### Fase C — Regime engine: sorpresa vs attese, non output-gap

La review metodologica esistente (§2.1) e il documento esterno concordano
indipendentemente sullo stesso punto: il four-box growth/inflation di
Bridgewater è definito su **sorprese rispetto al consenso/alle attese**, non
su deviazioni da un trend potenziale stimato ex-post. `dalio.py` oggi usa
`growth_delta = crescita_corrente − potenziale WEO [ry+2, ry+5]` (output
gap, look-ahead) e `infl_delta = inflazione_corrente − media 3 anni prima`
(direzione, non sorpresa).

**Non è nello scope di questo addendum riscrivere il regime engine** (è già
coperto come raccomandazione P1.7 nella review metodologica e non è uno dei
5 motori del piano principale — il four-box resta un layer separato in
`dalio.py`). Questo documento aggiunge solo un vincolo tecnico per quando
verrà rifatto: se si introduce una vera "sorpresa vs attese", la fonte più
onesta con i dati già in casa è:

```text
growth_surprise(ref_date) = actual(ref_date) − forecast_pubblicato_UN_ANNO_PRIMA(ref_date)
```

cioè confrontare il valore attuale con **la previsione che il WEO stesso
faceva un anno prima per quello stesso anno** — non con un "potenziale"
implicito. Questo è calcolabile SOLO con `macro_panel_vintage` (serve la
previsione come pubblicata un anno fa, non quella di oggi rivista) — altra
conferma che Fase A è un prerequisito, non un'opzione, per fare questo bene
in futuro.

### Fase D — Validation harness: vintage replay test (sostituisce/precisa Fase 6 del piano principale)

Il documento esterno propone (in contesto portfolio) walk-forward test e
combinatorial purged CV. Questi sono concetti di *validazione out-of-sample
di un modello predittivo/allocativo* — non si applicano 1:1 a un
classificatore descrittivo di stato-del-mondo come i 5 motori, ma il
principio sotto (non testare su dati che il modello non avrebbe potuto
vedere) si applica in pieno, ed è più semplice da implementare qui grazie a
Fase A. Concretamente, per ciascuno dei 10 episodi storici già elencati in
Fase 6 del piano principale (§20.1 della proposta ChatGPT: Giappone,
Asia 1997, Argentina, USA 2008, Eurozona 2010-12, Grecia, Turchia, Sri Lanka,
UK gilt 2022, USA 2020-2026):

```text
per ogni episodio (paese, data_crisi):
  per ogni ref_date in [data_crisi - 24 mesi ... data_crisi], passo mensile/trimestrale:
    score = engine.compute(con, ref_date, asof=ref_date)   # <- SEMPRE asof=ref_date, mai "oggi"
    registra (ref_date, score, label, mesi_a_crisi = data_crisi - ref_date)
  verifica: il motore rilevante (Sovereign Solvency per Grecia/Argentina,
  Funding Liquidity per UK gilt 2022, External Constraint per Sri Lanka/Asia 1997)
  ha raggiunto 'stressed'/'critical' con ALMENO 12 mesi di anticipo?
  registra: vero/falso positivo, vero/falso negativo, esplicitamente (non solo "successo")
```

Questo È il "vintage replay test" del documento esterno, tradotto da
contesto-portfolio a contesto-classificatore: non misura un rendimento, ma
precision/recall del classificatore sugli episodi noti, con dati
temporalmente onesti grazie a `asof=`.

**Definition of done:** una tabella con 10 righe (una per episodio), colonne
`mesi_di_anticipo` (o "mancato") e `motore_che_ha_segnalato_per_primo`,
prodotta rieseguendo i motori con `asof=` storico — non un'affermazione
qualitativa senza numeri.

---

## 4. Cosa resta esplicitamente fuori scope

Tutto ciò che nel documento sorgente riguarda **costruzione di portafoglio**:
risk parity/ERC/HRP, skfolio/Riskfolio-Lib/PyPortfolioOpt, backtest di
allocazione (bt/zipline-reloaded/vectorbt), performance attribution di
portafoglio (pyfolio/empyrical/alphalens), feed di mercato ad alta
granularità (Databento/Tiingo/Nasdaq Data Link). Vedi
[DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md](DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md)
per i dettagli salvati come riferimento futuro. Nessuna di queste librerie va
installata o wired ora: il sistema a 5 motori produce diagnosi paese, non
pesi di portafoglio, e non c'è oggi un consumatore a valle che le
richiederebbe.

---

## 5. Relazione con il piano principale — riepilogo modifiche

| Fase del piano principale | Modifica introdotta da questo addendum |
|---|---|
| Fase 0 (fondamenta) | `engine_scores.components_json` ora ha uno schema fisso (§2 sopra), non libero |
| Fase 1-4 (motori) | Ogni `compute()` deve leggere via `reader.read_macro_panel(asof=...)`, non `v_macro_panel_ext` direttamente, quando invocato in modalità storica (Fase A) |
| Fase 6 (validazione) | Il backtest storico (§20 proposta ChatGPT) si implementa concretamente come "vintage replay test" (Fase D sopra), riusando l'API `asof=` già esistente invece di costruire nuova infrastruttura di versioning dati |
| §6 "Cosa NON fare" | Aggiungi: non introdurre librerie di portfolio construction (skfolio, Riskfolio-Lib, bt, zipline-reloaded, vectorbt, pyfolio/empyrical/alphalens) — deferred, vedi `DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md` |

# Piano di implementazione — Dalio 5-engine architecture (2026-07-09)

> **AGGIORNAMENTO 2026-07-09 (post-audit):** un deep audit indipendente
> ([DALIO_V2_DEEP_AUDIT_2026-07.md](DALIO_V2_DEEP_AUDIT_2026-07.md)) ha
> passato in rassegna l'implementazione delle Fasi 1-4 e il report unificato.
> Le issue trovate (look-ahead REER, real_credit_growth mal misurato,
> private_dsr min-max invece di percentile, nessun guard di staleness,
> writer senza lock, ecc.) sono state corrette nelle "Fasi A-E" descritte in
> quel documento. Precisazioni sullo stato dei ✅ qui sotto:
> - **Fase 0 non è mai stata eseguita**: i flag `reserve_currency_status`/
>   `commodity_exporter_flag`/`financial_center_flag` in countries.yaml e la
>   tabella `dalio_cycle_v2` NON esistono. Il set reserve-currency è
>   hardcoded in `external_constraint.py` (USA/JPN/GBR/CHE + membri euro);
>   i caveat vanno in `components_json`, non in `dalio_cycle_v2.caveats_json`.
> - Deviazioni di metodo rispetto al testo del piano (documentate nei moduli):
>   trend REER via OLS (non media mobile 10y); npl_ratio a soglie assolute
>   (non percentile cross-country); la componente "FX depreciation 12m" del
>   §12.4 è sostituita da fx_overvaluation_pct; il fallback
>   DT.DOD.DSTC.ZS per fx_debt_share non è implementato (cap di tier al suo
>   posto). Il doppio trend actuals-only della Fase 1 è stato aggiunto
>   nell'audit trail (campo `debt_trend_actuals_5y` + flag).
> - `robust_z` (design decision #5) è stato rimosso come dead code: nessun
>   motore usa z-score cross-country; political_execution usa percentile.

> **Come usare questo documento in un'altra sessione:** è scritto per essere
> autosufficiente. Non serve rileggere la conversazione originale. Leggi in
> ordine: §1 (contesto), §2 (verdetto di fattibilità dati — il cuore del
> documento), poi esegui le fasi in §4 nell'ordine dato. Ogni fase ha
> obiettivo, file da toccare, logica esatta, e definition of done.
>
> Documenti collegati (leggerli se serve più contesto, non obbligatorio per
> eseguire il piano):
> - [DALIO_CHATGPT_5ENGINE_PROPOSAL_2026-07.md](DALIO_CHATGPT_5ENGINE_PROPOSAL_2026-07.md) — proposta originale (ChatGPT) che ha innescato questo piano. Fonte di formule/soglie/pseudocodice citate qui.
> - [DALIO_METHODOLOGY_REVIEW_2026-07.md](DALIO_METHODOLOGY_REVIEW_2026-07.md) — revisione statistica precedente del sistema attuale (`dalio.py`). I difetti che identifica (policy rate ≠ costo debito, look-ahead da forecast WEO, z-score fragili, nessuna isteresi, nessun backtest) restano validi e vanno tenuti a mente mentre si costruiscono i nuovi motori — **non riprodurli**.
> - [DALIO_DATA_COVERAGE_2026-07.md](DALIO_DATA_COVERAGE_2026-07.md) — audit di copertura dati del sistema attuale (measurements 2026-07-08).
> - [DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md](DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md) — addendum tecnico che precisa la Fase 6 (validazione) qui sotto: point-in-time correctness (riusa `macro_panel_vintage`/`reader.read_macro_panel(asof=...)`, già esistenti nel repo) e schema fisso per `engine_scores.components_json`. Leggerlo prima di eseguire la Fase 6.
> - [DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md](DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md) — note su uno stack Python di portfolio construction (skfolio/Riskfolio-Lib/zipline/ecc.). **Deferred, non fa parte di questo piano** — i 5 motori producono diagnosi paese, non pesi di portafoglio.

---

## 1. Contesto e obiettivo

Il sistema attuale (`market_data_hub/dalio.py` + `make_dalio_report.py`) classifica
64 paesi con **un** classificatore a soglie + **un** composite z-score
cross-country pesato su 10 pillar. La proposta ChatGPT (documento collegato)
suggerisce di sostituirlo con **5 motori indipendenti** (Sovereign Solvency,
Funding Liquidity, Private Credit Cycle, External Currency Constraint,
Political Execution) che alimentano un classificatore finale multi-dimensionale,
invece di un singolo score compensabile.

**Obiettivo di questo piano:** rendere quella proposta implementabile,
correggendo dove la proposta assume dati che non esistono gratis su scala
globale, e sequenziando il lavoro in base a cosa è già disponibile oggi nel
repo vs cosa richiede nuovi connettori.

**Non-goal:** questo piano NON sostituisce `dalio.py` in-place. I 5 motori
vengono costruiti come **livello additivo** (nuove tabelle, nuovo modulo),
mentre `dalio.py`/`make_dalio_report.py` restano intatti e continuano a
produrre l'output attuale. Solo dopo che i nuovi motori sono validati si
decide (in una fase separata, non coperta qui) se e come sostituire il
report esistente. Questo evita di rompere qualcosa che funziona mentre si
costruisce qualcosa di nuovo e non ancora testato.

---

## 2. Verdetto di fattibilità dati (verificato 2026-07-09, non teorico)

Prima di scrivere una riga di codice, questo è quello che è stato verificato
live (grep del repo + query World Bank/IMF/OECD/BIS/ECB/Treasury) sulle 9
fonti citate dalla proposta ChatGPT. **Questa sezione è la ragione per cui
l'ordine delle fasi in §4 non segue l'ordine dei 5 motori nel documento
originale.**

### 2.1 Cosa è già pronto nel repo (baseline, nessun nuovo connettore)

| Dato | Indicator ID in `macro_panel.yaml` | Fonte/connettore | Copertura (2026-07-08) |
|---|---|---|---:|
| Debito pubblico lordo/PIL | `public_debt_gdp` | IMF WEO via `imf.py` | 64/64 |
| Debito pubblico **netto**/PIL | `govt_net_debt_gdp` | IMF WEO via `imf.py` | ampia (WEO) |
| Saldo fiscale/PIL | `fiscal_balance_gdp` | IMF WEO | 64/64 |
| Saldo primario/PIL | `primary_balance_gdp` | IMF WEO | ampia |
| Crescita reale | `gdp_growth_weo` | IMF WEO | 64/64 |
| Inflazione | `inflation_avg_weo` | IMF WEO | 64/64 |
| **Costo effettivo del debito** | `interest_on_debt_gdp` → `implied_interest_rate` (view) | IMF Fiscal Monitor (`dataset: FM, code: ie`) | 60/64 |
| Rendimento reale lungo termine | `real_long_rate` | IMF FM (`code: rltir`) | 49/64 |
| Rendimento 10Y nominale | `bond_yield_10y` | FRED | 32/64 |
| REER | `reer_broad` | BIS | 56/64 |
| Credit-to-GDP gap | `bis_credit_gap` | BIS | 43/64 |
| DSR privato | `bis_dsr_private` | BIS | 32/64 |
| Debito corporate | `corporate_debt_gdp` | IMF GDD | 58/64 |
| Debito privato totale (proxy credit gap dove BIS manca) | `private_debt_gdp` | IMF GDD | 64/64 |
| NPL ratio | `npl_ratio` | World Bank WDI | 61/64 |
| Current account/PIL | `current_account_gdp` | IMF WEO (fallback WB) | ampia |
| NIIP | `iip_net_position` | IMF SDMX (IIP) | ampia |
| Debito estero verso non-residenti + quota in valuta estera | `ext_debt_nonres_usd`, `fx_debt_usd` → `fx_debt_share` (view) | IMF SDMX (**IIPCC**, non CPIS) | **~19/64** |
| **Governance (6 indicatori WGI)** | `wgi_voice_accountability`, `wgi_political_stability`, `wgi_government_effectiveness`, `wgi_regulatory_quality`, `wgi_rule_of_law`, `wgi_control_corruption` | World Bank WGI (`api_source_id: 3`) | 59-64/64, **già pesato nel composite (pillar `governance`, peso 5)** |
| Gini (disuguaglianza) | `gini` | World Bank WDI | 59/64 |
| Metadati paese: regione, income group, DM/EM, G7, EU, **euro area flag**, fx_regime | `market_data_hub/config/countries.yaml` | statico, manuale | 64/64, già ricco |

**Conclusione chiave #1:** il Political Execution Engine (§9 della proposta)
è quasi **già pronto** — i 6 indicatori WGI ci sono, sono già mappati al
pillar `governance` e già pesati nel composite attuale. Non serve nessun
nuovo connettore. Serve solo isolarlo come motore a sé stante con un proprio
scoring/etichettatura (vedi Fase 2).

**Conclusione chiave #2:** il costo effettivo del debito (`implied_interest_rate`,
60/64) e `r_minus_g` sono già calcolabili con dati esistenti — il P0.1 della
review metodologica precedente è già risolto lato dati, va solo isolato nel
nuovo Sovereign Solvency Engine (Fase 1).

**Conclusione chiave #3:** manca completamente un `country_master` con i
flag `reserve_currency_status`, `commodity_exporter_flag`, `financial_center_flag`
richiesti dalla proposta per i caveat automatici (§19 della proposta). Ma
`countries.yaml` copre già l'80% dello schema proposto in §13.1 (region,
income, development, euro flag, g7, eu, fx_regime) — va solo esteso con 3
campi nuovi, non creato da zero.

### 2.2 Cosa manca e quanto è colmabile gratis (verificato con ricerca live)

**Holder composition (chi detiene il debito — alimenta Funding Liquidity §6
e External Constraint §8 della proposta):**

| Fonte | API gratuita | Copertura reale | Verdetto |
|---|---|---|---|
| IMF CPIS | Sì (SDMX, no login) | ~80 paesi *reporter*, ma dato **mirror** (chi detiene cosa), non "chi detiene il debito di X" diretto — rumoroso su EM/frontier | Solo proxy approssimato, non affidabile ovunque |
| US Treasury TIC | Sì (CSV mensile) | **Solo USA** (detentori esteri di titoli USA) | Alimenta solo 1 riga su 64 |
| ECB SHSS | Sì (SDMX) | **19 paesi eurozona**, dettaglio bilaterale per singolo paese estero riservato | Solo eurozona |
| IMF COFER | Sì (SDMX) | Confermato: **mai per singolo paese**, solo mondo/gruppo aggregato | Morto per un panel paese-per-paese |
| Arslanalp-Tsuda (IMF WP dataset) | **No API vera**, XLSX manuale, ancora così a metà 2026 | ~24 AE + set EM, buon dettaglio | Lavoro manuale ricorrente, non automatizzabile via cron |
| World Bank QEDS/IDS (SDDS) | Sì (API `api.worldbank.org/v2`) | **73 paesi SDDS-subscriber** | Solo quota debito **estero**, no sector split — ma è la copertura più ampia reale, e IDS/QEDS **non è ancora wired nel repo** (nessun `api_source_id` per IDS in `worldbank.py`/`macro_panel.yaml`) |

**Verdetto: un panel a 60-64 paesi, sector-level, completamente
programmatico e gratuito NON è costruibile.** Il meglio ottenibile:
dettaglio pieno (sector-of-holder) per ~20-30 paesi (ECB SHSS + TIC + eventuale
ingest manuale Arslanalp-Tsuda), quota-estera approssimata più ampia (~73
paesi) via QEDS/IDS.

**Funding mechanics (GFN, aste, maturity wall — Motore 2 della proposta):**

| Fonte | API gratuita | Copertura | Ha GFN/aste/maturity reali? |
|---|---|---|---|
| IMF Fiscal Monitor DataMapper | Sì | ~190 paesi | **No** — GFN esiste solo in PDF/Excel per ~30 paesi curati, non è un indicatore DataMapper |
| IMF Article IV/DSA | No API | per paese | Solo PDF |
| OECD Central Gov Debt Statistics (SDMX) | Sì, API vera | **Solo 34-38 paesi OECD** | Sì, maturity structure — ma esclude ~25-30 EM/frontier del panel |
| DMO nazionali (US TreasuryDirect/fiscaldata.treasury.gov, UK DMO, ecc.) | US: sì, JSON pulito con bid-to-cover. UK: solo tabelle web | Un paese alla volta | Sì per i pochi paesi con open data portal, zero aggregatore gratuito cross-country |
| World Bank QEDS/IDS | Sì | ~120+ paesi | Solo debito **estero** short/long term — proxy debole per rollover risk, non GFN vero |
| World Bank WGI | Sì | 214 paesi | N/A (già trattato sopra) |

**Verdetto: GFN e dati d'asta reali (bid-to-cover, auction tail) restano
gratis solo per ~15-25 economie maggiori/OECD.** Per gli altri ~35-45 paesi
del panel l'unico proxy libero è `DT.DOD.DSTC.ZS` (World Bank IDS: quota
debito estero a breve termine sul totale) combinato con lo stock WEO — un
segnale strutturalmente più debole di GFN/maturity-wall/aste, e va
etichettato come tale, non spacciato per equivalente.

### 2.3 Matrice di fattibilità finale per motore

| Motore | Fattibilità 64 paesi | Cosa manca | Priorità di build |
|---|---|---|---|
| **Sovereign Solvency** | ✅ ~95% pronto | nulla di bloccante | **1 (subito)** |
| **Political Execution** | ✅ ~95% pronto | 3 flag statici in countries.yaml | **1 (subito, insieme al precedente)** |
| **Private Credit Cycle** | ⚠️ 67% (credit gap) / 50% (DSR) — gap noto, 21 paesi a zero BIS | nessun nuovo connettore, ma serve fallback esplicito via `private_debt_gdp` (IMF GDD, 64/64) per i 21 orfani | **2** |
| **External Currency Constraint** | ⚠️ A due livelli: dettaglio buono ~19-30 paesi, quota-estera proxy ~73 paesi | connettore World Bank **IDS** (nuovo `api_source_id`, non WDI/WGI) | **3** |
| **Funding Liquidity** | ❌ Solo ~15-25 paesi con dati reali (OECD+major); resto = proxy debole | connettore World Bank IDS (condiviso con External Constraint) + eventuale OECD SDMX per il sottoinsieme ricco; **niente aste/GFN globali, va dichiarato esplicitamente come limite strutturale, non colmabile** | **4 (ultimo, scope ridotto rispetto alla proposta originale)** |

---

## 3. Decisioni di design (da rispettare in tutte le fasi)

1. **Output additivo, non distruttivo.** Nuove tabelle (§4.0), `dalio.py`
   e le tabelle `dalio_signals`/`pillar_scores`/`regime_state` restano
   invariate finché non si decide un cutover esplicito (fuori scope qui).
2. **Ogni riga porta un tier di copertura.** Non un numero unico senza
   contesto: ogni score per (country, engine, date) porta anche
   `coverage_tier` (`full` / `proxy` / `insufficient`) e `confidence`
   (`high`/`medium`/`low`, come da proposta §18). Un paese con `insufficient`
   su Funding Liquidity non riceve un numero fittizio — riceve NULL + il
   motivo.
3. **Niente compensazione tra motori.** I 5 output restano separati (come
   vuole la proposta, §11.1). Il classificatore finale (Fase 5) combina le
   *etichette* con regole IF/THEN esplicite, non fa una media pesata unica —
   è esattamente il difetto (§2.4 della review metodologica) che la
   struttura a 5 motori deve evitare, quindi non reintrodurlo nel
   classificatore finale.
4. **Isteresi minima fin da subito.** A differenza della proposta originale
   (che usa soglie crisp ovunque, §11.2/§14), ogni bucket categorico
   (strong/stable/watch/stressed/critical, ecc.) usa **due soglie con banda
   morta** invece di una sola: un paese passa da `watch` a `stressed` solo
   se supera `stress_threshold + hysteresis_margin`, e torna indietro solo
   sotto `stress_threshold - hysteresis_margin`. Margine iniziale: 10% della
   distanza tra soglie adiacenti. Questo risponde direttamente al difetto
   §2.5 della review metodologica ("nessuna isteresi, flip-flop ai confini")
   che la proposta ChatGPT non affronta.
5. **z-score robusti, non media/std.** Dove si calcola uno z cross-country
   (tutti i motori), usare mediana/MAD invece di media/std (raccomandazione
   §4.4 della review metodologica): `z = 0.6745 * (x - median) / MAD`,
   clip a ±3.5 (soglia standard per MAD). Riusa/estendi l'helper esistente
   in `dalio.py`, non duplicarlo.
6. **Pesi dichiarati come arbitrari finché non validati.** Ogni tabella di
   pesi (§6.3, §7.4, §9.3 della proposta) va salvata in
   `config/settings.yaml` sotto un blocco `dalio_v2:`, MAI hardcoded nel
   Python, cosicché la Fase 6 (sensitivity analysis) possa iterare senza
   toccare codice.

---

## 4. Fasi

### Fase 0 — Fondamenta comuni

**Obiettivo:** infrastruttura condivisa da tutti i motori, così le fasi 1-5
non duplicano codice.

**Task:**

1. **Estendi `market_data_hub/config/countries.yaml`** con 3 campi nuovi per
   ciascuno dei 64 paesi (compilazione manuale, sono fatti statici e noti):
   - `reserve_currency_status: true` per USA, area euro (tutti i membri),
     JPN, GBR, CHE (le 5 valute/aree che la proposta cita esplicitamente in
     §8.5); `false` altrove.
   - `commodity_exporter_flag`: true per i paesi dove export di
     petrolio/gas/minerali >~20% export totali (fonte rapida: WEO
     "Fuel exporters" classification, o World Bank commodity dependence
     data, già citata come esistente). Elenco indicativo da verificare:
     SAU, ARE, QAT, KWT, RUS, NOR, AUS, CAN, CHL, PER, ZAF, NGA, COL, IDN.
   - `financial_center_flag`: true per SGP, HKG, CHE, LUX, IRL, NLD (§19.6
     della proposta).
2. **Nuove tabelle DB** in `market_data_hub/db/schema.sql`, in coda alla
   sezione Dalio esistente (dopo `regime_state`, prima di `download_log`).
   Pattern identico alle tabelle esistenti (stesso stile PK, `computed_at`):

   ```sql
   -- ----------------------------------------------------------------------
   -- Dalio v2: 5-engine architecture (additive, non sostituisce dalio_signals/
   -- pillar_scores/regime_state). Vedi docs/DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md
   -- ----------------------------------------------------------------------
   CREATE TABLE IF NOT EXISTS engine_scores (
       country_iso3   VARCHAR NOT NULL,
       ref_date       DATE    NOT NULL,
       engine         VARCHAR NOT NULL,   -- 'sovereign_solvency' | 'funding_liquidity' |
                                           -- 'private_credit' | 'external_constraint' |
                                           -- 'political_execution'
       score          DOUBLE,             -- 0-100, alto = peggio (rischio), come da proposta
       label          VARCHAR,            -- bucket categorico del motore (vedi §12 proposta)
       coverage_tier  VARCHAR,            -- 'full' | 'proxy' | 'insufficient'
       confidence     VARCHAR,            -- 'high' | 'medium' | 'low'
       n_components   INTEGER,            -- quanti input su quanti attesi
       components_json VARCHAR,           -- {"debt_gdp": {"value":.., "z":..}, ...} per audit
       computed_at    TIMESTAMP,
       PRIMARY KEY (country_iso3, ref_date, engine)
   );

   CREATE TABLE IF NOT EXISTS dalio_cycle_v2 (
       country_iso3        VARCHAR NOT NULL,
       ref_date            DATE    NOT NULL,
       dalio_stage          VARCHAR,   -- early/mid/late/crisis/post-crisis (§11.1 proposta)
       deleveraging_type    VARCHAR,   -- none/beautiful/inflationary/repressive/restructuring/ugly
       overall_confidence   VARCHAR,   -- min confidence tra i motori usati nella regola
       top_risk_drivers_json VARCHAR, -- top 5 componenti che pesano di più, per country sheet
       caveats_json          VARCHAR, -- lista caveat attivati (§19 proposta)
       computed_at           TIMESTAMP,
       PRIMARY KEY (country_iso3, ref_date)
   );
   ```

3. **Helper condiviso** in nuovo modulo `market_data_hub/dalio_v2/scoring.py`:
   - `robust_z(values: pd.Series, orientation: int) -> pd.Series` — mediana/MAD
     come da §3.5 sopra.
   - `score_threshold(value, watch, stress, critical, orientation=1) -> float`
     — mappa un valore grezzo su scala 0-100 per interpolazione lineare tra
     le soglie (non un semplice `if/elif`, per evitare gradini bruschi:
     0 sotto `watch`, 50 a `stress`, 100 a `critical`, interpolato tra i
     punti). Questa è la funzione `score_threshold` che la proposta usa nello
     pseudocodice (§14) ma non definisce mai — va scritta da zero qui.
   - `bucket_with_hysteresis(score, thresholds, prev_bucket) -> str` —
     implementa la banda morta di §3.4. Richiede lo stato precedente
     (ultimo bucket assegnato a quel paese/motore), letto da `engine_scores`
     prima di sovrascriverlo.
   - `coverage_tier(n_available, n_expected) -> str` — `full` se
     `n_available/n_expected >= 0.8`, `proxy` se `>= 0.4`, altrimenti
     `insufficient`.
4. **Aggiungi il blocco config** `dalio_v2:` in `settings.yaml` (vuoto per
   ora, popolato fase per fase con soglie/pesi — vedi §3.6).

**Definition of done:** `countries.yaml` ha i 3 nuovi campi per tutti i 64
paesi; `schema.sql` applica le due nuove tabelle senza errori
(`get_conn()` le crea automaticamente); `dalio_v2/scoring.py` ha test
unitari minimi per le 4 funzioni (valori noti in ingresso → output atteso).

---

### Fase 1 — Sovereign Solvency Engine + Political Execution Engine

> ✅ **Implementata** (2026-07-09) — `market_data_hub/dalio_v2/{sovereign_solvency,political_execution}.py`,
> config in `settings.yaml` (`dalio_v2:`), test in `tests/test_dalio_v2.py`.

*(Raggruppate perché entrambe usano dati già presenti al 95%+, quindi è il
lavoro a più alto ritorno/minimo rischio — farle insieme.)*

**Obiettivo Sovereign Solvency:** score 0-100 (100 = insolvenza probabile)
da 7 componenti (proposta §5.5).

**Logica esatta:**

```text
per ogni paese, ref_date:
  debt_gdp            = public_debt_gdp (IMF WEO, già in panel)
  net_debt_gdp         = govt_net_debt_gdp (IMF WEO)
  interest_gdp         = interest_on_debt_gdp (IMF FM)
  interest_revenue     = interest_on_debt_gdp / (government_revenue_gdp/100)   # NUOVO calcolo, entrambi input già in panel
  primary_deficit_gdp  = -primary_balance_gdp (segno invertito: deficit positivo = peggio)
  g_nominale           = (1 + gdp_growth_weo/100) * (1 + inflation_avg_weo/100) - 1   # formula esatta §5.3 proposta, NON l'approssimazione additiva
  r_effettivo          = implied_interest_rate (già calcolato come view in dalio.py: interest %GDP ÷ debt %GDP)
  r_minus_g            = r_effettivo - g_nominale
  debt_trend_5y         = riusa _slope() esistente da dalio.py su [ref-3, ref+5] — MA calcola
                          ANCHE una seconda versione solo-actual [ref-5, ref] (raccomandazione
                          P1.2 della review metodologica: segnalare quando il trend è
                          forecast-dependent, non solo usare quello con le proiezioni)

  componenti = {
    debt_gdp:            score_threshold(debt_gdp, watch, stress, critical)   # soglie per income group, vedi sotto
    net_debt_gdp:        score_threshold(net_debt_gdp, ...)   # se disponibile, altrimenti pesa 0 e ridistribuisci
    interest_revenue:    score_threshold(interest_revenue, 10, 15, 25)   # soglie §12.1 proposta
    interest_gdp:        score_threshold(interest_gdp, 3, 5, 7)
    primary_deficit_gdp: score_threshold(primary_deficit_gdp, 2, 4, 6)
    r_minus_g:           score_threshold(r_minus_g, 1, 3, 5)
    debt_trend_5y:       score_threshold(debt_trend_5y, 0.7, 1.5, 3.0)   # riusa soglie esistenti debt_trend_moderate/high da settings.yaml
  }
  score = media pesata dei componenti disponibili (pesi uguali 1/7 per iniziare — la
          sensitivity analysis di Fase 6 li affina); coverage_tier = coverage_tier(n_disponibili, 7)
  label = bucket_with_hysteresis(score, {watch:20, stress:40, critical:60, ...}, prev_label)
```

**Soglie per income group (§12.1 proposta, differenziate AE vs EM):**
usa il campo `income`/`development` già presente in `countries.yaml`
(`DM`/`EM`) per scegliere la riga soglie giusta per `debt_gdp` e
`net_debt_gdp` (AE: 90/110/130; EM: 60/80/100). Le altre soglie (interessi,
deficit, r-g) restano uguali per tutti — la proposta non le differenzia e
non c'è motivo economico forte per farlo in questa prima versione.

**Obiettivo Political Execution:** score 0-100 (100 = esecuzione politica
impossibile) da 5 componenti WGI, già tutti disponibili.

**Logica esatta (§9.3 proposta, pesi da salvare in settings.yaml):**

```text
political_execution_score =
    0.30 * (100 - percentile(wgi_government_effectiveness))   # WGI è "alto = buono", quindi invertito
  + 0.25 * (100 - percentile(wgi_rule_of_law))
  + 0.20 * (100 - percentile(wgi_control_corruption))
  + 0.15 * (100 - percentile(wgi_political_stability))
  + 0.10 * (100 - percentile(wgi_regulatory_quality))
```
Usa il percentile cross-country (non lo z grezzo) perché i WGI sono già
indici standardizzati -2.5/+2.5 — un percentile 0-100 è più leggibile nel
bucket finale ed evita di ri-standardizzare un indice già standardizzato.

`voice_accountability` (6° indicatore WGI) NON entra nella formula (la
proposta stessa non lo include in §9.3, benché lo elenchi in §9.2 — è
intenzionale nella loro stessa proposta, mantienilo così).

**File da creare:** `market_data_hub/dalio_v2/sovereign_solvency.py`,
`market_data_hub/dalio_v2/political_execution.py`, ciascuno con una funzione
`compute(con, ref_date) -> pd.DataFrame` che scrive su `engine_scores`.

**Definition of done:** per un run su dati reali, Sovereign Solvency ha
`coverage_tier='full'` per almeno 55/64 paesi (in linea con la copertura
`implied_interest_rate` 60/64), Political Execution ha `full` per almeno
59/64. Smoke test manuale sui 4 paesi esempio della proposta (USA, ARG,
SGP, JPN, §15) — verificare che la label prodotta sia qualitativamente in
linea con la diagnosi attesa lì descritta (non serve match esatto, serve
plausibilità: USA e JPN devono uscire watch/stressed su Sovereign Solvency,
SGP strong, ARG stressed/critical).

---

### Fase 2 — Private Credit Cycle Engine

> ✅ **Implementata** (2026-07-09) — `market_data_hub/dalio_v2/private_credit.py`.
> `real_house_price_gap` resta sempre `None` (nessun connettore BIS WS_SPP —
> segnato come lavoro opzionale non fatto, non bloccante).

**Obiettivo:** score 0-100 da 5 componenti (proposta §7.4), con fallback
esplicito per i 21 paesi senza dati BIS.

**Logica esatta:**

```text
per ogni paese:
  credit_gap = bis_credit_gap se disponibile (43/64)
             altrimenti CALCOLA un proxy da private_debt_gdp (IMF GDD, 64/64):
                proxy_credit_gap = private_debt_gdp - rolling_trend(private_debt_gdp, 10y, HP-filter o media mobile)
                # marca coverage_tier='proxy' quando si usa questo ramo, MAI 'full'
  private_dsr = bis_dsr_private se disponibile (32/64), altrimenti NULL (nessun proxy libero affidabile — dichiaralo mancante, non inventarlo)
  real_credit_growth = crescita YoY di private_debt_gdp deflazionata per inflation_avg_weo
  real_house_price_gap = NULL per ora (nessuna fonte gratuita wired nel repo — BIS ha una property price series
                          separata, WS_SPP, non ancora nel connettore bis.py: aggiungerla è lavoro OPZIONALE
                          di questa fase, vedi nota sotto)
  npl_ratio = npl_ratio (World Bank WDI, 61/64, già in panel)

  componenti = {
    credit_gap:            0.30 * score_threshold(credit_gap, 2, 5, 10)
    private_dsr:            0.25 * score_threshold(private_dsr, percentile 75/90/95 della STORIA del paese — riusa _pct_in_range() da dalio.py, non un livello assoluto)
    real_credit_growth:     0.15 * score_threshold(real_credit_growth, 5, 8, 12)
    real_house_price_gap:   0.15 * (se disponibile, altrimenti redistribuisci il peso sugli altri 4)
    npl_ratio:              0.15 * score_threshold(npl_ratio, ...)   # soglie da calibrare, non nella proposta — usa percentile cross-country nel frattempo
  }
```

**Nota opzionale (non bloccante):** BIS pubblica anche `WS_SPP` (residential
property prices) via lo stesso connettore SDMX già usato per credit gap/DSR
— aggiungere `real_house_price_gap` è un'estensione naturale di `bis.py`
(stesso pattern di `bis_credit_gap`), ma non è necessaria per chiudere la
fase: se manca tempo, lasciare il componente NULL con redistribuzione pesi
è accettabile e già previsto sopra.

**Definition of done:** tutti i 64 paesi ricevono uno score (nessun NULL
totale), ma quelli senza BIS credit gap E senza BIS DSR sono marcati
`coverage_tier='proxy'` esplicitamente — non silenziosamente uguali a chi ha
dati BIS pieni. Verifica che i 21 paesi già noti come "zero dati BIS" dal
coverage audit (ARE, BGD, BGR, CYP, EGY, EST, HRV, KWT, LTU, LVA, MLT, NGA,
PAK, PER, PHL, QAT, ROU, SVK, SVN, UKR, VNM) risultino tutti `proxy`, non
`full`.

---

### Fase 3 — External Currency Constraint Engine

> ✅ **Implementata** (2026-07-09) — `market_data_hub/dalio_v2/external_constraint.py`.
> **Correzione post-implementazione (stesso giorno):** il "Prerequisito —
> nuovo connettore World Bank IDS" sotto si è rivelato **non necessario**.
> `macro_panel.yaml` aveva già `external_debt_gni` e `debt_service_exports`
> (WDI, `api_source_id: 2`, già live/verificati) con **lo stesso identico
> codice World Bank** (`DT.DOD.DECT.GN.ZS` / `DT.TDS.DECT.EX.ZS`) dei nuovi
> indicatori IDS aggiunti in un primo momento — e `short_term_debt_reserves`
> (già presente, WDI `DT.DOD.DSTC.IR.ZS`) è esattamente il rapporto
> "short-term external debt/reserves" richiesto dalla proposta (§12.4,
> soglie 50/100/150), migliore del rapporto %-su-debito-estero-totale che
> l'indicatore IDS rimosso usava. Il blocco IDS (`api_source_id: 6`, mai
> verificato live) è stato rimosso da `macro_panel.yaml`; il motore ora
> legge questi tre indicatori pre-esistenti. **Nessun nuovo connettore
> serviva davvero** — il prerequisito sotto resta come nota storica di cosa
> è stato *considerato* prima di scoprire la duplicazione, non come
> descrizione dello stato finale.

**Obiettivo:** score 0-100 da 7 componenti (proposta §8.3), a due livelli di
copertura come emerso da §2.2 sopra.

**Prerequisito — nuovo connettore World Bank IDS:**
`worldbank.py` già supporta WDI (`api_source_id: 2`) e WGI (`api_source_id: 3`)
con lo stesso pattern URL (`api.worldbank.org/v2/country/{iso3}/indicator/{code}?source={api_source_id}`).
IDS (International Debt Statistics) è un altro "source" nello stesso API
World Bank — **verificare l'id esatto interrogando `https://api.worldbank.org/v2/sources?format=json`
prima di hardcodarlo** (non era ancora stato verificato al momento di
scrivere questo piano). Aggiungere in `macro_panel.yaml` nuovi indicatori
con `source: WB, dataset: IDS, api_source_id: <verificato>`:
  - `DT.DOD.DSTC.ZS` — short-term external debt / total external debt
  - `DT.DOD.DECT.GN.ZS` — external debt stock / GNI
  - `DT.TDS.DECT.EX.ZS` — total debt service / exports

Nessuna modifica di codice a `worldbank.py` dovrebbe servire: è lo stesso
connettore, solo un nuovo `api_source_id` + nuovi `code` in config, esattamente
come WGI è stato aggiunto accanto a WDI.

**Logica esatta:**

```text
per ogni paese:
  current_account_gdp        = current_account_gdp (già in panel)
  niip_gdp                    = iip_net_position / gdp_current_usd * 100   # NUOVO calcolo, entrambi già in panel
  short_term_ext_debt_reserves = DT.DOD.DSTC.ZS (nuovo, IDS) combinato con fx_reserves_usd (già in panel)
  ext_debt_service_exports    = DT.TDS.DECT.EX.ZS (nuovo, IDS)
  fx_debt_share                = fx_debt_share (view esistente, IIPCC) se paese tra i ~19 coperti,
                                  altrimenti fallback su DT.DOD.DSTC.ZS come proxy grezzo (quota debito
                                  estero a breve come proxy di currency mismatch — più debole, marca 'proxy')
  inflation                    = inflation_avg_weo (già in panel)
  real_fx_overvaluation        = deviazione di reer_broad (BIS, 56/64) dalla propria media mobile 10y
  reserve_adequacy              = fx_reserves_months_imports (già in panel, WDI)

  score = combinazione pesata come score_threshold su ciascun componente, soglie §12.4 proposta
```

**Caveat automatico obbligatorio (§19.3 proposta):** se
`reserve_currency_status == true` (da countries.yaml, Fase 0), il punteggio
di vincolo esterno va abbassato ma va scritto in `dalio_cycle_v2.caveats_json`
un caveat esplicito tipo `"Reserve currency issuer: external constraint
score adjusted downward, monetary debasement risk elevated instead"` — così
il paese non risulta semplicemente "sicuro" senza spiegazione.

**Definition of done:** connettore IDS verificato e funzionante (test con
1-2 paesi noti, es. TUR/ARG dove il dato è pubblico e verificabile a mano);
copertura `full` (fx_debt_share via IIPCC) per ~19 paesi, `proxy` (via IDS)
per il resto dei ~73 paesi SDDS, `insufficient` per chi non è in nessuna
delle due fonti.

---

### Fase 4 — Funding Liquidity Engine (scope ridotto rispetto alla proposta)

> ✅ **Implementata, solo ramo B** (2026-07-09) — `market_data_hub/dalio_v2/funding_liquidity.py`.
> Il ramo A (OECD SDMX, dati GFN/aste reali per ~15-25 economie maggiori)
> **non è stato costruito** — richiede una verifica di rete non disponibile
> in questa sessione, oltre a un nuovo connettore `sources/oecd.py` da zero.
> `coverage_tier` è forzato a `proxy` (mai `full`) su ogni riga, per
> disciplina esplicita. **Correzione:** il rapporto short-term-debt/reserves
> usa `short_term_debt_reserves`, un indicatore WDI già presente (non un
> nuovo indicatore World Bank IDS come inizialmente previsto) — vedi la nota
> di correzione nella Fase 3 sopra, stesso indicatore condiviso.

**Attenzione:** questa è l'unica fase dove il piano si discosta
esplicitamente dalla proposta ChatGPT per limiti di dati reali (§2.2/§2.3
sopra). Non costruire l'illusione di un GFN/auction-based score globale.

**Obiettivo:** score utile per ~15-25 paesi con dati reali; per il resto,
un proxy dichiarato esplicitamente più debole, MAI mascherato da equivalente.

**Logica a due rami:**

```text
ramo A — paesi OECD/major (dati reali):
  Prerequisito: nuovo connettore OECD SDMX (market_data_hub/sources/oecd.py,
  stesso pattern di ecb.py: wildcard REST, no auth). Dataset target:
  OECD Central Government Debt Statistics (average maturity, short-term share).
  Verificare l'endpoint esatto su sdmx.oecd.org prima di implementare — non
  ancora verificato in questo piano (solo confermata l'esistenza dell'API).

  gfn_gdp                = da IMF Fiscal Monitor statistical appendix (~30 paesi,
                            NON in DataMapper — richiede parsing di un file Excel/CSV
                            pubblicato annualmente, non un endpoint REST pulito;
                            trattalo come import manuale annuale, simile a
                            import_investor_base.py esistente per Arslanalp-Tsuda)
  maturity_wall           = da OECD SDMX (nuovo connettore)
  foreign_holder_share    = da fx_debt_share (IIPCC, Fase 3) o ECB SHSS per i 19 euro
  yield_change_12m         = derivato da bond_yield_10y (FRED, già in panel) — YoY delta
  auction_tail/bid_to_cover = SOLO per USA (fiscaldata.treasury.gov, JSON pulito) — non
                              generalizzabile ad altri paesi senza integrazione one-off per
                              ciascuno; se serve espandere, farlo come task separato per paese
                              (UK DMO, IT MEF, ecc.), fuori scope di questa fase.

  score = combinazione pesata §6.3 proposta sui componenti disponibili, coverage_tier='full' o 'proxy'
          a seconda di quanti degli 8 componenti sono popolati

ramo B — resto del panel (~35-45 paesi, proxy):
  rollover_proxy = DT.DOD.DSTC.ZS (World Bank IDS, quota debito ESTERO a breve — Fase 3)
  yield_change_12m = se disponibile (bond_yield_10y FRED copre solo 32/64)
  score = media dei soli 2 componenti disponibili, coverage_tier='proxy' SEMPRE
          (mai 'full' per questo ramo — è strutturalmente un proxy più debole)
```

**Definition of done:** il report finale per un paese in `ramo B` mostra
esplicitamente "Funding Liquidity: proxy (rollover risk esterno come
sostituto di GFN/aste — dati non disponibili gratis per questo paese)",
non un numero silenzioso indistinguibile da un paese `ramo A`. Questo è il
punto di disciplina più importante di tutto il piano: la proposta originale
tratta Funding Liquidity come "il motore più importante", ma è anche quello
con la base dati più fragile — la Fase 4 deve rendere questa fragilità
visibile nel dato, non nasconderla dietro uno score che sembra comparabile
cross-country quando non lo è.

---

### Fase 5 — Classificatore finale (`dalio_cycle_v2`) ✅ (2026-07-10)

**Obiettivo:** combinare i 5 `engine_scores` in `dalio_stage` +
`deleveraging_type`, con le regole IF/THEN di §11.2 della proposta, MA con
isteresi (§3.4) e senza mai far collassare i 5 motori in un unico numero
prima di applicare le regole (§3.3).

**Logica:** implementare `classify_dalio_stage()` e `classify_deleveraging()`
esattamente come lo pseudocodice §14 della proposta, sostituendo però ogni
soglia crisp con `bucket_with_hysteresis()` (Fase 0) e ogni riferimento a
"funding_score"/"sovereign_score" con il valore da `engine_scores` per quel
paese/data. `top_risk_drivers_json` = i 3 componenti con lo z-score peggiore
tra tutti i motori (per la country card, §16.1 proposta).

**Costruita in `market_data_hub/dalio_v2/cycle_classifier.py`, con alcune
deviazioni deliberate e documentate rispetto allo pseudocodice §14/§11.2:**
- **Isteresi**: non un secondo meccanismo indipendente. Ogni ramo IF/THEN
  testa la **label** già stabilizzata dall'isteresi del motore sorgente
  (`bucket_with_hysteresis`), mai lo score grezzo — lo score grezzo NON è
  stabilizzato, quindi soglie sullo score reintrodurrebbero esattamente il
  flutter che l'isteresi esiste per prevenire. La stabilità del
  classificatore composito è ereditata da quella dei 5 motori.
- **"repressive"**: nessuna fonte dati sui holdings di banca centrale del
  debito esiste — usa un proxy (`r_effective_pct - inflation_pct` molto
  negativo E `fx_debt_share_pct` basso, come stand-in per "debito
  prevalentemente in valuta locale, assorbibile dal sistema domestico").
  Onestamente debole, ma **mantenuto come ramo separato e reale**: piegarlo
  in "inflationary" avrebbe reso il Giappone (uno dei quattro esempi del
  Definition of Done qui sotto) irraggiungibile.
- **"restructuring"**: nessun flag di evento di ristrutturazione esiste in
  nessuna parte del repo — folded in "ugly" come default conservativo,
  stesso trattamento di `real_house_price_gap` (sempre mancante) in
  `private_credit.py`.
- **Gating sulla copertura**: per-output, tutto-o-niente sui motori
  referenziati da almeno un ramo di quell'output (mai un soft-skip per-ramo,
  che potrebbe far atterrare un paese senza dati su "early_or_mid_cycle" —
  legge come "va tutto bene" quando la verità è "non lo sappiamo").
- **Ogni soglia/insieme di label vive in `settings.yaml::dalio_v2.cycle_classifier`**,
  mai hardcoded in Python — richiesta esplicita, validata da
  `validate_config.py`.

**Definition of done — verificato (2026-07-10)**, rieseguendo gli esempi
di riclassificazione §15 della proposta (USA, ARG, SGP, JPN) via
`classify_deleveraging()`/`classify_dalio_stage()` con input costruiti sul
profilo economico reale di ciascun paese:

| Paese | `deleveraging_type` ottenuto | `dalio_stage` ottenuto | Atteso (proposta §15) | Coerente? |
|---|---|---|---|---|
| USA | `none` (debito in crescita, non in calo) | `late_long_debt_cycle` | late long debt cycle | ✅ |
| ARG | **`inflationary`** (mai `beautiful`) | `crisis` | inflationary/post-crisis, non beautiful | ✅ — è il bug che ha aperto questa Fase 5 |
| SGP | `none` | `early_or_mid_cycle` | non late cycle | ✅ |
| JPN | **`repressive`** | `early_or_mid_cycle` (nota sotto) | repression/inflation tolerance/currency depreciation risk | ✅ sul tipo; lo stage dipenderà dal vero `sovereign_solvency` score sui dati reali (il debito/PIL giapponese reale è ben oltre la soglia `critical`) |

Verificato anche end-to-end attraverso la pipeline reale (non solo le
funzioni pure): `tests/test_dalio_v2_cycle_classifier.py::test_full_pipeline_arg_profile_is_inflationary`
fa girare `macro_panel` → tutti e 5 i motori reali → `dalio_cycle_v2` e
conferma `deleveraging_type == "inflationary"` sul caso Argentina.

---

### Fase 6 — Validazione e irrobustimento (P1/P2 della review metodologica, applicati al nuovo sistema)

Non fare quanto segue PRIMA che le fasi 1-5 producano output stabili — ma
non saltarlo: è quello che rende il nuovo sistema scientificamente più
solido del vecchio, non solo architetturalmente diverso.

1. **Sensitivity analysis sui pesi** (review §4, P1.6): per ogni motore,
   far variare i pesi ±30% entro bande plausibili e verificare quanto
   cambia il ranking dei paesi. Se il ranking è instabile, dichiararlo nel
   report (`dalio_cycle_v2` guadagna un campo `rank_stability` opzionale).
2. **Backtest storico** (review §4, P2.8; proposta §20): per il sottoinsieme
   di paesi/periodi dove i dati storici sono disponibili (WEO ha vintage
   storici via `macro_panel_vintage`, già in schema.sql, con un'API di
   lettura point-in-time già pronta — `reader.read_macro_panel(asof=...)`),
   ricostruire `dalio_cycle_v2` a data storica e verificare se avrebbe
   segnalato gli episodi di §20.1 della proposta (Giappone, Asia 1997,
   Argentina, USA 2008, Eurozona 2010-12, Grecia, Turchia, Sri Lanka, UK
   gilt 2022) con almeno 1 anno di anticipo. Riportare esplicitamente falsi
   positivi/negativi. **Vedi
   [DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md](DALIO_VINTAGE_AND_AUDIT_PLAN_2026-07.md)
   §3 Fase D per il protocollo esatto ("vintage replay test") — non
   reinventarlo qui, è già specificato passo-passo lì.**
3. **Nota di limiti nel report finale**: sezione obbligatoria (proposta
   §21.6/§21.7 "Methodology"/"Data Quality") che dichiari esplicitamente,
   per ogni motore: quanti paesi sono `full` vs `proxy` vs `insufficient`,
   e per Funding Liquidity in particolare il limite strutturale descritto
   in Fase 4 (non è un GFN/auction score globale, è un ibrido dati-reali +
   proxy-esterno).

---

## 5. Ordine di esecuzione riassunto

```text
Fase 0 → Fase 1 (Sovereign Solvency + Political Execution, parallele) →
Fase 2 (Private Credit) → Fase 3 (External Constraint, sblocca il
connettore IDS) → Fase 4 (Funding Liquidity, riusa IDS da Fase 3 +
eventuale nuovo connettore OECD) → Fase 5 (classificatore finale) →
Fase 6 (validazione, solo dopo che 1-5 sono stabili)
```

Le fasi 1-2 non hanno dipendenze tra loro e sono il lavoro a più alto
ritorno/minimo rischio — è ragionevole farle per prime anche se si ha tempo
limitato in una sessione, e fermarsi lì se serve: producono già 2 motori su
5 pienamente funzionanti con dati che il repo ha già, senza nessun nuovo
connettore esterno.

> ✅ **Report unificato v1+v2** (2026-07-09) — `make_dalio_report.py` è ora il
> dashboard unico: le classificazioni v1 (fase del ciclo, quadrante
> growth/inflation, tag paese, grafici storici) e i punteggi dei 5 motori v2
> convivono sulla stessa scheda paese (`python make_dalio_report.py --calc
> --calc-v2`). `run_dalio_v2.py` resta come alternativa più leggera, solo-v2,
> per un refresh veloce senza dover popolare le tabelle di classificazione v1.
> `make_dalio_report.py` degrada in modo pulito (nessun crash, nota esplicita)
> se `engine_scores` non è ancora popolata.

## 6. Cosa NON fare (promemoria esplicito)

- Non tentare di costruire un CPIS/TIC/SHSS/COFER "unificato" a 64 paesi:
  è stato verificato non esistere gratis (§2.2). Se serve dettaglio
  sector-of-holder oltre ai ~19-30 paesi coperti, l'unica strada è ingest
  manuale periodico di Arslanalp-Tsuda (stesso pattern già scaffoldato in
  `import_investor_base.py`), non un nuovo connettore automatico.
- Non promettere GFN/auction data per l'intero panel (§2.2/Fase 4): è un
  limite strutturale delle fonti pubbliche gratuite, non una lacuna di
  ingegneria colmabile con più tempo.
- Non sostituire `dalio.py`/`make_dalio_report.py` finché Fase 6 non ha
  dato un verdetto sulla robustezza del nuovo sistema (§1, non-goal).
- Non tornare a un singolo composite z-score pesato per il classificatore
  finale (Fase 5) — è esattamente il difetto che la struttura a 5 motori
  deve risolvere.
- Non introdurre librerie di portfolio construction (skfolio, Riskfolio-Lib,
  PyPortfolioOpt, bt, zipline-reloaded, vectorbt, pyfolio-reloaded,
  empyrical-reloaded, alphalens-reloaded) o logica di risk parity/asset
  allocation: i 5 motori producono diagnosi paese, non pesi di portafoglio,
  e non c'è oggi un consumatore a valle. Deferred — vedi
  [DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md](DALIO_PYTHON_PORTFOLIO_FRAMEWORK_NOTES_2026-07.md).

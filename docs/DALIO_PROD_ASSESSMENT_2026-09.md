# LazyRay in produzione — assessment metodologico e proposta (2026-09-04)

> Oggetto: il job `InvestmentCommittee_LazyRay_DalioV2Report` (lun–ven 13:40,
> `run_dalio_v2.py --csv` → `send_telegram_report.py`). Tutti i numeri sotto sono
> misurati oggi su `lazyray.duckdb` e `market_data.duckdb` di produzione, non
> dedotti dal codice. I documenti di luglio (`DALIO_METHODOLOGY_REVIEW`,
> `DALIO_DATA_COVERAGE`, `DALIO_VINTAGE_AND_AUDIT_PLAN`) avevano già visto la
> maggior parte dei problemi di metodo; questo documento misura **cosa è
> effettivamente in produzione oggi** e propone un assetto coerente con la
> cadenza reale dei dati.

## 1. Verdetto in tre righe

1. **Il job è giornaliero, i dati no.** 76 degli 83 indicatori sono annuali;
   gli input cambiano in pochi giorni al mese. Il "daily" produce quasi sempre
   lo stesso file — e nessuno può accorgersene, perché l'output è una sola
   fotografia sovrascritta (`ref_date = 2026-12-31`, un CSV e un HTML con lo
   stesso nome ogni giorno). Conseguenza non ovvia: **l'isteresi non è mai
   attiva** (cerca un `ref_date` precedente che non esiste).
2. **Le "condizioni correnti" sono proiezioni.** Con `ref_date` a fine anno,
   le componenti WEO leggono la previsione 2026 (USA: debito 125,8% = forecast
   IMF), mescolate ad actual 2024 e a un trend che arriva al 2031. Il report
   dice "As of 2026-12-31": una data futura.
3. **Le scale non sono comparabili tra paesi**, e l'etichetta lo nasconde:
   Turchia "strong" (0,8) e Argentina "strong" (4,3) sul sovrano; mediana del
   funding = 0 e 14 paesi senza punteggio; 49/64 "early_or_mid_cycle". Un
   framework "Dalio" che nel 2026 non vede nessun paese nella fase avanzata del
   ciclo del debito non sta misurando il ciclo del debito.

## 2. Che cosa gira davvero (misurato)

| Aspetto | Stato in produzione |
|---|---|
| Schedulazione | lun–ven 13:40; ~50 s; Telegram riceve un HTML da 398 KB con caption fissa |
| Output | `engine_scores` 320 righe = 64 paesi × 5 motori, **un solo `ref_date` (2026-12-31)**, `INSERT OR REPLACE` → nessuna storia; `dalio_cycle_v2` 64 righe |
| v1 (`dalio.py`, z-score, four-box) | **non gira**: `dalio_signals`, `pillar_scores`, `regime_state` = 0 righe |
| Artefatti | `reports/dalio_v2/` contiene 1 CSV + 1 HTML, sovrascritti ogni giorno |
| Point-in-time | `vintage_safe=false` su tutte le righe; `v_macro_panel_asof.known_from` NULL al 100%; log vintage del hub esiste solo dal 2026-07-10 (28 date) |
| Isteresi | `prev_label()` filtra `ref_date < ref_date corrente` → con un solo `ref_date` restituisce sempre None: assegnazione secca ogni giorno |

### 2.1 Cadenza reale degli input (macro_panel_coverage, 04/09/2026)

| Famiglia | Indicatori | Frequenza | Ultimo dato | Copertura |
|---|---|---|---|---|
| IMF WEO | 19 (debito, saldo fiscale, crescita, inflazione, CA…) | annuale + **proiezioni al 2031** | actual 2025 (stime), forecast oltre | 64/64 |
| World Bank WDI | 43 | annuale | 2025 (lag 247 gg); esterni IDS 2024 **solo 20/64** (LIC/MIC); turismo 2020; rendite risorse 2021 | 31–100% |
| WGI | 6 | annuale | 2024 (lag 612 gg) | 64/64 |
| IMF GDD/FM | 5 (debito privato, famiglie, imprese, spesa/entrate) | annuale | 2024 | 58–64/64 |
| BIS gap / DSR | 2 | trimestrale | 2025-Q4 (lag strutturale 6–9 mesi) | 43 / 32 su 64 |
| BIS policy rate, REER; ECB MIR; IMF MFS_IR | 5 | mensile | lug–ago 2026 | 21–56/64 |
| 10Y yield (FRED IRLTLT, derivato) | 1 | mensile | **giugno 2026** | 32/64 (OCSE) |

Nel log vintage del hub le revisioni si concentrano in 4 giorni su 55
(15/07: 1.015 righe, 23/07: 774, 31/07: 1.610, 20/08: 713 — refresh WDI/WEO/IIP);
negli altri giorni cambiano <60 righe, quasi solo REER e policy rate. Nessuna
revisione di `public_debt_gdp`/`fiscal_balance_gdp`/`gdp_growth_weo` dal 10/07.
Nota: `macro_panel.updated_at` viene aggiornato ogni giorno su tutte le righe
dall'upsert, quindi "aggiornato oggi" non significa "dato nuovo".

### 2.2 Distribuzioni degli score (oggi)

| Motore | mediana | p75 | etichette | tier |
|---|---|---|---|---|
| sovereign_solvency | 10,1 | 24,5 | strong 43 · stable 14 · watch 5 · stressed 1 | full 60 |
| private_credit | 2,3 | 23,7 | low 47 · moderate 12 · elevated 4 · high 1 | full 32 · proxy 32 |
| external_constraint | 12,0 | 18,9 | low 51 · moderate 9 · elevated 3 | **proxy 59** (5,6 input su 8) |
| funding_liquidity | **0,0** | **0,0** | easy 48 · stress 1 · severe 1 · **n/a 14** | proxy 50 (1,04 input su 2) |
| political_execution | 49,7 | 75,6 | 12–15 per bucket | full 64 (percentile: uniforme per costruzione) |

Ciclo: early_or_mid 49 · late_leveraging 1 · **None 14**; deleveraging: none 31,
beautiful 14, inflationary 3, ugly 2.

### 2.3 Tre casi che mostrano il problema

- **USA** — sovereign 44,6 "watch": `debt_gdp` 125,8 (**forecast WEO 2026**,
  obs_date 2026-12-31), `debt_trend_5y` +2,8 pp/anno (finestra 2023→2031,
  `forecast_dependent=true`; sugli actual è +0,59), interessi e saldo primario
  2024. Tre date diverse dentro un solo punteggio "as of 2026-12-31".
- **Turchia** — sovereign 0,8 "strong" (debito 25,5% PIL, r−g = −20 perché
  l'inflazione al 28,6% gonfia la crescita nominale) mentre lo stesso paese ha
  external "elevated" 50 (debito in valuta 88%, ST debt/reserves 115%,
  overvaluation REER 29%), funding "stress" 64,5 e political "impaired" 85.
  L'etichetta "strong" è un'affermazione di solvibilità che gli input non
  sostengono: è "basso debito", non "forte".
- **Argentina** — sovereign 4,3 "strong"; funding 100 "severe" calcolato su
  **un solo** input (ST debt/reserves).

### 2.4 Il motore funding non è un motore

`funding_liquidity` ha 2 input possibili: variazione 12m del 10Y (FRED, mensile,
32 paesi OCSE, fermo a giugno) e ST debt/reserves (IDS, annuale, 20 paesi a
basso/medio reddito). Gli insiemi sono quasi disgiunti: 30 paesi hanno solo il
primo, 18 solo il secondo, 14 nessuno (ARE, KWT, QAT, SAU, SGP, HKG, MYS, CYP,
EST, HRV, LTU, LVA, MLT, ROU). Lo stesso bucket "easy/stress" confronta
grandezze diverse su popolazioni diverse. Il testo del modulo lo dichiara
("ramo B, proxy tier only"), il report no.

### 2.5 Cosa il hub ha già a frequenza alta e LazyRay ignora

- **USA, giornaliero (FRED)**: curva (3M/2Y/10Y/30Y, T10Y2Y), TIPS reali
  5Y/10Y, breakeven 5Y/10Y/5Y5Y, OAS IG/BBB/HY/CCC, NFCI, STLFSI, VIX, EFFR,
  TGA, RRP, attivi Fed, dollaro trade-weighted; aste Treasury (bid-to-cover),
  debito in essere.
- **Area euro, giornaliero**: tassi ECB (DFR/MRO/MLF); mensile costo del credito.
- **Mercati, giornaliero**: 29 ETF obbligazionari (TLT, IEF, SHY, **EMB, EMLC**),
  10 FX, 62 azionari, commodity; struttura a termine VIX; CFTC settimanale.
- 10Y mensili per 32 paesi; calendario macro con sorprese (84 eventi).

Questo è l'unico strato dove una cadenza giornaliera ha senso — ed è quello che
Dalio chiama davvero "funding": domanda marginale di bond, condizioni di
credito, risposta della banca centrale.

## 3. Proposta — tre prodotti, tre cadenze, un solo DB

Principio: **la cadenza di calcolo segue la cadenza del dato, e ogni corsa
lascia una riga con la sua data**.

### 3.1 Country risk strutturale (5 motori) → mensile/event-driven, mai giornaliero

1. **Trigger**: ricalcolo quando il log vintage del hub registra revisioni sugli
   input dei motori (giorni di refresh WEO/WDI/WGI/BIS/IIP) e comunque a fine
   mese. Cinque righe al mese invece di 22 identiche.
2. **`ref_date` = data della corsa**, mai fine anno; `INSERT` (non REPLACE):
   nasce la serie storica degli score, l'isteresi comincia a funzionare, il
   report può mostrare **cosa è cambiato rispetto alla corsa precedente** e
   perché (componente per componente: il `components_json` c'è già).
3. **Point-in-time da subito**: `read_macro_panel(asof=run_date)` esiste nel hub
   e il log vintage copre dal 10/07/2026 in poi → `vintage_safe=true` per tutte
   le corse da oggi. Per il pregresso serve l'archivio vintage IMF WEO
   (pubblico, per edizione) — vedi 3.4.
4. **Condizione corrente ≠ traiettoria**: le componenti "livello" usano l'ultimo
   **actual** (ultimo anno chiuso, stime WEO marcate come tali), mai una
   proiezione; le proiezioni restano solo nelle componenti di trend, con il flag
   `forecast_dependent` già esistente esposto nel report. Intestazione: "as of
   <data corsa>, dati fino a <anno>".
5. **Comparabilità**: accanto alla soglia assoluta, percentile sulla **propria
   storia** (≥15 anni disponibili per debito, CA, riserve, inflazione: dal 2000)
   e percentile **entro gruppo** (36 DM / 21 EM / 7 Frontier, già in
   `countries.yaml`). Il bucket finale dichiara quale delle due scale lo guida.
6. **Sovrano gated dall'esterno** per chi non emette valuta di riserva: con
   `fx_debt_share` > 50% o inflazione > 15% il sovrano non può etichettarsi
   "strong"; al massimo "low-debt (FX-constrained)". Rinominare i bucket del
   sovrano in termini descrittivi (low/moderate/high debt burden), non
   valutativi.
7. **Funding spezzato in due, per dato disponibile**: (a) OCSE-32 su dati
   mensili (Δ12m 10Y, term spread vs policy rate, REER) e (b) EM-20 su IDS
   annuale; i 14 senza dati escono dal motore con etichetta esplicita "no
   funding data" (e dal classificatore di ciclo con "n/a per dati", non None).
8. **Report**: digest breve (Telegram) con diff vs corsa precedente + top mover
   con la componente responsabile; HTML completo solo su richiesta o allegato al
   digest mensile; CSV con la data nel nome. Sezione "Limiti" con: non validato,
   proxy tier, date effettive dei dati.

### 3.2 Condizioni di finanziamento e stress di mercato → giornaliero (nuovo)

Il prodotto giornaliero legittimo, costruito su ciò che è già nel hub:

- **USA**: livello/pendenza curva, tassi reali, breakeven, OAS IG/HY/CCC e loro
  variazioni 1m/3m, NFCI/STLFSI, VIX e term structure (contango/backwardation),
  TGA/RRP/attivi Fed (liquidità netta), bid-to-cover delle aste.
- **Euro area**: tassi ECB, 10Y core/periferia (mensile oggi; passare a
  giornaliero via ECB SDW se serve) → spread BTP-Bund.
- **EM aggregato**: EMB/EMLC vs IEF (spread proxy hard/local currency), FX.
- Regimi con l'HMM già in LazyStats (stesso strumento del regime SPY in
  produzione) su un indice composito di stress; **Telegram solo su cambio di
  regime o breach**, altrimenti 5 righe.

Copertura onesta: USA completo, EA parziale, EM solo via ETF. Non si finge un
"funding engine" per 64 paesi.

### 3.3 Regime crescita/inflazione (four-box) → mensile, dalle sorprese

Il v1 non gira. Se si vuole un four-box, la review di luglio aveva già la
ricetta corretta: sorpresa vs atteso (actual vs consenso del calendario del hub,
o vs WEO precedente), non output gap; mensile, sui paesi con calendario coperto.
Da fare solo dopo 3.1 e 3.2.

### 3.4 Validazione (senza questa, il report deve dirsi "non validato")

- Lista episodi: Laeven-Valencia (2018, pubblico) + Reinhart-Rogoff; oggi
  assenti dal repo.
- Vintage storici: archivio WEO per edizione (pubblico dal 2000) → panel
  point-in-time annuale; WDI ha snapshot d'archivio. Questo è l'unico modo di
  fare un backtest prima del 2026-07-10.
- Metrica: quota di crisi segnalate ≥1 anno prima, falsi allarmi, per motore e
  per gruppo paesi; risultato pubblicato nel report anche se mediocre.

## 4. Sequenza consigliata

| Fase | Contenuto | Effetto |
|---|---|---|
| 1 (giorni) | 3.1.1–3.1.4 + 3.1.8: trigger su vintage, `ref_date`=data corsa, INSERT, `asof`, actual-only per i livelli, digest diff | il prodotto smette di mentire su data e novità; nasce la storia; isteresi attiva; niente cambi di metodo |
| 2 (1 settimana) | 3.1.5–3.1.7: doppia scala, gate esterno→sovrano, funding sdoppiato, 14 paesi dichiarati | etichette comparabili e oneste |
| 3 (2 settimane) | 3.2: monitor giornaliero di stress con HMM | il "daily" ha finalmente un contenuto |
| 4 (aperta) | 3.4 backtest con vintage WEO | il report può dichiarare una validità misurata |

Cosa fermare subito, senza costi: la corsa giornaliera del 5-motori e l'invio
quotidiano dell'HTML da 400 KB. Sostituirla con la corsa a trigger di Fase 1 non
richiede modifiche ai motori: cadenza, `ref_date`, modalità di scrittura e
lettura `asof` sono tutte decisioni del runner e della configurazione del job.

## 5. Riferimenti esterni (ricerca 2026-09-04): come altri risolvono gli stessi problemi

| Famiglia | Strumento | Metodo | Dati | Uso per LazyRay |
|---|---|---|---|---|
| DSA | [eu-debt-sustainability-analysis](https://github.com/lennardwelslau/eu-debt-sustainability-analysis) (Welslau/Bruegel, Python) | identità del debito con tasso implicito, quota FX, primario, SFA; 3 stress deterministici; fan chart da shock trimestrali (normale congiunta, winsor 5/95); criterio "debito in calo con prob. 70% a 5 anni" | AMECO, Eurostat, ECB, Ageing Report; Bloomberg solo per aspettative | motore sovrano = probabilità che il debito salga (fan chart su shock annuali 2000–2025 del hub) |
| DSA | [debtkit](https://cran.r-project.org/package=debtkit) (R) | proiezione, decomposizione, fan chart, 6 stress IMF, Bohn, S1/S2 | OECD via readoecd | primitive da portare in Python |
| DSA | countryrisk.io DSA toolkit (commerciale) | IMF/WB-aligned, decomposizione driver, API/MCP | CountryData.io | conferma della forma "DSA come servizio" |
| DSA | IMF SRDSF (2022) | near-term logit a segnali; medio termine fan chart + GFN; realism tools | WEO + paese | struttura di riferimento |
| EWS | Kaminsky-Reinhart (1999); [Sovereign Debt Stress Monitor](https://github.com/The-House-Of-Kgosi/The-Sovereign-Debt-Stress-Monitor) | soglie per indicatore che minimizzano rumore/segnale su 24 mesi pre-crisi; segnale su breach o deterioramento YoY; soglie per gruppo di reddito | WB API (20 ind.), IMF DataMapper, OECD ECA | gemello di LazyRay: stesse soglie importate, nessun backtest |
| EWS | BIS WP421 (Drehmann-Juselius), Aldasoro et al. 2018 | AUC per orizzonte 1–3 anni con requisiti di policy; credit gap a lungo, DSR a breve | BIS trimestrale, 26–44 economie; in EM la crescita del credito batte il gap | metrica di validazione |
| EWS | Laeven-Valencia (IMF WP18/206; IMF ER 2020) | 151 crisi bancarie 1970–2017 + valutarie + sovrane; script Stata pubblici per gli spell | gratuito | etichette per il backtest |
| EWS | ML (SovereignRisk 117 paesi; Egitto XGBoost+SHAP; Jena logit+politica) | logit/RF su panel annuale; predittori: riserve, REER, debito, CA | WB/WEO | conferma che i dati bastano; AUC ~0,8 in-sample |
| EWS | CAUSENTIA | "CI = Stress×Weight/(Absorption+Resilience)", 87% su 15 crisi 2010–25 | WB, FRED, GDELT | non verificabile, campione minimo |
| Stress | ECB CISS (Holló-Kremer-Lo Duca 2012) | 15 indicatori in 5 segmenti, CDF empirica, aggregazione con correlazioni tempo-varianti; 0–1 | giornaliero, ECB Data Portal `CISS.D.U2.Z0Z.4F.EC.SS_CI.IDX` | aggregazione per il monitor giornaliero; serie ufficiale come benchmark |
| Stress | Chicago Fed NFCI/ANFCI; Fed FCI-G | fattore dinamico su 105 serie settimanali; FCI-G pesa 7 variabili per risposta sul PIL | FRED | benchmark |
| Stress | ESM stochastic DSA (2025) | fan chart validati contro episodi di distress → classi di rischio | ESM | validare le code dei fan chart |
| PIT | [macrosynergy](https://github.com/macrosynergy/macrosynergy) (Python, BSD) | QuantamentalDataFrame (real_date, cid, xcat, value, grading); `make_zn_scores` sequenziali; `linear_composite`; `SignalReturnRelations` | JPMaQS a pagamento; il pacchetto lavora su qualunque panel | z-score espansivi senza look-ahead sul nostro panel |
| Compositi | JRC COINr (R) / COIN Tool | handbook OECD/JRC: normalizzazione, pesi, incertezza/sensibilità | — | nessun equivalente Python maturo trovato |

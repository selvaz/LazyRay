# L'elefante nella stanza — copertura dati del framework Dalio (2026-07)

> Companion di [DALIO_METHODOLOGY_REVIEW_2026-07.md](DALIO_METHODOLOGY_REVIEW_2026-07.md).
> Tesi: prima di ritoccare la metodologia va affrontata la **copertura**. Oggi il
> "cuore" del ciclo del debito Dalio (credito privato, DSR, costo del debito) è
> osservato solo su una frazione dei 64 paesi, e diverse etichette poggiano su
> dati assenti o su proiezioni. Sotto: (1) copertura reale misurata sul DB,
> (2) impatto sulle etichette, (3) cosa manca del tutto, (4) piano di sourcing
> **solo gratuito**, verificato a luglio 2026, mappato sui connettori esistenti.

---

## 1. Copertura reale degli input Dalio-core (misurata su `market_data.duckdb`)

64 paesi nel panel. Copertura per indicatore (n. paesi con valore non nullo,
ultimo anno *actual* ≤2026):

| Input Dalio | indicatore | n/64 | ultimo actual | fonte | ruolo nel metodo |
|---|---|---:|---:|---|---|
| **Bubble gauge** | `bis_credit_gap` | **43** | 2025 | BIS | BUBBLE se >+10pp |
| **DSR (top/depression)** | `bis_dsr_private` | **32** | 2025 | BIS | picco DSR = top/depressione |
| Policy rate | `bis_policy_rate` | 54 | 2026 | BIS | proxy costo del debito |
| Debito pubblico/PIL | `public_debt_gdp` | 64 | 2026 | WEO | livello ciclo lungo |
| Saldo fiscale | `fiscal_balance_gdp` | 64 | 2026 | WEO | deterioramento |
| Crescita reale (WEO) | `gdp_growth_weo` | 64 | 2026 | WEO | crescita nominale |
| Inflazione (WEO) | `inflation_avg_weo` | 64 | 2026 | WEO | crescita nominale |
| Credito privato/PIL | `private_credit_gdp` | 64 | 2025 | WDI | leva privata (livello) |
| Real interest rate | `real_interest_rate` | 43 | 2025 | WDI | r vs g |
| **Debito estero** | `total_external_debt_usd` | **20** | 2024 | WDI/IDS | solo debitori (DM esclusi) |
| **Debt service/export** | `debt_service_exports` | **20** | 2024 | WDI/IDS | idem |
| Interessi/entrate | `interest_revenue` | 57 | 2024 | WDI | onere interessi |
| NPL banche | `npl_ratio` | 61 | 2025 | WDI | stress bancario |

**Il buco è il ciclo del debito privato (BIS):** credit gap 43/64 (67%), DSR
**32/64 (50%)**. **21 paesi non hanno NESSUN dato BIS credit_gap/DSR:** ARE, BGD,
BGR, CYP, EGY, EST, HRV, KWT, LTU, LVA, MLT, NGA, PAK, PER, PHL, QAT, ROU, SVK,
SVN, UKR, VNM — cioè proprio molti EM/frontier su cui verte l'analisi di crisi
di Dalio.

## 2. Cosa fa questo alle etichette (attribuzione misurata sul report di oggi)

- **BUBBLE strutturalmente irraggiungibile per 21 paesi**: la fase BUBBLE
  richiede `bis_credit_gap` (nessun fallback). Chi non ha la serie non può mai
  essere BUBBLE, per quanto surriscaldato sia il credito. (Oggi BUBBLE = 0
  paesi, ma è un limite di *misura*, non un dato di realtà.)
- **DEPRESSION degradata a CONTRACTION per 32 paesi**: DEPRESSION richiede DSR al
  picco storico; senza DSR il ramo cade su CONTRACTION. Metà panel non può
  distinguere recessione da crisi da debito.
- **5 etichette LATE_LEVERAGING su 14 (BGR, EST, LTU, ROU, SVK) non hanno alcun
  dato BIS**: sono state assegnate *solo* dalla traiettoria del debito pubblico
  — cioè dallo slope che include le proiezioni WEO. Qui il gap di copertura e il
  problema look-ahead (rev. §2.3) si sommano: l'etichetta di "leveraging" non
  poggia su nessun eccesso di credito osservato, ma su un forecast.
- **Costo del debito appiattito**: `bis_policy_rate` per l'area euro è l'unico
  tasso ECB → Grecia, Spagna, Portogallo, Irlanda, Cipro, Croazia, Slovenia
  hanno tutti 2,25%. Lo spread sovrano — la variabile chiave in una crisi del
  debito — è invisibile (rev. §2.2).

## 3. Cosa manca del tutto (assente dal panel, richiesto da Dalio)

Nessuna di queste serie esiste oggi nel `macro_panel` (verificato: 0 indicatori
yield/reer/gold/dxy/fx-debt):

| Variabile Dalio | perché conta | stato (aggiornato) |
|---|---|---|
| **Rendimento 10Y sovrano** | costo effettivo del debito, "bond vigilantes", r vs g | ✅ **fatto** — FRED IRLTLT01* (32) + IMF `rltir` reale (49), via `v_macro_panel_ext` |
| **Tasso implicito sul debito** | test spirale del debito | ✅ **fatto** — `implied_interest_rate` = IMF `ie` ÷ debito, nella view (60) |
| **REER/NEER** | competitività, deprezzamento nel deleveraging | ✅ **fatto** — BIS `reer_broad` (56) |
| **Oro, indice USD** | dinamica valuta/riserva | ✅ **già presente** — `GLD` (SPDR Gold, prices_daily, giornaliero 2010→oggi) + `SLV`/`PPLT`; USD index `DTWEXBGS` (FRED). Sono serie **globali** lato prezzi, non per-paese → overlay globale, non indicatore panel |
| **Leva corporate (proxy credito privato)** | ciclo del credito dove manca il gap BIS | ✅ **fatto** — IMF `corporate_debt_gdp` (58, solo corporate) |
| **Quota debito in valuta estera + quota detenuta da non residenti** | *"può stampare per uscirne?"* — la distinzione #1 di Dalio | ❌ assente (nessuna API gratuita — Arslanalp–Tsuda XLSX / crawling) |
| **Quote valute di riserva (COFER)** | tesi declino del dollaro / changing world order | ❌ assente (IMF COFER = solo aggregato mondiale) |
| **Bilancio banca centrale /PIL** | monetizzazione = leva del "beautiful deleveraging" | ⚠️ parziale — solo `WALCL` (Fed) lato FRED; cross-country assente |
| **Disuguaglianza (Gini) + polarizzazione politica** | pilastro "changing world order" (conflitto interno) | ❌ assente (WB Gini / WID / V-Dem) |

## 4. Piano di sourcing — solo gratuito, verificato luglio 2026

Mappato sui 4 connettori già funzionanti (FRED, BIS-SDMX, IMF-WEO, WorldBank).

| # | Gap | Fonte | ID/endpoint esatto | Copertura | Freq | Note |
|---|---|---|---|---|---|---|
| 1 | **10Y yield** | FRED (mirror OECD) | `IRLTLT01{ISO2}M156N` es. `IRLTLT01USM156N`; CSV `fredgraph.csv?id=` | ~40 (OECD+major) | M | **Spot-check ok: dati a mag-2026** (US 4,48 / DE 3,05 / JP 2,65). Euro-area fallback ECB `IRS` dataflow. |
| 2 | **REER/NEER** | BIS-SDMX (già usata) | dataflow `WS_EER`; key `M.R.B.{ISO}` (real broad) | **broad = 64** | M | stesso connettore BIS; basket broad combacia col nostro universo |
| 3 | **Tasso implicito sul debito** | IMF WEO (derivare) | interessi%PIL = `GGXONLB_NGDP` − `GGXCNL_NGDP`; ÷ `GGXWDG_NGDP` | ~190 | A | **zero nuovi dati**: `pb`(60) e `fiscal_balance`(64) già nel panel; WEO non ha una riga interessi diretta |
| 4 | **Oro / USD index** | FRED | oro: WB Pink Sheet `GOLD` o LBMA CSV (**vecchie ID FRED gold dismesse ~2021**); USD: `DTWEXBGS` (broad), `DTWEXEMEGS` (EM) | globale | D/M | DXY ICE è proprietario → usare indici Fed trade-weighted |
| 5 | **COFER quote riserve** | IMF-SDMX | dataset `IMF.STA:COFER` | **solo aggregato mondiale** (no breakdown paese) | Q | riservato a livello paese; ottieni solo quote USD/EUR/CNY/… globali |
| 6 | **Bilancio CB /PIL** | FRED (major) + IMF IFS | `WALCL`,`ECBASSETSW`,`JPNASSETS`; cross-country IFS "Monetary Authorities: Total Assets" | major puliti, IFS irregolare | W/M | nessuna serie 64-paesi pulita; dividere per PIL nominale |
| 7 | **Debito in FX / quota non residenti** | Arslanalp–Tsuda + WB IDS | XLSX free da IMF WP/12/284 (AE) e WP/14/39 (EM); IDS solo LIC/MIC | AT ~24 AE + 24 EM | Q/A | **il gap più difficile: nessuna API live gratuita** → ingest manuale, aggiorna ~annuale |
| 8 | **Gini / polarizzazione** | WB + WID + V-Dem | Gini `SI.POV.GINI` (WDI); WID bulk download; V-Dem `v2cacamps` | Gini ~160 / V-Dem ~180 | A | Gini con lag pluriennale → forward-fill |

## 5. Sequenza raccomandata (impatto ÷ costo)

**Ondata 1 — riusa i connettori esistenti, sblocca subito la metodologia**
1. **#3 Tasso implicito sul debito** — *zero nuovi dati*, deriva da WEO già
   scaricato. Sostituisce `nom_rate = policy rate` (fix rev. §2.2) per ~60 paesi
   e disaccoppia l'area euro. **Fai questo per primo.**
2. **#1 Rendimento 10Y (FRED `IRLTLT01*`)** — pattern FRED banale, serie vive.
   Costo effettivo del debito osservato per ~40 paesi; migliora r vs g e il test
   beautiful/ugly. Fallback ECB per euro-area.
3. **#2 REER (BIS `WS_EER` broad)** — stesso connettore BIS, copertura 64/64.
   Aggiunge la leva del deprezzamento nel deleveraging.

**Ondata 2 — nuovo dataset ma connettore vicino**
4. **#4 Oro + USD index** (FRED/WB Pink Sheet) e **#5 COFER** (IMF-SDMX):
   abilitano il filone "reserve currency / changing world order" (solo a livello
   globale per COFER — dichiararlo).
5. **#8 Gini + V-Dem polarizzazione**: pilastro conflitto interno.

**Progetto a parte — alto valore, alto attrito**
6. **#7 debito in FX / quota non residenti** (Arslanalp–Tsuda XLSX + IDS):
   analiticamente *la* variabile di Dalio ("stampare per uscirne"), ma niente
   API gratuita → ingest manuale, copertura parziale. Trattare come mini-progetto.

**Non colmabile gratis a copertura piena:** DSR pubblico (esiste solo DSR privato
BIS); quota debito FX/non residenti per tutti i 64 (solo ~48 via AT). Vanno
dichiarati come limiti, non aggirati con proxy silenziosi.

---

### Nota trasversale sui confini di copertura
Diversi indicatori sono **developing-only by design** (WDI IDS: debito estero,
debt service → 20 paesi, DM esclusi) o **BIS-only** (credit gap/DSR → economie
segnalanti BIS). Non è un bug del pipeline: è la frontiera delle fonti gratuite.
La conseguenza metodologica (rev. §2.6) è che **il confronto cross-country e lo
z-score compensano dati mancanti in modo non uniforme** — ragione in più per
marcare, nel report, quali fasi/score poggiano su copertura piena e quali no.

*Fonti verificate: FRED (IRLTLT01*, DTWEXBGS), BIS Data Portal (WS_EER),
IMF WEO/COFER/IFS SDMX, World Bank WDI/IDS, Arslanalp–Tsuda IMF WP/12/284 &
WP/14/39, WID.world, V-Dem. Spot-check freschezza rendimenti FRED eseguito
2026-07-08.*

---

## 6. Inventario completo di copertura & staleness (misurato 2026-07-08)

Su tutti i 70 indicatori del panel (n. paesi con dato, ultimo anno *actual* ≤2026).

### 6a. Buchi di copertura (indicatori che non coprono i 64)
| Gruppo | Indicatori | n/64 | Chi manca | Fonte migliore |
|---|---|---:|---|---|
| **Debito estero** | total_external_debt, external_debt_gni, ppg_external_debt, short_term_debt (×2), debt_service_exports | **20** | i 44 DM/high-income (WDI IDS è solo-debitori) | IMF IIP/BOP, Eurostat, nazionale |
| Tassi reali/lending | real_interest_rate, lending_interest_rate | 43 | 21, quasi tutti area euro | ECB / IMF IFS |
| Broad money | broad_money_gdp | 44 | 20 | IMF IFS (DataMapper vuoto: 2/64) |
| Policy rate | bis_policy_rate | 54 | 10 EM | IMF IFS / nazionale |
| Fiscali WEO | primary_balance, revenue, expenditure, unemployment_weo | 60 | 4 Golfo (ARE,KWT,QAT,BGD) | nazionale |
| BIS DSR | bis_dsr_private | 32 | 32 | non pubblicato → calcolare |
| BIS credit gap | bis_credit_gap | 43 | 21 | non pubblicato → calcolare |

### 6b. Staleness (ultimo dato actual)
| Indicatore | Ultimo | Note |
|---|---|---|
| tourism_receipts / tourism_exports | **2020** | 6 anni — WDI fermo |
| natural_resource_rents | **2021** | 5 anni |
| WGI governance (×6), debito estero (×6), fiscali, remittances | 2024 | lag ~2y |
| ~30 serie WDI | 2025 | lag 1y normale |
| WEO (crescita/inflazione/debito/fiscale) | 2026 (+fcst 2031) | fresche ✓ |

### 6c. Probe IMF DataMapper (connettore ESISTENTE) sui buchi — cosa riempie davvero
| Codice DataMapper | Cosa | n/64 | Ultimo | Riempie i mancanti? | Verdetto |
|---|---|---:|---|---|---|
| `ie` | Interessi su debito pubblico %PIL | **60** | 2024 | — | ✅ tasso implicito diretto (meglio del derivato) |
| `rltir` | Rendimento reale LT sovrano | **49** | 2024 | **11/13** dei paesi EM che FRED non ha | ✅ completa i rendimenti EM |
| `NFC_LS` | Debito corporate (loans+securities) %PIL | **58** | 2024 | **15/21** senza credit-gap BIS | ⚠️ solo *corporate* (≠ credito privato totale BIS) → base per calcolare un gap, non backfill diretto |
| `NFC_ALL` | Debito corporate (all instruments) | 41 | 2024 | 10/21 | ⚠️ idem |
| `Reserves_STD` | Riserve/debito breve | 28 | 2025 | — | parziale |
| `EREER`/`ENEER` | REER/NEER IMF | **2** | — | 1/8 | ❌ vuoto — BIS WS_EER (56) resta superiore |
| `FMB_GDP` | Broad money %PIL | **2** | — | 0/20 | ❌ vuoto in DataMapper |
| `DG_GDP` | Debito estero %PIL | **2** | — | 0/15 | ❌ vuoto — serve IMF IIP/BOP |

**Conclusione probe:** col connettore IMF DataMapper già esistente si riempiono 3 buchi ad alto valore
(`ie` interessi, `rltir` rendimento reale EM, `NFC_LS` debito corporate) — zero nuova infrastruttura.
Restano fuori: debito estero DM, broad money, e gli ultimi REER EM → richiedono IMF IFS-proper (SDMX),
IMF IIP/BOP, Eurostat o crawling nazionale. **Caveat comparabilità:** `NFC_LS` è corporate, non credito
privato totale — va tenuto come indicatore separato e usato per *calcolare* un gap, non per backfillare
`bis_credit_gap` (romperebbe lo z-score).

---

## 7. Fonti ufficiali per i gap residui — RICERCATE e verificate (2026-07-08)

Dopo aver letto la documentazione (non a tentativi): i gap residui si dividono per **regione**, con due API SDMX ufficiali diverse.

### 7a. IMF SDMX 3.0 — `https://api.imf.org/external/sdmx/3.0`
- Data: `/data/dataflow/IMF.STA/{FLOW}/+/{KEY}?startPeriod=YYYY`, header `Accept: application/vnd.sdmx.data+csv`. Struttura: `/structure/dataflow/IMF.STA/{FLOW}/+?references=all`.
- COUNTRY = **ISO3** (nessuna transcodifica). **NIENTE wildcard** → chiave completa + loop per-paese. Chiave = ordine dimensioni del DSD.
- Ordini-chiave: MFS_IR `COUNTRY.INDICATOR.FREQ`; MFS_MA `COUNTRY.INDICATOR.UNIT.FREQ`; MFS_CBS/ODC `…​.TYPE_OF_TRANSFORMATION.FREQ`; IIP `COUNTRY.BOP_ACCOUNTING_ENTRY.INDICATOR.UNIT.FREQ`; IRFCL `COUNTRY.INDICATOR.SECTOR.FREQ`.
- **Codici confermati (pull reale):** policy rate `MFS166_RT_PT_A_PT` (`{ISO3}.MFS166_RT_PT_A_PT.M`) → riempie **5 EM**: BGD, BGR, EGY, NGA, QAT (freschi 2024-26). Broad money `BM_MAI` (`{ISO3}.BM_MAI.XDC.M`) → non-euro EM (POL, IDN, BRA…), **livello in valuta locale** (serve ÷GDP per %PIL).
- **Limite verificato:** i paesi **euro NON riportano all'IMF** i tassi/monetari (DEU, FRA, ESP, GRC → vuoti). Lending `MFS162`/deposit `MFS135` resi solo per FRA(2017)/TUR. UKR assente a ogni frequenza. → per l'euro-area l'IMF **non serve**.

### 7b. ECB Data Portal — `https://data-api.ecb.europa.eu/service/data` (SDMX 2.1)
- Data: `/data/{FLOW}/{KEY}?startPeriod=YYYY-MM&format=csvdata`. **Wildcard funzionano** (posizione vuota = tutti) → un'unica chiamata per serie, CSV con metadati ricchi.
- `MIR` = tassi bancari MFI **per-paese** (chiave `M.{REF_AREA}.B.{BS_ITEM}.{MAT}.{DATA_TYPE}.A.{SECTOR}.EUR.N`). Verificato: `M..B.L22.H.R.A.2250.EUR.N` restituisce AT BE BG DE EE ES FI FR GR HR IE IT LT LU LV MT NL PT SI SK (+U2 aggregato).
- **Copre 15/16 dei nostri euro-mancanti** per lending/deposit (solo CY fuori da quella serie specifica) + bonus non-euro EU (BG, HR). `BSI` = aggregati monetari (M3) se servono.
- **È più semplice dell'IMF** (wildcard → una call/serie), quindi il connettore ECB è il primo da costruire.

### Conclusione (ricercata, non indovinata)
| Gap | Fonte ufficiale giusta | Come |
|---|---|---|
| Lending/deposit euro-area (~16 paesi) | **ECB MIR** | 1 wildcard call, 15/16 coperti |
| Broad money euro-area | ECB BSI (ma M3 nazionale in unione monetaria è concettualmente debole) | — |
| Policy rate 5 EM | IMF `MFS166` | loop per-paese |
| Broad money EM non-euro | IMF `BM_MAI.XDC` | livello → ÷GDP |
| FX-debt / non-residenti | Arslanalp–Tsuda | manuale, no API |

**Un nuovo connettore `sources/ecb.py`** (SDMX 2.1, wildcard, CSV) copre il grosso dei gap-tasso euro-area ufficialmente; l'IMF SDMX resta per i ~5 EM.

---

## 8. REPORT COPERTURA AGGIORNATO (2026-07-08) — stato consolidato

Dopo la sessione: costruito il connettore **ECB** (`sources/ecb.py`, SDMX 2.1 wildcard),
aggiunti indicatori IMF via DataMapper, bridge FRED via view. Tutto **staged su pillar
`markets` (peso 0)** → composite/fasi invariati (verificato: 0 diff). Copertura **verificata
con pull reali**.

### 8a. WIRED questa sessione (config + connettori esistenti/nuovo) — caricano al prossimo `run_daily`
| Indicatore | Fonte | Copertura verificata | Freschezza |
|---|---|---:|---|
| `bond_yield_10y` (nominale) | FRED `IRLTLT01*` (via view) | 32/64 | 2026-05 |
| `real_long_rate` (reale) | IMF `rltir` | 49/64 | 2024 |
| `interest_on_debt_gdp` | IMF `ie` | 60/64 | 2024 |
| `implied_interest_rate` | derivato `ie`÷debito (view) | 60/64 | 2024 |
| `reer_broad` | BIS `WS_EER` | 56/64 | 2025 |
| `corporate_debt_gdp` | IMF `NFC_LS` | 58/64 | 2024 |
| `ecb_cost_borrow_nfc` / `_house` | **ECB `MIR`** (nuovo connettore) | 21/64 (tutti UE) | **2026-05** |

### 8b. VERIFICATO disponibile (ufficiale) ma NON ancora wired — pronto, serve solo aggiungerlo
| Cosa | Fonte ufficiale | Copertura verificata | Come |
|---|---|---:|---|
| **Credito privato totale** (→ calcolare credit-gap EM) | IMF GDD `PVD_LS` | **64/64** | connettore IMF esistente (config) |
| Debito famiglie | IMF `HH_LS` | 58/64 | config |
| Debito pubblico **netto** (caveat SGP/NOR) | IMF `GGXWDN` | 47/64 (fresco 2026) | config |
| **Disuguaglianza (Gini)** | WB `SI.POV.GINI` | **59/64** (2025) | connettore WB esistente (config) |
| Policy rate EM | IMF `MFS166` (SDMX) | 5 EM (BGD,BGR,EGY,NGA,QAT) | serve adattatore SDMX |
| Broad money EM non-euro | IMF `BM_MAI.XDC` (SDMX) | POL/IDN/BRA… (livello→÷PIL) | serve adattatore SDMX |
| **Debito estero DM** | IMF `IIP` (SDMX) | da mappare | serve adattatore SDMX |
| **Bilancio banca centrale** | IMF `MFS_CBS` (SDMX) | da mappare | serve adattatore SDMX |

### 8c. VERIFICATO — assente o senza fonte pulita gratuita
| Cosa | Verdetto verificato |
|---|---|
| **Debito in valuta estera / non-residenti** (Dalio #1) | ⬆️ **CORREZIONE**: esiste `IMF IIPCC` (composizione valutaria dell'IIP: quote USD/EUR/valuta estera di attività e **passività**) — non più "solo crawling". Ma chiave SDMX + copertura da mappare (dataset specialistico). Quota **non-residenti** resta Arslanalp–Tsuda (no API). |
| **COFER per-paese** | ❌ confermato: 147 reporter ma **riservato** → solo aggregato mondiale (quote USD/EUR/CNY globali) |
| **Oro / USD index** | ✅ già presenti lato prezzi (`GLD`, `DTWEXBGS`) — overlay globale |

### 8d. Correzioni al mio inventario iniziale (§3) — cose che credevo mancanti ma c'erano/ci sono
- **Oro, USD index**: già lato prezzi.
- **Disuguaglianza (Gini)**: 59/64 via WB esistente.
- **FX-denominated debt**: esiste IMF IIPCC ufficiale (non solo crawling).
→ Il "progetto da 60 crawler" si riduce a: **quota debito detenuta da non-residenti** (Arslanalp–Tsuda, manuale). Tutto il resto è API ufficiale (IMF/ECB/BIS/WB).

### 8e. Prossimi passi (ordine di valore)
1. **Config-only, subito**: `PVD_LS` (64, sblocca il credit-gap EM), `HH_LS`, `GGXWDN` (net debt), `gini` (WB).
2. **Adattatore SDMX IMF** (`sources/imf_sdmx.py`): policy-rate EM, broad money, `IIP` (debito estero DM), `MFS_CBS` (bilancio CB). Loop per-paese, riusa `imf`/`iso3`.
3. **Mappare IIPCC** per il gap FX-denominated (dataset specialistico, sessione dedicata).
4. **Metodologia**: cablare gli input `markets` nello scoring / test beautiful-ugly → *lì* le etichette cambiano (con diff prima/dopo).

---

## 9. Aggiunte sessione 2 — adattatore SDMX, scoring, ingest manuale

**#4 — Adattatore IMF SDMX 3.0** (`sources/imf_sdmx.py`): parla il servizio ufficiale
`api.imf.org/external/sdmx/3.0` (MFS/IIP/IRFCL/IIPCC), ISO3, per-paese (niente wildcard).
Cablato in router+validator. Primo indicatore live: `imf_policy_rate` (MFS166) → riempie
i **5 EM** senza BIS (BGD, BGR, EGY, NGA, QAT), verificato con pull reale. IIP (debito
estero DM) / MFS_CBS (bilancio CB) / IIPCC (debito in FX) **non ancora mappati** — le chiavi
multi-dimensione non si indovinano, servono i codelist letti in una sessione dedicata;
l'adattatore le accetta via config appena mappate.

**#3 — Disuguaglianza cablata nello scoring**: `gini` spostato su un nuovo pilastro
**`social` pesato 5** (Dalio: conflitto interno / changing-world-order). È l'unica
dimensione *genuinamente nuova* — gli altri indicatori `markets` (tassi/debito) sono
ridondanti coi pilastri esistenti e restano staged per non fare **double-counting**.
Effetto misurato (diff before/after su dati veri): **37/64 composite cambiano** — scendono
i paesi ad alta disuguaglianza (Sudafrica −0,13, Colombia −0,13, Brasile −0,10, USA −0,07),
salgono gli egualitari (Slovacchia +0,08, Slovenia/Cechia/Olanda +0,06/0,07). Report JS
aggiornato (`PILLAR_W` include `social`).

**#5 — Non-residenti (Arslanalp–Tsuda)**: verificato che **non esiste API** (solo XLSX
periodico). Creato `import_investor_base.py` — scaffold di ingest **manuale** documentato
(scarica l'XLSX AT → `nonresident_debt_share` nel panel, staged). Il mapping colonne va
verificato sul file reale (il layout cambia tra vintage); è un template onesto, non codice
finto-testato. La **quota non-residenti** resta l'unico vero gap senza fonte automatizzabile.

---

## 10. Chiavi IMF SDMX mappate (deterministicamente, wildcard `*`)

Trovate leggendo i metadati reali (wildcard `*`, non i tentativi), verificate con pull:

| Concetto | Chiave `{iso3}.…` | Copertura | Note |
|---|---|---:|---|
| Net IIP (posizione estera netta) | `NETAL_P.NIIP.USD.A` (IIP) | ampia DM+EM | USD (÷PIL per %) |
| Passività estere lorde | `L_P.IIP.USD.A` (IIP) | ampia | USD |
| Debito verso non-residenti | `L_P.DLNRES_DIC._T.USD.A` (IIPCC) | 19 | USD |
| **Debito in valuta estera** | `L_P.DLNRES_DIC.FC.USD.A` (IIPCC) | 19 | → `fx_debt_share` (view) |
| Attivo banca centrale | `S121_A_TA_ASEC_CB1SR.USD.M` (MFS_CBS) | **solo USA** | thin, non aggiunto |

**`fx_debt_share`** (view, FC/totale×100) = il segnale #1 di Dalio, verificato: USA 8, IT 5,
DE 14, ES 11 (emittenti valuta di riserva → possono stampare) vs TUR 94, HRV 94, HUN 84,
IDN 80 (dipendenti da valuta estera → vulnerabili). **Fonte ufficiale IMF IIPCC → supera
l'ingest manuale Arslanalp–Tsuda** per i 19 paesi coperti (lo scaffold resta per gli altri).
MFS_CBS (bilancio CB) è thin (solo USA) — la chiave c'è, il dato no.

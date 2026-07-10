# Revisione metodologica del report macro ispirato a Ray Dalio

> **Nota di provenienza:** documento ricevuto da ChatGPT (upload utente, 2026-07-09),
> salvato qui verbatim come materiale di riferimento. È la proposta di
> ri-architettura a 5 motori discussa in
> [DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md](DALIO_5ENGINE_IMPLEMENTATION_PLAN_2026-07.md),
> che ne verifica la fattibilità dati contro il repo reale e ne deriva un piano
> di implementazione a fasi. Leggere quel documento per il piano azionabile;
> questo è il testo sorgente non modificato.

**Data:** 2026-07-08
**Oggetto:** come modificare il report esistente usando solo fonti dati pubbliche
**Tesi centrale:** il report attuale va mantenuto come livello visuale, ma il motore va rifatto attorno a sostenibilità del debito, funding, domanda marginale di bond, valuta e risposta della banca centrale.

---

## 1. Verdetto sintetico

Il report attuale è utile come dashboard macro comparativa, ma non è abbastanza vicino alla logica operativa di Ray Dalio.

Il punto debole non è il lessico. Il report usa correttamente parole come *long-term debt cycle*, *beautiful deleveraging*, *ugly deleveraging*, *credit gap*, *growth/inflation quadrant*. Il problema è che trasforma una logica causale in una tassonomia a soglie.

L'approccio corretto dovrebbe passare da:

```text
fase = funzione(debito/PIL, crescita reale, inflazione, credit gap, policy rate)
```

a:

```text
fase = funzione(
    debt service,
    entrate fiscali,
    gross financing needs,
    domanda per il debito,
    costo effettivo di rifinanziamento,
    crescita nominale,
    banca centrale,
    valuta,
    vincolo esterno,
    stabilità politica
)
```

La classificazione per fase resta utile, ma deve diventare un output finale, non il cuore del modello.

---

## 2. Perché l'attuale approccio è incompleto

Il report esistente usa indicatori come:

- debito pubblico/PIL;
- pendenza del debito/PIL;
- crescita reale e inflazione;
- policy rate;
- credit-to-GDP gap;
- debt service ratio privato;
- current account;
- saldo fiscale;
- pillar score normalizzati cross-country.

Questo produce una buona vista comparativa, ma ha quattro problemi.

### 2.1 Debito/PIL non basta

Il debito/PIL è necessario, ma non sufficiente. Due paesi con lo stesso debito/PIL possono avere rischi completamente diversi se:

- uno ha lunga maturity e investitori domestici stabili;
- l'altro deve rifinanziare grandi importi nei prossimi 12-24 mesi;
- uno ha debito in valuta propria;
- l'altro ha debito in dollari o euro;
- uno ha banca centrale credibile;
- l'altro deve difendere un cambio semi-fisso;
- uno ha asset pubblici finanziari;
- l'altro ha solo passività lorde.

Il caso tipico è Singapore: debito lordo alto, ma bilancio pubblico sostanzialmente asset-backed. Un classificatore basato sul debito lordo rischia di leggere "late long-cycle" dove la fragilità sovrana effettiva è bassa.

### 2.2 Il policy rate non è il costo del debito

Usare il policy rate come proxy del costo del debito è una scorciatoia.

Il costo rilevante è:

```text
costo effettivo del debito pubblico = interessi pagati / stock di debito
```

e, in chiave prospettica:

```text
costo marginale di rifinanziamento = rendimento di mercato sulle nuove emissioni
```

Il policy rate può essere molto distante dal costo del debito, soprattutto quando:

- la curva è ripida o invertita;
- il debito ha duration lunga;
- il rischio sovrano è prezzato nei rendimenti lunghi;
- il paese ha debito in valuta estera;
- la banca centrale tiene il tasso breve artificialmente basso;
- il mercato richiede premio di inflazione o premio fiscale.

### 2.3 "Beautiful deleveraging" è troppo permissivo

Nel report attuale una situazione tende a diventare "beautiful deleveraging" quando:

```text
crescita nominale > tasso nominale
e debito/PIL scende
```

Questa condizione è utile, ma non sufficiente.

Una riduzione del debito/PIL può avvenire per ragioni molto diverse:

1. crescita reale solida;
2. inflazione moderata;
3. repressione finanziaria;
4. svalutazione della valuta;
5. default o restructuring;
6. manipolazione statistica;
7. collasso del denominatore reale seguito da rebound nominale;
8. erosione inflazionistica dei debiti domestici.

Solo il primo e il secondo caso sono davvero coerenti con un deleveraging "bello". Gli altri sono forme di deleveraging coercitivo, inflazionistico o traumatico.

### 2.4 Manca il cuore del Big Debt Cycle: chi compra il debito

Nel framework di Dalio il problema non è solo "quanto debito esiste". Il problema è se il debito può essere collocato senza:

- tassi molto più alti;
- monetizzazione crescente;
- svalutazione della valuta;
- perdita di fiducia nei bond;
- fuga verso hard assets;
- compressione della spesa pubblica;
- instabilità politica.

Il motore deve quindi misurare l'equilibrio tra offerta di debito e domanda per quel debito.

---

## 3. Nuova architettura del modello

La nuova architettura dovrebbe avere cinque motori separati.

```text
1. Sovereign Solvency Engine
2. Funding Liquidity Engine
3. Private Credit Cycle Engine
4. External Currency Constraint Engine
5. Political Execution Engine
```

Questi cinque motori alimentano un classificatore finale:

```text
Dalio Cycle Stage =
combinazione pesata di:
- sostenibilità fiscale
- rischio di funding
- ciclo del credito privato
- vincolo valutario/esterno
- capacità politica di aggiustamento
- risposta monetaria probabile
```

La dashboard finale non deve produrre un solo score aggregato, ma una diagnosi in più dimensioni.

---

## 4. Fonti dati pubbliche

### 4.1 IMF WEO

Fonte: [IMF World Economic Outlook Databases](https://www.imf.org/en/publications/sprolls/world-economic-outlook-databases)
Uso: crescita reale, inflazione, saldo fiscale, debito pubblico, current account, PIL nominale, proiezioni fino all'orizzonte WEO.

Variabili da usare:

| Area | Variabile |
|---|---|
| Crescita | real GDP growth |
| Prezzi | CPI inflation average/end-period |
| Fiscale | general government gross debt |
| Fiscale | general government net lending/borrowing |
| Estero | current account balance |
| Dimensione | GDP nominale in valuta locale e USD |

Ruolo nel modello:

```text
macro_base = crescita reale + inflazione + fiscal balance + debt/GDP + current account
```

Il WEO è buono per la base macro e le proiezioni, ma non basta per il funding.

### 4.2 IMF Fiscal Monitor / DataMapper

Fonte: [IMF DataMapper](https://www.imf.org/external/datamapper/datasets/weo)
Uso: indicatori fiscali selezionati e proiezioni multi-paese.

Da usare per:

- saldo primario;
- interessi quando disponibili;
- debito lordo;
- fabbisogno fiscale;
- confronto tra economie avanzate, emergenti e low-income.

### 4.3 BIS credit data

Fonti:

- [BIS Credit-to-GDP gaps](https://data.bis.org/topics/CREDIT_GAPS)
- [BIS Debt Service Ratios](https://data.bis.org/topics/DSR)
- [BIS Credit to the non-financial sector](https://www.bis.org/statistics/totcredit.htm)

Uso:

| Indicatore | Significato |
|---|---|
| credit-to-GDP gap | eccesso di credito rispetto al trend |
| DSR famiglie | quota reddito usata per servire debito household |
| DSR imprese | quota reddito usata per servire debito corporate |
| DSR privato totale | stress aggregato del settore privato |
| credito/PIL | stock di leva privata |

Ruolo nel modello:

```text
private_credit_cycle =
credit_gap
+ DSR_private
+ real_credit_growth
+ property_price_gap se disponibile
```

### 4.4 World Bank International Debt Statistics

Fonte: [World Bank International Debt Statistics](https://www.worldbank.org/en/programs/debt-statistics/ids)
Uso: debito estero, debt service estero, composizione per creditori, flussi e stock.

Variabili:

| Indicatore | Uso |
|---|---|
| external debt stock / GNI | leva esterna |
| short-term external debt / reserves | rischio rollover esterno |
| external debt service / exports | stress valutario |
| public and publicly guaranteed external debt | rischio sovrano esterno |
| private nonguaranteed debt | rischio corporate esterno |
| concessional vs private creditors | qualità del funding |

Ruolo:

```text
external_debt_stress =
external_debt_service_exports
+ short_term_external_debt_reserves
+ external_debt_gni
+ FX_depreciation_pressure
```

### 4.5 IMF CPIS / Portfolio Investment Positions

Fonti:

- [IMF CPIS securities statistics](https://www.imf.org/en/data/statistics/working-group-on-securities-databases/data-collection-on-securities-statistics)
- [IMF CPIS via World Bank Data360](https://data360.worldbank.org/en/dataset/IMF_CPIS)

Uso: stock di investimenti di portafoglio cross-border in debt securities ed equity.

Variabili:

| Indicatore | Uso |
|---|---|
| non-resident holdings of debt securities | dipendenza da funding estero |
| creditor concentration | rischio di fuga da pochi investitori |
| currency/instrument split se disponibile | qualità del funding |
| variazione annuale holdings esteri | domanda marginale |

Ruolo:

```text
foreign_buyer_dependency =
foreign_holdings_government_debt / total_government_debt
+ concentration_of_foreign_holders
- stability_of_holder_base
```

### 4.6 U.S. Treasury TIC

Fonte: [Treasury International Capital System](https://home.treasury.gov/data/treasury-international-capital-tic-system)
Fonte specifica: [Major Foreign Holders of Treasury Securities](https://www.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.html)

Uso: per gli Stati Uniti, misura detentori esteri e flussi su Treasury e altri securities.

Indicatori:

| Indicatore | Uso |
|---|---|
| foreign holdings of Treasuries | base estera di domanda |
| official vs private flows | chi compra |
| variazione holdings per paese | concentrazione geopolitica |
| net TIC flows | pressione di funding esterno |

Questo è cruciale per una lettura Dalio degli USA.

### 4.7 ECB SHSS

Fonte: [ECB Securities Holdings Statistics](https://data.ecb.europa.eu/methodology/securities-holdings-statistics)
Uso: per area euro, detentori di securities per settore, paese e strumento.

Indicatori:

| Indicatore | Uso |
|---|---|
| holdings di titoli governativi per settore | chi assorbe debito |
| banche domestiche vs esteri | stabilità del buyer base |
| assicurazioni/fondi pensione | domanda strutturale |
| Eurosystem holdings | monetizzazione / backstop |
| esposizione cross-border intra-eurozona | rischio frammentazione |

### 4.8 IMF COFER

Fonte: [IMF COFER](https://data.imf.org/en/datasets/IMF.STA%3ACOFER)
Uso: composizione valutaria delle riserve ufficiali aggregate.

Ruolo:

- contesto per valuta di riserva;
- trend del dollaro/euro/yen/yuan nelle riserve;
- analisi sistemica, più che paese-per-paese.

Limite: COFER non sempre consente una lettura completa paese-per-paese.

### 4.9 World Bank WGI

Fonte: [Worldwide Governance Indicators](https://www.worldbank.org/en/publication/worldwide-governance-indicators)
Uso: capacità politica e istituzionale di gestire aggiustamenti fiscali.

Dimensioni:

| Dimensione | Uso |
|---|---|
| Voice and Accountability | rischio politico/sociale |
| Political Stability | rischio rottura politica |
| Government Effectiveness | capacità di implementazione |
| Regulatory Quality | qualità del policy framework |
| Rule of Law | credibilità istituzionale |
| Control of Corruption | qualità del bilancio pubblico |

Ruolo:

```text
political_execution_score =
government_effectiveness
+ rule_of_law
+ control_of_corruption
+ political_stability
```

---

## 5. Motore 1 — Sovereign Solvency Engine

### 5.1 Obiettivo

Misurare se lo Stato può sostenere il suo debito senza dover ricorrere a:

- default;
- repressione finanziaria estrema;
- inflazione elevata;
- monetizzazione persistente;
- austerità politicamente destabilizzante.

### 5.2 Variabili principali

| Variabile | Fonte | Direzione rischio |
|---|---|---:|
| debito lordo/PIL | IMF WEO | alta = peggio |
| debito netto/PIL | IMF/OECD/Eurostat dove disponibile | alta = peggio |
| interessi/entrate | IMF/OECD/World Bank | alta = peggio |
| interessi/PIL | IMF/OECD | alta = peggio |
| saldo primario/PIL | IMF | basso = peggio |
| saldo fiscale/PIL | IMF WEO | basso = peggio |
| crescita nominale | IMF WEO | bassa = peggio |
| costo effettivo debito | calcolato | alto = peggio |
| r - g | calcolato | alto = peggio |

### 5.3 Formula r - g

```text
g_nominale = crescita_reale + inflazione + interazione
```

Approssimazione semplice:

```text
g_nominale ≈ crescita_reale + inflazione
```

Formula corretta:

```text
g_nominale = (1 + crescita_reale) * (1 + inflazione) - 1
```

Costo effettivo:

```text
r_effettivo = interessi_pagati / debito_pubblico_medio
```

Spread dinamico:

```text
r_minus_g = r_effettivo - g_nominale
```

### 5.4 Debt dynamics

La dinamica base del debito pubblico:

```text
Δd ≈ primary_deficit + (r - g) * d
```

dove:

```text
d = debito/PIL
```

Interpretazione:

| Stato | Diagnosi |
|---|---|
| r < g, saldo primario vicino a equilibrio | stabilizzazione naturale |
| r < g, deficit primario alto | stabilità fragile |
| r > g, saldo primario positivo | aggiustamento possibile |
| r > g, deficit primario negativo | rischio spirale |
| r > g, funding stress crescente | fase avanzata del ciclo del debito |

### 5.5 Output

```text
Sovereign Solvency Score =
  z(debt_gdp)
+ z(net_debt_gdp)
+ z(interest_revenue)
+ z(interest_gdp)
+ z(r_minus_g)
+ z(primary_deficit_gdp)
+ z(debt_trend_5y)
```

Con segno invertito per indicare rischio.

Output leggibile:

| Score | Etichetta |
|---:|---|
| 0-20 | strong |
| 20-40 | stable |
| 40-60 | watch |
| 60-80 | stressed |
| 80-100 | critical |

---

## 6. Motore 2 — Funding Liquidity Engine

### 6.1 Obiettivo

Misurare se il paese riesce a collocare il debito necessario senza destabilizzare tassi, valuta o banca centrale.

Questo è il motore più importante da aggiungere.

### 6.2 Variabili

| Variabile | Fonte | Uso |
|---|---|---|
| gross financing needs/PIL | IMF/OECD/DMO nazionali | fabbisogno annuale |
| debito in scadenza entro 12 mesi | DMO/OECD | rollover risk |
| maturity media | DMO/OECD | sensibilità ai tassi |
| quota debito detenuta da esteri | CPIS/TIC/ECB/OECD | dipendenza estera |
| quota detenuta da banca centrale | banche centrali/ECB | monetizzazione/backstop |
| bid-to-cover aste | DMO nazionali | domanda marginale |
| auction tail | DMO nazionali | stress di collocamento |
| rendimento 10Y | banche centrali/FRED/ECB | costo marginale |
| spread sovrano | mercato/FRED/ECB | rischio percepito |

### 6.3 Formula

```text
Funding Stress Score =
  w1 * z(gross_financing_needs_gdp)
+ w2 * z(short_term_maturity_share)
+ w3 * z(foreign_holder_share)
+ w4 * z(change_in_foreign_holdings)
+ w5 * z(ten_year_yield_change)
+ w6 * z(auction_tail)
- w7 * z(domestic_stable_holder_share)
- w8 * z(central_bank_absorption_capacity)
```

Pesi iniziali:

| Variabile | Peso |
|---|---:|
| GFN/PIL | 25 |
| debito in scadenza breve | 15 |
| quota estera | 15 |
| variazione domanda estera | 15 |
| rendimento 10Y / spread | 15 |
| metriche aste | 10 |
| base domestica stabile | -10 |
| assorbimento banca centrale | dipende dal regime |

Nota: l'assorbimento della banca centrale non è automaticamente positivo. Nel breve può ridurre il rischio di funding; nel medio può aumentare rischio valuta/inflazione.

### 6.4 Classi

| Stato | Condizioni tipiche |
|---|---|
| Easy funding | GFN basso, domanda domestica stabile, rendimenti calmi |
| Normal funding | GFN gestibile, aste regolari, buyer base diversificata |
| Watch | GFN alto o domanda estera in calo |
| Stress | rendimenti in rialzo, aste deboli, rollover elevato |
| Fiscal dominance | banca centrale costretta ad assorbire emissioni |

---

## 7. Motore 3 — Private Credit Cycle Engine

### 7.1 Obiettivo

Separare ciclo del credito privato dal ciclo del debito sovrano.

Questa separazione è essenziale. Un paese può avere:

- debito pubblico basso e bolla privata alta;
- debito pubblico alto e credito privato debole;
- entrambi alti;
- entrambi bassi.

### 7.2 Variabili

| Variabile | Fonte | Direzione rischio |
|---|---|---:|
| credit-to-GDP gap | BIS | alto = rischio bolla |
| private DSR | BIS | alto = fragilità |
| household DSR | BIS | alto = fragilità consumi |
| corporate DSR | BIS | alto = fragilità imprese |
| crescita credito reale | BIS | troppo alta = rischio |
| real house price gap | BIS/OECD | alto = rischio |
| NPL ratio | World Bank/IMF/banche centrali | alto = stress bancario |

### 7.3 Stati del ciclo privato

| Fase | Segnale |
|---|---|
| Early private expansion | credito cresce da livelli bassi, DSR basso |
| Healthy expansion | credito cresce con redditi, DSR stabile |
| Late leveraging | credit gap positivo, asset inflation |
| Bubble | credit gap > +10pp, property gap alto, DSR in salita |
| Squeeze | DSR alto, tassi alti, credito rallenta |
| Private deleveraging | credito/PIL scende, default/NPL salgono |

### 7.4 Formula

```text
Private Credit Risk =
  0.30 * z(credit_gap)
+ 0.25 * z(private_DSR)
+ 0.15 * z(real_credit_growth)
+ 0.15 * z(real_house_price_gap)
+ 0.15 * z(NPL_ratio)
```

### 7.5 Uso nel classificatore

Il credit cycle non deve automaticamente determinare il sovereign cycle.

Esempi:

```text
Hong Kong:
private_credit_risk = alto
sovereign_solvency_risk = basso
diagnosi = private late leveraging, not sovereign late debt cycle
```

```text
USA:
private_credit_risk = medio
sovereign_solvency_risk = alto/deteriorating
diagnosi = late sovereign long debt cycle
```

---

## 8. Motore 4 — External Currency Constraint Engine

### 8.1 Obiettivo

Misurare se il paese ha un vincolo esterno che può trasformare un problema fiscale in crisi valutaria.

### 8.2 Variabili

| Variabile | Fonte | Uso |
|---|---|---|
| current account/PIL | IMF WEO | fabbisogno esterno |
| NIIP/PIL | IMF IIP | posizione patrimoniale estera |
| riserve/importazioni | IMF/World Bank | difesa valuta |
| short-term external debt/riserve | World Bank IDS/QEDS | rollover esterno |
| external debt service/export | World Bank IDS | stress valuta |
| quota debito pubblico in valuta estera | DMO/World Bank | mismatch valutario |
| FX regime | IMF AREAER / database accademici | rigidità cambio |
| inflazione | IMF WEO | pressione nominale |
| variazione FX reale | BIS/IMF | aggiustamento esterno |

### 8.3 Formula

```text
Currency Vulnerability Score =
  z(current_account_deficit_gdp)
+ z(net_external_liability_position)
+ z(short_term_external_debt_reserves)
+ z(external_debt_service_exports)
+ z(fx_debt_share)
+ z(inflation)
+ z(real_fx_overvaluation)
- z(reserve_adequacy)
```

### 8.4 Classi

| Stato | Diagnosi |
|---|---|
| Reserve currency issuer | basso vincolo esterno immediato |
| Strong external creditor | basso vincolo esterno |
| Balanced external position | neutrale |
| External funding dependent | rischio in caso di shock |
| FX fragile | crisi valutaria plausibile |
| Hard-currency debt trap | alto rischio default/svalutazione |

### 8.5 Nota sulle valute di riserva

Per USA, eurozona, Giappone, UK e Svizzera il vincolo esterno va trattato diversamente. Un emittente di valuta di riserva può sostenere più a lungo squilibri fiscali, ma paga il costo attraverso:

- rendimenti reali più bassi o più volatili;
- svalutazione reale della valuta;
- inflazione;
- repressione finanziaria;
- riallocazione verso oro e asset reali.

---

## 9. Motore 5 — Political Execution Engine

### 9.1 Obiettivo

Misurare se il paese può fare l'aggiustamento richiesto senza crisi politica.

Dalio lega il ciclo del debito al ciclo politico interno e geopolitico. Il debito eccessivo non produce solo stress finanziario; produce conflitto distributivo.

### 9.2 Variabili

| Variabile | Fonte | Uso |
|---|---|---|
| WGI government effectiveness | World Bank | capacità esecutiva |
| WGI rule of law | World Bank | credibilità istituzionale |
| WGI control of corruption | World Bank | qualità dello Stato |
| WGI political stability | World Bank | rischio rottura |
| polarizzazione politica | V-Dem / WGI proxy | capacità di compromesso |
| protest/social unrest | ACLED se usato | rischio sociale |
| election risk | calendario elettorale | rischio policy reversal |

### 9.3 Formula base

```text
Political Execution Score =
  0.30 * government_effectiveness
+ 0.25 * rule_of_law
+ 0.20 * control_of_corruption
+ 0.15 * political_stability
+ 0.10 * regulatory_quality
```

Poi si applica un penalizzatore:

```text
adjustment_feasibility =
political_execution_score
- fiscal_adjustment_required
- inequality/polarization_penalty
```

### 9.4 Uso

Due paesi con stesso deficit non hanno stesso rischio.

```text
Paese A:
deficit alto, istituzioni forti, valuta propria, base domestica stabile
=> aggiustamento difficile ma credibile
```

```text
Paese B:
deficit alto, istituzioni fragili, debito estero, inflazione alta
=> rischio crisi molto più alto
```

---

## 10. Nuova definizione di deleveraging

Il report attuale dovrebbe sostituire il binomio semplice:

```text
beautiful vs ugly
```

con una tassonomia a cinque categorie.

### 10.1 Beautiful deleveraging

Condizioni minime:

```text
debito/reddito scende
+ crescita reale positiva
+ inflazione sotto controllo
+ interessi/entrate stabili o in calo
+ valuta stabile
+ accesso al mercato preservato
+ sistema bancario non in crisi
```

Questa è la forma sana.

### 10.2 Inflationary deleveraging

Condizioni:

```text
debito/PIL scende
ma inflazione alta
o valuta in forte deprezzamento
o tassi reali molto negativi
```

Esempi tipici:

- erosione del valore reale dei debiti domestici;
- repressione finanziaria;
- perdita di potere d'acquisto dei creditori;
- fuga verso dollaro, oro o beni reali.

Questa categoria evita errori come classificare automaticamente l'Argentina come "beautiful".

### 10.3 Repressive deleveraging

Condizioni:

```text
tassi reali negativi
+ controlli o incentivi regolamentari che forzano domanda domestica di debito
+ banca centrale o sistema bancario assorbono emissioni
+ valuta/risparmiatori sopportano il costo
```

Esempi potenziali:

- yield curve control;
- obblighi regolamentari su banche/fondi;
- controllo capitali;
- tassazione implicita dei risparmiatori.

### 10.4 Restructuring/default deleveraging

Condizioni:

```text
debito scende tramite haircut, maturity extension, reprofiling, default selettivo
```

Non è "beautiful" anche se il rapporto debito/PIL migliora dopo la ristrutturazione.

### 10.5 Ugly deleveraging

Condizioni:

```text
debito/reddito scende
ma con recessione profonda
+ crisi bancaria
+ disoccupazione alta
+ default
+ caduta asset
+ instabilità politica
```

---

## 11. Classificatore finale del ciclo

### 11.1 Output separati

Il nuovo report deve produrre questi output per ogni paese:

```text
Sovereign Solvency: strong / stable / watch / stressed / critical
Funding Liquidity: easy / normal / watch / stress / fiscal dominance
Private Credit Cycle: early / healthy / late / bubble / squeeze / deleveraging
External Constraint: low / moderate / high / severe
Political Execution: strong / adequate / weak / impaired
Deleveraging Type: none / beautiful / inflationary / repressive / restructuring / ugly
Dalio Cycle Stage: early / mid / late / crisis / post-crisis
```

### 11.2 Regole principali

#### Late long debt cycle

```text
IF
    sovereign_solvency_score >= watch
AND
    debt_gdp high OR debt_trend deteriorating
AND
    interest_revenue rising
AND
    funding_stress >= watch
THEN
    stage = late long debt cycle
```

#### Fiscal dominance risk

```text
IF
    GFN/GDP high
AND
    private/foreign demand weakening
AND
    central bank holdings rising
AND
    inflation above target OR currency weakening
THEN
    stage = fiscal dominance risk
```

#### Beautiful deleveraging

```text
IF
    debt_income falling
AND
    real_growth positive
AND
    inflation controlled
AND
    interest_revenue stable_or_falling
AND
    FX stable
AND
    funding_stress <= normal
THEN
    deleveraging_type = beautiful
```

#### Inflationary deleveraging

```text
IF
    debt_gdp falling
AND
    inflation high OR FX depreciation high
AND
    real_rate negative
THEN
    deleveraging_type = inflationary
```

#### Private bubble

```text
IF
    credit_gap > +10pp
AND
    private_DSR high_or_rising
AND
    property_price_gap high
THEN
    private_credit_cycle = bubble
```

#### Sovereign stress without private bubble

```text
IF
    sovereign_solvency_score high
AND
    private_credit_risk low_or_moderate
THEN
    diagnosis = sovereign-driven debt cycle
```

---

## 12. Soglie operative iniziali

Le soglie devono essere calibrate storicamente, ma si può partire con una griglia prudente.

### 12.1 Sovereign

| Indicatore | Watch | Stress | Critical |
|---|---:|---:|---:|
| debito/PIL economie avanzate | 90% | 110% | 130% |
| debito/PIL emergenti | 60% | 80% | 100% |
| interessi/entrate | 10% | 15% | 25% |
| interessi/PIL | 3% | 5% | 7% |
| deficit/PIL | -4% | -6% | -8% |
| saldo primario/PIL | -2% | -4% | -6% |
| r - g | +1pp | +3pp | +5pp |

### 12.2 Funding

| Indicatore | Watch | Stress | Critical |
|---|---:|---:|---:|
| GFN/PIL | 10% | 15% | 25% |
| debito in scadenza 12m/PIL | 8% | 12% | 20% |
| quota estera debito gov | 30% | 45% | 60% |
| calo quota estera 12m | -3pp | -7pp | -12pp |
| aumento rendimento 10Y 12m | +100bp | +200bp | +350bp |

### 12.3 Private credit

| Indicatore | Watch | Stress | Bubble |
|---|---:|---:|---:|
| credit-to-GDP gap | +2pp | +5pp | +10pp |
| DSR privato | 75° percentile | 90° percentile | 95° percentile |
| crescita credito reale | +5% | +8% | +12% |
| house price gap | +10% | +20% | +30% |

### 12.4 External

| Indicatore | Watch | Stress | Critical |
|---|---:|---:|---:|
| current account/PIL | -3% | -5% | -8% |
| external debt service/export | 15% | 25% | 40% |
| short-term external debt/reserves | 50% | 100% | 150% |
| riserve/importazioni | 4 mesi | 3 mesi | 2 mesi |
| FX depreciation 12m | -10% | -20% | -35% |

---

## 13. Nuovo schema dati

Ogni paese dovrebbe avere una tabella normalizzata con questa struttura.

### 13.1 Country master

```text
iso3
country_name
region
income_group
development_status
currency
fx_regime
reserve_currency_status
commodity_exporter_flag
euro_area_flag
imf_program_flag
```

### 13.2 Macro annual

```text
iso3
year
real_gdp_growth
inflation_avg
nominal_gdp_growth
fiscal_balance_gdp
primary_balance_gdp
public_debt_gdp
net_public_debt_gdp
interest_expense_gdp
interest_revenue
current_account_gdp
```

### 13.3 Funding annual/quarterly

```text
iso3
date
gross_financing_needs_gdp
maturing_debt_12m_gdp
average_maturity_years
foreign_holder_share
domestic_bank_holder_share
central_bank_holder_share
pension_insurance_holder_share
auction_bid_to_cover
auction_tail_bp
ten_year_yield
real_ten_year_yield
sovereign_spread
```

### 13.4 Credit private

```text
iso3
date
credit_to_gdp_gap
private_dsr
household_dsr
corporate_dsr
real_credit_growth
real_house_price_growth
real_house_price_gap
npl_ratio
```

### 13.5 External

```text
iso3
date
niip_gdp
external_debt_gni
short_term_external_debt_reserves
external_debt_service_exports
fx_reserves_import_months
fx_debt_share_public
fx_debt_share_total
real_effective_exchange_rate_gap
```

### 13.6 Governance

```text
iso3
year
voice_accountability
political_stability
government_effectiveness
regulatory_quality
rule_of_law
control_corruption
political_execution_score
```

---

## 14. Pseudocodice del modello

```python
def nominal_growth(real_growth, inflation):
    return (1 + real_growth) * (1 + inflation) - 1


def r_minus_g(interest_expense, avg_debt_stock, real_growth, inflation):
    r_eff = interest_expense / avg_debt_stock
    g_nom = nominal_growth(real_growth, inflation)
    return r_eff - g_nom


def sovereign_solvency_score(row):
    components = {
        "debt_gdp": score_threshold(row.debt_gdp, by_income_group=True),
        "net_debt_gdp": score_threshold(row.net_debt_gdp, by_income_group=True),
        "interest_revenue": score_threshold(row.interest_revenue),
        "interest_gdp": score_threshold(row.interest_gdp),
        "primary_deficit": score_threshold(-row.primary_balance_gdp),
        "r_minus_g": score_threshold(row.r_minus_g),
        "debt_trend": score_threshold(row.debt_trend_5y),
    }
    return weighted_average(components)


def funding_liquidity_score(row):
    components = {
        "gfn_gdp": score_threshold(row.gfn_gdp),
        "maturity_wall": score_threshold(row.maturing_debt_12m_gdp),
        "foreign_share": score_threshold(row.foreign_holder_share),
        "foreign_flow": score_threshold(-row.change_foreign_holdings),
        "yield_change": score_threshold(row.ten_year_yield_change_12m),
        "auction_tail": score_threshold(row.auction_tail_bp),
        "domestic_stable_base": 100 - score_threshold(row.domestic_stable_holder_share),
    }
    return weighted_average(components)


def classify_deleveraging(row):
    if row.debt_income_falling:
        if (
            row.real_growth > 0
            and row.inflation < row.inflation_threshold
            and row.interest_revenue_trend <= 0
            and row.fx_stress < 40
            and row.funding_score < 40
        ):
            return "beautiful"
        if row.inflation > row.high_inflation_threshold or row.fx_depreciation > row.fx_depreciation_threshold:
            return "inflationary"
        if row.real_rate < 0 and row.central_bank_holdings_rising:
            return "repressive"
        if row.restructuring_event:
            return "restructuring"
        return "ugly"
    return "none"


def classify_dalio_stage(row):
    if row.funding_score >= 80 and row.currency_score >= 70:
        return "crisis"
    if row.sovereign_score >= 60 and row.funding_score >= 50:
        return "late_long_debt_cycle"
    if row.private_credit_score >= 75:
        return "private_bubble"
    if row.private_credit_score >= 55:
        return "late_leveraging"
    if row.real_growth < 0:
        return "contraction"
    return "early_or_mid_cycle"
```

---

## 15. Esempi di riclassificazione

### 15.1 Stati Uniti

Diagnosi proposta:

```text
Sovereign Solvency: deteriorating / watch-stress
Funding Liquidity: watch
Private Credit Cycle: neutral-moderate
External Constraint: low near-term, rising long-term
Political Execution: impaired by polarization
Dalio Cycle Stage: late long debt cycle
Expected Adjustment: repression / monetization risk, not classic default
```

Ragione:

- il problema centrale non è una bolla privata classica;
- il problema è deficit strutturale, aumento interessi, necessità di collocare grandi quantità di Treasury;
- il vincolo esterno è basso nel breve per via dello status del dollaro;
- il rischio cresce attraverso tassi reali, domanda estera, Fed balance sheet, valuta e asset reali.

### 15.2 Argentina

Diagnosi proposta:

```text
Sovereign Solvency: fragile
Funding Liquidity: constrained
Private Credit Cycle: shallow
External Constraint: severe
Political Execution: volatile
Dalio Cycle Stage: post-crisis / inflationary adjustment
Deleveraging Type: inflationary / restructuring, not beautiful
```

Ragione:

- il debito/PIL può scendere per effetto nominale;
- inflazione e valuta distorcono il segnale;
- l'accesso al mercato è fragile;
- non va classificata come beautiful solo perché il denominatore nominale cresce.

### 15.3 Singapore

Diagnosi proposta:

```text
Sovereign Solvency: strong
Funding Liquidity: strong
Private Credit Cycle: monitor
External Constraint: low
Political Execution: strong
Dalio Cycle Stage: not late sovereign debt cycle
Balance Sheet Type: asset-backed sovereign model
```

Ragione:

- debito lordo alto;
- asset pubblici molto rilevanti;
- alta credibilità istituzionale;
- funding domestico/stabile;
- classificare sulla base del solo debito lordo è fuorviante.

### 15.4 Giappone

Diagnosi proposta:

```text
Sovereign Solvency: structurally weak but domestically financed
Funding Liquidity: dependent on domestic institutions / central bank credibility
Private Credit Cycle: weak
External Constraint: low-moderate
Political Execution: adequate
Dalio Cycle Stage: advanced managed long debt cycle
Expected Adjustment: repression, inflation tolerance, currency depreciation risk
```

Ragione:

- il debito è molto alto;
- la banca centrale e gli investitori domestici sono fondamentali;
- il rischio non è default classico, ma rendimento reale, repressione e valuta.

---

## 16. Visualizzazione consigliata

Il report dovrebbe mostrare meno ranking unico e più mappe diagnostiche.

### 16.1 Country card

Ogni paese:

```text
Country: USA

Sovereign Solvency      ███████░░░ 70 / 100
Funding Liquidity       ██████░░░░ 60 / 100
Private Credit Risk     ████░░░░░░ 40 / 100
External Constraint     ██░░░░░░░░ 20 / 100
Political Execution     ███░░░░░░░ 35 / 100

Dalio Stage:
Late long debt cycle

Adjustment Path:
Repressive / monetization risk

Key Watch Items:
- interest/revenue
- Treasury net issuance
- foreign holdings
- Fed balance sheet
- real yields
- gold/USD behavior
```

### 16.2 Heatmap

Righe = paesi.
Colonne = motori.

```text
Country | Solvency | Funding | Private Credit | External | Political | Stage
USA     | 70       | 60      | 40             | 20       | 35        | Late long cycle
JPN     | 80       | 50      | 20             | 35       | 55        | Managed late cycle
ARG     | 85       | 85      | 20             | 90       | 30        | Inflationary adjustment
SGP     | 20       | 15      | 45             | 10       | 85        | Strong balance sheet
```

### 16.3 Flow-of-funds view

Per paesi principali:

```text
New debt issued
→ bought by domestic banks
→ bought by households/pensions
→ bought by foreigners
→ bought by central bank
→ failed demand / yield jump
```

Questa vista è più vicina a Dalio della sola etichetta di fase.

---

## 17. Pipeline dati

### 17.1 Frequenza

| Blocco | Frequenza |
|---|---|
| WEO/Fiscal Monitor | semestrale |
| BIS credit/DSR | trimestrale |
| TIC USA | mensile |
| ECB SHSS | trimestrale |
| World Bank IDS | annuale |
| WGI | annuale |
| rendimenti sovrani | giornaliera/mensile |
| aste governative | per asta/mensile |

### 17.2 Processo

```text
1. ingest official data
2. map country ISO3
3. normalize units
4. compute derived indicators
5. detect stale data
6. compute absolute thresholds
7. compute changes/trends
8. compute peer z-scores
9. calculate engine scores
10. classify stages
11. generate dashboard
12. generate caveats automatically
```

### 17.3 Staleness rules

Ogni indicatore deve avere un flag:

```text
fresh
lagged
stale
missing
proxy_used
```

Regole:

| Tipo dati | Stale se più vecchio di |
|---|---:|
| rendimenti | 30 giorni |
| aste | 90 giorni |
| BIS trimestrale | 9 mesi |
| WEO | 9 mesi |
| World Bank IDS | 24 mesi |
| WGI | 24 mesi |

Se un dato è stale, il report deve abbassare la confidenza, non riempire silenziosamente.

---

## 18. Confidence score

Ogni diagnosi deve avere una confidenza.

```text
confidence =
  data_coverage
* freshness_score
* source_quality
* model_agreement
```

Esempio:

```text
USA:
data coverage = high
freshness = high
source quality = high
model agreement = high
confidence = high
```

```text
Frontier market:
data coverage = low
freshness = mixed
source quality = medium
model agreement = low
confidence = low
```

Output:

| Confidence | Significato |
|---|---|
| High | dati completi, freschi, fonti robuste |
| Medium | qualche proxy o dato lagged |
| Low | dati mancanti o classificazione fragile |

---

## 19. Controlli anti-falso segnale

Il modello deve avere caveat automatiche.

### 19.1 Debito lordo vs netto

```text
IF gross_debt_high AND net_debt_low:
    caveat = "High gross debt but asset-backed public balance sheet"
```

### 19.2 Inflazione

```text
IF debt_gdp_falling AND inflation_high:
    caveat = "Debt ratio falling through inflationary denominator effect"
```

### 19.3 Valuta di riserva

```text
IF reserve_currency_status == true:
    external_constraint_score adjusted downward
    but monetary_debasement_risk adjusted upward
```

### 19.4 Eurozona

```text
IF euro_area_member == true:
    policy_rate = ECB
    monetary_sovereignty = limited
    sovereign_spread_to_bund required
```

### 19.5 Commodity exporters

```text
IF commodity_exporter == true:
    fiscal_balance adjusted for commodity cycle
    current_account adjusted for terms-of-trade shock
```

### 19.6 Financial centers

```text
IF financial_center == true:
    external assets/liabilities gross positions require separate treatment
```

---

## 20. Validazione storica

Il modello va validato su crisi note.

### 20.1 Campione minimo

| Episodio | Tipo |
|---|---|
| Giappone anni 1990-oggi | deleveraging/repressione |
| Asia 1997 | crisi esterna/FX |
| Argentina 2001 e 2018-2024 | default/inflazione |
| USA 2008 | private debt crisis |
| Eurozona 2010-2012 | sovereign/funding crisis |
| Grecia 2010-2015 | sovereign restructuring |
| Turchia 2018-2024 | FX/inflationary stress |
| Sri Lanka 2022 | external debt crisis |
| UK 2022 gilt crisis | funding/liability-driven shock |
| USA 2020-2026 | fiscal dominance watch |

### 20.2 Obiettivo della validazione

Il modello deve segnalare:

- accumulo del rischio prima della crisi;
- deterioramento funding prima del default o bailout;
- differenza tra crisi privata e crisi sovrana;
- differenza tra default risk e debasement risk;
- casi in cui il debito/PIL da solo avrebbe dato falso positivo o falso negativo.

---

## 21. Output finale consigliato

Il report dovrebbe avere questa struttura.

```text
1. Global Overview
   - mappa per stage
   - mappa per funding stress
   - mappa per external constraint

2. Big Debt Cycle
   - sovereign solvency
   - funding liquidity
   - monetary response risk

3. Private Credit Cycle
   - credit gap
   - DSR
   - property/asset risk

4. External Constraint
   - current account
   - reserves
   - external debt service
   - FX regime

5. Country Sheets
   - score a 5 motori
   - stage
   - deleveraging type
   - top 5 risk drivers
   - caveat automatiche
   - dati stale/missing

6. Methodology
   - fonti
   - formule
   - soglie
   - limiti

7. Data Quality
   - coverage matrix
   - stale data table
   - proxy usage
```

---

## 22. Sintesi finale

La modifica principale è questa:

```text
Non classificare il paese partendo da debito/PIL.
Classificarlo partendo dalla capacità di servire, rifinanziare e collocare il debito.
```

La lettura più vicina a Dalio richiede di guardare:

1. debito rispetto al reddito;
2. interessi rispetto alle entrate;
3. fabbisogno lordo di finanziamento;
4. compratori marginali del debito;
5. banca centrale come compratore di ultima istanza;
6. valuta e riserve;
7. inflazione e rendimenti reali;
8. capacità politica di fare aggiustamento;
9. probabilità di repressione, monetizzazione, default o ristrutturazione.

Il report esistente non va buttato. Va riclassificato come **front-end visuale**. Il motore sottostante va sostituito con una struttura causale a cinque engine.

La versione corretta non dovrebbe dire solo:

```text
Paese X = BEAUTIFUL_DELEVERAGING
```

Dovrebbe dire:

```text
Paese X:
- debito/reddito in calo
- ma interessi/entrate in aumento
- funding estero in deterioramento
- inflazione alta
- valuta sotto pressione
=> inflationary/repressive deleveraging, non beautiful
```

Questo è il passaggio da dashboard "Dalio-inspired" a modello macro realmente più vicino alla meccanica del Big Debt Cycle.

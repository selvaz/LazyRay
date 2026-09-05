# LazyRay — metodologia (versione 2026-09)

> Descrive **ciò che il codice fa oggi** sul branch `feat/prod-cadence-asof-stress`:
> tre prodotti, le formule, i dati, la provenienza di ogni soglia e i limiti.
> Il documento che spiega *perché* si è arrivati qui (misure sul sistema
> precedente) è [DALIO_PROD_ASSESSMENT_2026-09.md](DALIO_PROD_ASSESSMENT_2026-09.md);
> il monitor di stress ha anche la sua scheda tecnica in
> [STRESS_MONITOR_2026-09.md](STRESS_MONITOR_2026-09.md).
> Ogni numero citato come "misurato" viene da una corsa reale sui DB di
> produzione (in sola lettura) del 2026-09-04.

---

## 0. Che cosa il sistema afferma di misurare — e che cosa no

LazyRay produce **descrizioni ordinate dello stato osservabile**, non previsioni.
In concreto:

| Afferma | Non afferma |
|---|---|
| Dove si colloca un paese, oggi, su otto misure di bilancio pubblico rispetto a soglie dichiarate e ai suoi pari | Che un paese andrà in default, o quando |
| Con quale probabilità il rapporto debito/PIL sarà più alto fra 5 anni **dato** il comportamento storico dei suoi shock | Che quella probabilità sia calibrata (nessun backtest è stato fatto) |
| Quanto sono tese oggi le condizioni di finanziamento di mercato rispetto alla storia della stessa area | Che una lettura "stress" preceda una crisi |
| Che cosa è cambiato rispetto alla corsa precedente e per quale componente | Che il cambiamento sia significativo in senso statistico |

**Nessuna delle etichette è validata contro episodi storici di crisi.** Finché
il §7 non è eseguito, ogni output porta la riga "non validato". Questa non è
una formula di cortesia: è la ragione per cui i bucket hanno nomi descrittivi
(`low/moderate/elevated/high/critical`) e non valutativi ("solido", "a rischio").

---

## 1. Architettura: tre prodotti, tre cadenze, un solo database

La regola che governa tutto: **la cadenza di calcolo segue la cadenza del dato,
e ogni corsa lascia una riga con la propria data.**

| Prodotto | Cadenza | Perché | Tabelle |
|---|---|---|---|
| **Rischio paese strutturale** (5 motori + classificatore di ciclo) | Giornaliera ma **a vuoto** se gli input non sono cambiati (`--if-changed`, uscita 3) | 76 su 83 indicatori sono annuali; le revisioni reali si concentrano in ~4 giorni al mese | `engine_scores`, `dalio_cycle_v2`, `run_meta` |
| **Stress di finanziamento di mercato** | Giornaliera, davvero | Costruito solo su serie giornaliere/settimanali (FRED, ETF, BCE) | `stress_index`, `stress_components` |
| **Brief** (l'unico output umano) | A ogni corsa che produce qualcosa | Un messaggio breve + un HTML da ~60 KB | `digest_log` |

Input letti **esclusivamente** tramite l'API pubblica di market-data-hub
(`reader.read_macro_panel`, `read_macro_panel_ext`, `read_macro`, `read_prices`);
mai SQL diretto sul DuckDB del hub. Output scritti solo sul DuckDB proprio di
LazyRay, sotto lock di scrittura.

### 1.1 Il modello temporale (quattro decisioni distinte)

1. **`ref_date` = la data della corsa**, non un 31 dicembre fisso. Corse in
   giorni diversi accumulano storia (una ri-corsa nello stesso giorno
   sostituisce solo quel giorno). È ciò che rende possibili sia il diff sia
   l'isteresi, che nel sistema precedente non si attivava mai.
2. **Livelli solo da dati realizzati.** `actual_cutoff(ref_date)` = 31 dicembre
   dell'anno *precedente*: nessuna componente di "condizione corrente" può
   leggere una proiezione WEO. Prima di questa regola gli Stati Uniti venivano
   valutati su un debito di 125,8% del PIL che era la *previsione* IMF per il
   2026; ora leggono 123,9%, l'ultimo dato realizzato.
   *Eccezione dichiarata:* le serie ad alta frequenza che non contengono righe
   di previsione (BIS trimestrali, rendimenti FRED mensili, REER) non sono
   tagliate al 31/12 — sarebbe scartare un dato legittimo più recente.
   *Eccezione opposta:* le componenti di **traiettoria** (`debt_trend_5y`)
   continuano deliberatamente a includere le proiezioni, e lo dichiarano con il
   flag `debt_trend_forecast_dependent` più un valore gemello calcolato solo
   sugli actual.
3. **Point-in-time per le date passate.** Con `--as-of <data>` il pannello è
   ricostruito com'era noto a quella data leggendo le tabelle vintage del hub;
   le tre serie derivate della vista del hub (`bond_yield_10y`,
   `implied_interest_rate`, `fx_debt_share`) sono ricalcolate in Python dalle
   stesse letture vintage, così la forma è identica alla vista live.
   `components_json.vintage_safe` registra quale percorso è stato usato.
   *Limite:* il log vintage del hub inizia il 2026-07-10; prima di quella data
   il point-in-time è vuoto o parziale, e il sistema non lo maschera.
4. **Rilevazione del cambiamento.** Prima di calcolare, il runner costruisce un
   SHA-256 delle righe (data, paese, indicatore, valore) dei **28 indicatori che
   i motori leggono davvero** e lo confronta con l'ultima riga di `run_meta`.
   Se identico e `--if-changed` è attivo: nessun calcolo, nessuna scrittura,
   uscita 3 — così lo scheduler non manda un messaggio per un giorno in cui non
   è successo nulla. L'hash è ristretto ai 28 indicatori perché il hub ne
   aggiorna ~83, molti dei quali (tassi di policy, quote di commercio) nessun
   motore consuma: sull'intero pannello "invariato" non sarebbe quasi mai vero.

---

## 2. Rischio paese: la macchina comune

Ogni motore trasforma N componenti grezze in un punteggio 0-100 (**alto =
peggio**) e in un'etichetta, con lo stesso meccanismo.

### 2.1 Da valore grezzo a punteggio: `score_threshold`

Interpolazione lineare fra tre soglie nominate — `watch`, `stress`, `critical`:

```
valore <= watch                 -> 0
watch  <  valore <= stress      -> 50 * (v - watch) / (stress - watch)
stress <  valore <= critical    -> 50 + 50 * (v - stress) / (critical - stress)
valore >  critical              -> 100
```

`orientation = -1` inverte la direzione per le grandezze in cui *basso* è
peggio (riserve in mesi di import, pendenza della curva): le soglie restano
scritte dal più mite al più grave, quindi discendenti. Se dopo l'inversione non
sono strettamente ordinate la funzione **solleva un errore** invece di
degenerare silenziosamente in un gradino 0/100.

### 2.2 Aggregazione e copertura

Media pesata sulle sole componenti disponibili. Il rapporto fra componenti
presenti e attese determina il **livello di copertura**, che è parte
dell'output, non una nota a piè di pagina:

| Copertura | Regola | Effetto |
|---|---|---|
| `full` | ≥ 80% delle componenti attese | punteggio pubblicato, confidenza alta |
| `proxy` | ≥ 40% | punteggio pubblicato, marcato: **mai equiparabile a un `full`** |
| `insufficient` / `no_data` | < 40% / nessun dato | **punteggio soppresso (NULL)**, etichetta assente |

La soppressione è deliberata: un paese con 1 componente su 7 disponibile
leggerebbe "0,0 / low" con l'aria di un dato affidabile.

### 2.3 Etichette e isteresi

Punteggi tagliati a `[20, 40, 60, 80]` in cinque bucket. Per evitare che una
componente ballerina faccia oscillare l'etichetta a ogni corsa, il passaggio
*a un bucket adiacente* richiede di superare il confine di un margine pari al
10% dell'ampiezza fra la soglia precedente e la successiva (4 punti con i tagli
standard). Un salto di più di un bucket si applica sempre subito: l'isteresi
smorza il tremolio, non nasconde un movimento vero.

### 2.4 La doppia scala (assoluta e relativa)

Il difetto centrale del sistema precedente era la comparabilità: la Turchia
risultava "strong" sul sovrano con debito al 25% del PIL mentre lo stesso
report la dava "elevated" sull'estero. Ora ogni componente porta, oltre al
punteggio su soglia assoluta:

- **`pct_own_history`** — percentile del valore nella storia **dello stesso
  paese** (minimo 15 osservazioni, altrimenti nullo);
- **`pct_group`** — percentile fra i **pari per gruppo di reddito**
  (36 DM / 21 EM / 7 Frontier, da `countries.yaml`; nullo se il gruppo ha meno
  di due paesi con dato).

`relative_score` è la media dei `pct_group` disponibili del motore, e
`relative_label` applica gli stessi tagli. **L'etichetta primaria resta quella
su soglia assoluta**: la relativa la affianca e nel Brief compare come numero
("vs pari 78"), non come una seconda parola-verdetto.

---

## 3. I cinque motori

Ordine di lettura nel Brief: sovrano, estero, credito privato, funding, politico.

### 3.1 Sovrano (`sovereign_solvency`) — 8 componenti, peso 1 ciascuna

| Componente | Fonte | Soglie (watch/stress/critical) | Provenienza |
|---|---|---|---|
| `debt_gdp` | WEO, ultimo actual | DM 90/110/130 · EM 60/80/100 | proposta di origine |
| `net_debt_gdp` | WEO | DM 90/110/130 · EM 60/80/100 | proposta |
| `interest_revenue` | interessi/PIL ÷ entrate/PIL | 10/15/25 | proposta |
| `interest_gdp` | IMF | 3/5/7 | proposta |
| `primary_deficit_gdp` | IMF (segno invertito: disavanzo positivo) | 2/4/6 | proposta |
| `r_minus_g` | tasso implicito − crescita nominale | 1/3/5 | proposta |
| `debt_trend_5y` | pendenza OLS su [anno−3, anno+5], **include proiezioni** | 0,7/1,5/3,0 pp/anno | proposta |
| `debt_p_up_5y` | simulazione DSA (§3.6) | 0,50/0,70/0,85 | **ASSUNTA** |

`r` è il **tasso implicito** (interessi pagati ÷ stock di debito), non il tasso
di policy: era una delle tre correzioni chieste dalla revisione metodologica di
luglio, perché il tasso di policy appiattiva l'area euro e produceva
"deleveraging bellissimi" argentini spuri. `g` è nominale, da crescita reale e
inflazione WEO.

**Il gate FX.** Un'etichetta `low` afferma un basso *carico* di debito, non
solidità. Per un paese che non emette valuta di riserva (insieme definito in
`external_constraint`: USA, JPN, GBR, CHE e i membri dell'euro) e che ha
`fx_debt_share > 50%` **oppure** inflazione `> 15%`, l'etichetta `low` viene
sostituita da **`fx_constrained`**, con la ragione e i due valori registrati in
`components_json.gate`. Non è un sesto bucket: è un'annotazione post-assegnazione.
Effetto misurato: Turchia (debito FX 88%, inflazione 28,6%) e Argentina
(inflazione 42%) escono dal "low" in cui il sistema precedente le collocava.

### 3.2 Estero (`external_constraint`) — 8 componenti, pesi uguali

Disavanzo di parte corrente/PIL **3/5/8**, posizione netta sull'estero **35/50/70**
(ASSUNTA, allineata alla soglia di allerta −35% della Procedura per gli squilibri
macroeconomici UE), debito a breve/riserve **50/100/150**, servizio del
debito/export **15/25/40**, quota di debito in valuta **30/50/70** (ASSUNTA),
inflazione **5/10/20** (ASSUNTA), sopravvalutazione REER **10/20/30** (ASSUNTA),
riserve in mesi di import **4/3/2** (discendente). Chi emette valuta di riserva
riceve uno sconto moltiplicativo di **0,6** sul punteggio.

### 3.3 Credito privato (`private_credit`) — 5 componenti pesate

Gap credito/PIL **2/5/10** (BIS dove esiste, 43 paesi su 64; altrove un proxy per
detrend lineare su `private_debt_gdp`, che **forza la copertura a `proxy`**),
percentile del servizio del debito privato nella *propria* storia **75/90/95**,
crescita reale del credito **5/8/12**, gap dei prezzi delle case **non cablato**
(nessun connettore BIS WS_SPP: mantiene il peso, quindi pesa nella copertura
dichiarata, ma non produce punteggio), NPL **3/6/10** (ASSUNTA: la proposta di
origine non dà soglie).

*Avvertenza dalla letteratura:* nei mercati emergenti il gap credito/PIL ha
prestazioni deboli come indicatore di allerta; la crescita del credito fa meglio
(Econstor, 13 economie emergenti). È una ragione in più per non leggere questo
motore come predittivo.

### 3.4 Funding (`funding_liquidity`) — due rami, dati diversi

Il motore precedente confrontava, nello stesso bucket "easy/stress", due
popolazioni quasi disgiunte. Ora il ramo è esplicito e registrato in
`components_json.branch`:

| Ramo | Quando | Componenti | Copertura misurata |
|---|---|---|---|
| `market` | esiste un 10Y fresco (OCSE) | variazione 12m del 10Y **1,0/2,0/3,5 pp** (proposta §12.2), term spread 10Y−policy **1,0/0,0/−1,0** (orientamento −1, ASSUNTA), variazione 12m del REER in valore assoluto **10/15/25%** (ASSUNTA) | 32 paesi |
| `external` | niente 10Y ma c'è IDS | debito a breve/riserve **50/100/150**, servizio del debito/export **15/25/40** (soglie riusate da §3.2, non duplicate) | 18 paesi |
| `none` | nessuno dei due | nessuna: punteggio ed etichetta **NULL**, copertura `no_data` | 14 paesi |

Un paese con entrambi i rami tiene `market`. I 14 senza dati non spariscono:
sono dichiarati, e il classificatore di ciclo li marca
`unclassified_no_funding_data` invece di produrre uno stadio nullo senza spiegazione.

### 3.5 Politico (`political_execution`) — 5 indicatori WGI

Efficacia del governo 0,30 · stato di diritto 0,25 · controllo della corruzione
0,20 · stabilità politica 0,15 · qualità regolatoria 0,10. Gli indicatori WGI
sono già standardizzati (−2,5…+2,5) e vengono convertiti in **percentile
cross-country**, poi invertiti perché alto = peggio.

**Conseguenza strutturale, dichiarata:** essendo un percentile, questo motore
mette *sempre* circa 12 paesi in `weak` e 12 in `impaired`, per costruzione e
indipendentemente da qualunque cambiamento del mondo. Per questo nel Brief non
può da solo mettere un paese in watchlist (§5): compare solo come contesto
accanto a un'altra ragione.

### 3.6 La simulazione DSA (`dalio_v2/dsa.py`)

Sostituisce l'idea che una traiettoria deterministica dica qualcosa sul rischio:
la traiettoria WEO è già `debt_trend_5y`, e non dice nulla sulla **distribuzione**
degli esiti. Identità annuale del debito (r, g, pb in percentuale; pb positivo =
avanzo):

```
d(t+1) = d(t) * (1 + r(t)/100) / (1 + g(t)/100) - pb(t+1)
```

- **Shock** su `(r − g)` e `pb` estratti **congiuntamente** da una normale
  stimata sulle differenze prime della storia annuale del paese
  (2000 → ultimo actual), winsorizzate al 5°/95° percentile.
- **5.000 traiettorie**, 5 anni, `numpy.default_rng(seed=42)` → risultato
  riproducibile bit per bit.
- Minimo **12 osservazioni** annuali accoppiate, altrimenti la componente è
  nulla. Misurato: calcolata per **60 paesi su 64**.
- Output: `p_up` (quota di traiettorie con debito a +5 anni superiore
  all'ultimo actual) più p10/p50/p90 della distribuzione, tutto in
  `components_json.dsa`.

*Scelta di ancoraggio:* l'anno di partenza è l'ultimo in cui debito, r, g e pb
sono **tutti** realizzati — gli aggregati fiscali ritardano di un anno rispetto a
crescita e inflazione, e un ancoraggio rigido all'anno di cutoff avrebbe
azzerato la componente per quasi tutto il pannello.

*Limite noto:* gli shock si **accumulano** come random walk sul livello di
(r − g) e di pb, senza ritorno alla media. È coerente con il fatto che quei due
livelli storicamente derivano, ma allarga le code a 5 anni (per l'Ucraina il p90
arriva a 366% del PIL). Da rifinire con un processo con ritorno alla media
parziale e, soprattutto, da tarare contro gli episodi storici (§7).

### 3.7 Classificatore di ciclo

Combina le **etichette stabilizzate** dei cinque motori (mai i punteggi grezzi,
per non reintrodurre il tremolio che l'isteresi ha appena tolto) in
`dalio_stage` + `deleveraging_type`. Le soglie ausiliarie
(inflazione alta 10%, deprezzamento FX 15%, tasso reale repressivo −2%, quota
di debito FX "prevalentemente domestico" 20%) sono **ASSUNTE** e vivono nel file
di configurazione, mai come letterali nel codice.

Distribuzione misurata: `early_or_mid_cycle` 48 · `unclassified_no_funding_data`
14 · `late_leveraging` 1 · nullo 1. Che 48 paesi su 64 stiano in "inizio/metà
ciclo" resta un risultato poco informativo: è la prossima cosa da rivedere, e
non è nascosto.

---

## 4. Monitor di stress di mercato (giornaliero)

Costruzione **in stile CISS** (Holló, Kremer, Lo Duca, ECB WP 1426/2012): non una
media di indicatori, ma un aggregato che pesa di più lo stress **simultaneo** fra
segmenti diversi.

### 4.1 Trasformazione

Ogni indicatore è convertito nel proprio **percentile a finestra espansiva**
(CDF empirica su tutta la storia fino a quel giorno, minimo **756 giorni
lavorativi** = 3 anni), quindi confrontabile fra grandezze eterogenee e senza
guardare al futuro. Le serie non giornaliere sono portate a frequenza
giornaliera con riporto in avanti e un **tetto di obsolescenza per frequenza**:
10 giorni per le giornaliere, 21 per le settimanali, 100 per le mensili. Oltre
il tetto l'indicatore non contribuisce (NaN), invece di far finta che un dato
vecchio sia una condizione corrente.

### 4.2 Aggregazione

Con `s` il vettore dei sotto-indici di segmento, `a` i pesi fissi, `C(t)` la
matrice di correlazione EWMA (λ = 0,93 su dati giornalieri) e `w = a ⊙ s`:

```
indice(t) = sqrt( wᵀ C(t) w )   ∈ [0, 1]
```

Un segmento indisponibile non contribuisce (non viene ripesato), e il numero di
segmenti effettivi è pubblicato in `n_segments` — l'area euro è marcata
"parziale" quando i rendimenti mensili sono oltre il tetto.

### 4.3 Indicatori, segno e pesi

**Stati Uniti** — credito 25% (OAS high yield, investment grade, CCC), tassi 20%
(rendimento reale 10Y, |Δ60 giorni| del 10Y, pendenza 10Y−2Y **invertita**:
l'inversione è stress), volatilità 20% (VIX), condizioni 20% (NFCI, STLFSI),
liquidità 15% (**Δ20 giorni di (attivi Fed − conto del Tesoro − reverse repo)**,
con segno negativo: liquidità netta in calo = stress).

**Area euro** — tassi 25% (|Δ60| tasso sui depositi BCE), obbligazioni 30%
(|Δ60| del Bund 10Y), periferia 45% (spread BTP–Bund, Spagna–Bund,
Francia–Bund, in livello).

**Mercati emergenti** — valuta forte 40% (differenziale di rendimento a 20
giorni EMB−IEF, drawdown a 60 giorni di EMB), valuta locale 35% (idem EMLC),
cambio 25% (drawdown degli ETF valutari disponibili).

*Perché non i livelli di RRP, conto del Tesoro, tassi di policy o indice dei
prezzi:* sono serie non stazionarie. Il percentile su storia propria di una
serie che scende monotonicamente (il reverse repo, da 2.300 a ~0 miliardi)
resta inchiodato a 1 per sempre e non misura più nulla. Per questo la liquidità
entra come **variazione**, e l'indice dei prezzi euro è stato tolto del tutto.

### 4.4 Regimi

Il regime primario è **relativo alla storia della stessa area**, con tre livelli
e isteresi, calcolato su percentili a finestra espansiva (nessuno sguardo al
futuro): si entra in `stress` sopra l'85° percentile e si esce sotto il 75°;
si entra in `elevated` sopra il 60° e si esce sotto il 50°. Prima di 3 anni di
storia l'etichetta è `calm` per definizione.

Un HMM gaussiano a 3 stati (fallback a 2) resta come campo **ausiliario**
(`regime_hmm`, `p_stress`) e il documento lo dichiara per quello che è: è
stimato sull'intero campione, quindi le sue etichette storiche vengono
ri-assegnate a ogni ri-stima e **non sono point-in-time**. La ragione del
declassamento è misurata: con un HMM a 2 stati l'etichetta "stress" copriva il
36% dei giorni negli USA e il 77% nell'area euro — cioè "sopra la media", non
"stress".

Quote per regime sulla storia completa, dopo la correzione: USA calm 54% ·
elevated 32% · stress 14%; area euro 58/20/22; emergenti 57/24/22.

---

## 5. Il Brief: come vengono scelte le cose che leggi

- **Ordinamento dei paesi**: media dei `relative_score` fra i cinque motori
  (dove manca il relativo, si usa l'assoluto). È un ordinamento *rispetto ai
  pari*, e il sottotitolo lo dice.
- **Watchlist** — un paese entra se e solo se:
  (a) è scattato il gate FX; oppure (b) un motore **diverso dal politico** è
  nei due bucket peggiori; oppure (c) `p_up ≥ 0,70` **e** il sovrano non è
  `low`. Il motore politico si aggiunge come contesto solo se c'è già un'altra
  ragione (§3.5); la DSA da sola non basta se il debito è basso, altrimenti la
  lista si riempie di paesi che salgono da una base bassa (Israele, Finlandia,
  Nuova Zelanda).
- **Movimenti**: variazioni di punteggio con |Δ| ≥ 2,0 punti rispetto alla corsa
  precedente, con il nome della componente che ha spinto di più. Sotto quella
  soglia il Brief scrive esplicitamente che non è successo nulla di materiale e
  ricorda che gli input sono annuali.
- **Allegati**: il Brief HTML sempre; il report completo da 400 KB solo alla
  prima corsa di ogni mese (stato in `digest_log`, non "giorno ≤ 3": con
  `--if-changed` la corsa del 1° può non esserci).

---

## 6. Tracciabilità

Ogni riga di `engine_scores` porta un `components_json` con: versione del
modello (SHA del commit), `ref_date`, gruppo di reddito, valore grezzo,
punteggio, peso, **data di osservazione** e i due percentili per ogni
componente, `data_through`, elenco delle componenti mancanti, livello di
copertura, `vintage_safe`, più i blocchi `gate`, `dsa`, `branch` dove
pertinenti. `run_meta` registra per ogni corsa l'hash degli input, la versione
del modello, i motori eseguiti e il numero di righe lette. Da qui si ricostruisce
perché un'etichetta era quella che era, senza rieseguire nulla.

---

## 7. Limiti, in ordine di gravità — e come si chiudono

1. **Nessuna validazione.** Nessuna delle soglie è stata testata contro crisi
   realizzate. Il piano esiste ed è alla portata: episodi da Laeven-Valencia
   (151 crisi bancarie 1970-2017, pubblico) più le crisi valutarie/sovrane della
   stessa fonte; vintage storici del WEO per edizione (pubblici dal 2000) per
   ricostruire un pannello point-in-time annuale; metrica **AUC per orizzonte
   1-3 anni** come nella prassi BIS (Drehmann-Juselius), riportando anche i
   falsi allarmi, per motore e per gruppo di paesi. Finché non è fatto, il
   sistema resta descrittivo e lo dichiara in ogni output.
2. **Soglie assunte.** Otto delle soglie in uso non vengono da una fonte
   metodologica ma da stime informate (marcate ASSUNTA in §3 e nel file di
   configurazione). Sono il primo candidato all'analisi di sensibilità
   raccomandata dal manuale OECD/JRC sugli indicatori compositi: pubblicare il
   *range* del posizionamento di ogni paese al variare dei pesi entro bande
   plausibili, e dichiarare i casi instabili.
3. **Code della DSA.** Random walk senza ritorno alla media (§3.6).
4. **Point-in-time corto.** Il log vintage del hub parte dal 2026-07-10: le
   corse `--as-of` precedenti a quella data non sono davvero point-in-time.
5. **Copertura strutturalmente diseguale.** 59 paesi su 64 hanno il motore
   estero in `proxy`; il credito privato è `full` per 32; il funding non esiste
   per 14 paesi. Il sistema lo dichiara riga per riga, ma resta il fatto che
   confrontare un `proxy` con un `full` è un confronto fra oggetti diversi.
6. **Il classificatore di ciclo è poco informativo** (48 paesi su 64 nello
   stesso stadio).
7. **Area euro parziale ed emergenti per procura** nel monitor di stress: niente
   rendimenti giornalieri per l'area euro (solo mensili FRED, spesso oltre il
   tetto di obsolescenza), e nessun dato paese-per-paese per gli emergenti — si
   passa da ETF (EMB, EMLC) e dal cambio.

---

## 8. Riferimenti

- **CISS**: Holló, Kremer, Lo Duca, *CISS — A Composite Indicator of Systemic
  Stress in the Financial System*, ECB WP 1426 (2012).
- **Indicatori di allerta**: Drehmann, Juselius, *Evaluating early warning
  indicators of banking crises* (BIS WP 421, 2013); Aldasoro, Borio, Drehmann,
  *Early warning indicators of banking crises: expanding the family*
  (BIS Quarterly Review, marzo 2018).
- **Episodi di crisi**: Laeven, Valencia, *Systemic Banking Crises Revisited*
  (IMF WP 18/206, 2018) e *Systemic Banking Crises Database II* (IMF Economic
  Review, 2020).
- **Sostenibilità del debito**: IMF, *Staff Guidance Note on the Sovereign Risk
  and Debt Sustainability Framework for Market Access Countries* (2022);
  replica in Python della DSA della Commissione europea di Welslau/Bruegel;
  pacchetto R `debtkit` (proiezione, decomposizione, stress test, fan chart,
  funzione di reazione fiscale di Bohn, indicatori S1/S2).
- **Indicatori compositi**: OECD/JRC, *Handbook on Constructing Composite
  Indicators* (2008); Saisana, Saltelli, Tarantola (2005) sull'analisi di
  incertezza e sensibilità.
- **Correttezza point-in-time**: convenzioni JPMaQS/macrosynergy (z-score
  sequenziali senza sguardo al futuro).
- **Squilibri esterni**: soglia di allerta −35% del PIL della Procedura per gli
  squilibri macroeconomici della Commissione europea.

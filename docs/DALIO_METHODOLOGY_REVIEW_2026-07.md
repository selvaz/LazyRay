# Revisione metodologica e scientifica — Report "Ray Dalio" (2026-07)

> Oggetto: `market_data_hub/dalio.py` + `make_dalio_report.py`, con i parametri
> in `config/settings.yaml → dalio`. Obiettivo: valutare la **validità
> metodologica** delle classificazioni prodotte (fase del ciclo del debito,
> regime crescita/inflazione four-box, composite z-score cross-country) alla
> luce di (a) cosa Dalio/Bridgewater fanno realmente, (b) la letteratura su
> indicatori compositi ed early-warning systems, (c) i risultati attuali del
> report (`dalio_report_20260708`, 64 paesi).
>
> Verdetto in una riga: **l'ingegneria del dato è solida; l'impalcatura
> statistico-inferenziale no.** Il sistema produce etichette nette ("BEAUTIFUL
> DELEVERAGING", quadranti con asset allocation associata) che suggeriscono una
> precisione e una validazione predittiva che il metodo non possiede. Le
> raccomandazioni al §4 mirano a preservare l'output ma a **ricalibrarne le
> pretese** e chiudere tre difetti concreti (tasso di costo del debito,
> look-ahead da forecast, assenza di backtest).

---

## 1. Cosa fa il sistema (sintesi fedele)

Tre layer sovrapposti sul `macro_panel`:

1. **Fase del ciclo del debito** — albero di soglie deterministico
   (`classify_cycle_phase`) su ~11 fasi (EARLY_EXPANSION, LATE_LEVERAGING,
   BUBBLE, HIGH_DEBT_STABLE, LATE_LONG_CYCLE, CONTRACTION, DEPRESSION,
   BEAUTIFUL/UGLY_DELEVERAGING, …).
2. **Regime crescita/inflazione four-box** (`classify_regime`) — quadranti
   Q1–Q4 con asset allocation associata (Q1→risk assets, Q3→oro/difensivi, …).
3. **Composite z-score cross-country** — per ogni indicatore, z = (x − media
   fra paesi)/dev.std × orientamento, clip a ±3, alla data corrente; media per
   pillar; media pesata dei pillar (pesi 20/20/15/15/10/10/5/5) → composite.

Risultati attuali (64 paesi): 23 EARLY_EXPANSION, **17 BEAUTIFUL_DELEVERAGING**,
14 LATE_LEVERAGING, 7 LATE_LONG_CYCLE, 2 CONTRACTION, 1 HIGH_DEBT_STABLE;
quadranti Q4 29 / Q1 18 / Q3 9 / Q2 8; composite in [-0.79, +0.59], media
-0.03, **dev.std 0.30**.

---

## 2. Riscontri metodologici (ordinati per gravità)

### 2.1 — [ALTA] Il four-box non implementa la definizione di Bridgewater

**Cosa fa Bridgewater.** All Weather si basa sul fatto che i prezzi si muovono
su crescita e inflazione **rispetto a ciò che è già scontato dalle
aspettative/dal mercato** — cioè *sorprese rispetto al consenso*, non livelli né
scostamenti da un trend. I quattro ambienti sono crescita↑/↓ e inflazione↑/↓
*relative alle aspettative scontate*.
(bridgewater.com/research-and-insights/the-all-weather-story)

**Cosa fa il codice** (`dalio.py:311-334`):
- `growth_delta = crescita_corrente − potenziale WEO` (media anni [ry+2, ry+5]);
- `infl_delta   = inflazione_corrente − media dei 3 anni precedenti`.

Due problemi sovrapposti:
1. **Nessuno dei due è una "sorpresa rispetto alle aspettative"**: uno è un
   output gap (deviazione da trend potenziale), l'altro una direzione backward.
   Sono grandezze concettualmente diverse fra loro (una forward, una backward) e
   **entrambe diverse da ciò che guida la logica di asset dei quadranti**.
2. Il report mappa comunque i quadranti sull'asset allocation Bridgewater
   (Q1→risk assets, ecc.). Poiché gli *input* non sono quelli di Bridgewater,
   **l'implicazione d'investimento non è giustificata dal calcolo**.

**Conseguenza pratica.** 2022 ha mostrato la fragilità dei regimi: la
correlazione azioni-obbligazioni è passata da ~−0,2 a ~+0,65 e le strategie
risk-parity/All-Weather hanno perso ~22–26% — proprio nei momenti di svolta che
un four-box mal-normalizzato classifica peggio.
(markovprocesses.com/blog/is-2022-all-bad-weather-for-risk-parity; caia.org 2022)

### 2.2 — [ALTA] `nom_rate = policy rate` invalida il test beautiful/ugly

La distinzione *beautiful vs ugly deleveraging* poggia interamente su
**crescita nominale > tasso nominale**, dove il "tasso" deve approssimare il
**costo effettivo del debito** (rendimento sovrano / tasso implicito sullo
stock), non il tasso di policy.

Usando il policy rate (`IND["policy_rate"]`):
- **Tutti i paesi dell'area euro ricevono lo STESSO tasso (2,25%)**: Grecia,
  Spagna, Portogallo, Irlanda, Cipro, Croazia, Slovenia → 2,25 identico.
  Questo **azzera lo spread sovrano**, che è esattamente la variabile che conta
  in una crisi del debito (Grecia 2010–12 con Bund a rendimento simile ma
  BTP/GGB a doppia cifra). Il metodo non può, per costruzione, distinguere un
  sovrano sotto stress da uno core.
- **Argentina** finisce in `BEAUTIFUL_DELEVERAGING` (ng 33,9 > nr 29,0) mentre
  il debito/PIL cala per **erosione inflazionistica**, non per la combinazione
  bilanciata restructuring+stimolo che Dalio chiama "bella". Il composite è
  −0,53 (il peggiore). Il caveat esiste (`CAVEAT.ARG`) ma **l'etichetta di fase
  si propaga negli aggregati** ("17 beautiful deleveraging" nel titolo
  dell'Overview), dove il caveat non compare.

### 2.3 — [ALTA] Look-ahead e bias da forecast WEO nel classificare il "presente"

Sia il regime (potenziale = media WEO [ry+2, ry+5]) sia la traiettoria del
debito (`_slope` su [ry−3, ry+5], **5 punti su 9 sono previsioni**) usano
proiezioni IMF per definire lo **stato corrente**. Tre problemi documentati:

1. **Optimism bias del WEO**, più forte per EM e durante i boom del credito —
   cioè proprio i paesi sotto stress. Maggiore aggiustamento *pianificato* →
   previsioni di crescita più ottimistiche.
   (imf.org WP/20/... "Optimism Bias in Growth Forecasts")
2. **Critica di Orphanides–van Norden**: le stime *real-time* di output
   gap/prodotto potenziale sono inaffidabili, con revisioni grandi quanto il gap
   stesso, dovute soprattutto alla ristima del trend a fine campione. Classificare
   "ora" con un potenziale stimato su forecast a 5 anni incarna esattamente questo
   errore. (uh.edu/~oince/Output Gap Paper; ideas.repec.org restat 2002)
3. **Evidenza EWS**: usare input *forecast* peggiora la qualità del segnale di
   crisi rispetto ai dati *actual* più recenti.
   (link.springer.com/article/10.1007/s11079-019-09530-0)

**Conseguenza pratica.** Diverse "belle riduzioni del debito" attuali sono
*proiezioni*: Grecia −6,67, Cipro −4,68, Portogallo −2,94 pp/anno sono dominate
dal tratto forecast dello slope. Se il WEO rivede al ribasso quella traiettoria
(come storicamente accade nei paesi ad alto debito), la fase cambia
retroattivamente.

### 2.4 — [MEDIA] Il composite z-score cross-country: statistica fragile e non validata

- **Standardizzazione su singola data, N≈64.** Media e dev.std campionarie sono
  esse stesse instabili con N piccolo e distribuzioni fat-tailed/skewed
  (debito/PIL, inflazione). Il clipping a ±3 tampona l'outlier ma **non**
  impedisce che quell'outlier distorca media/std usate per *tutti* gli altri
  paesi. La letteratura suggerisce mediana/MAD come alternativa robusta.
  (OECD/JRC Handbook 2008; Saisana–Saltelli–Tarantola, JRSS-A 2005)
- **Pesi dei pillar arbitrari e non comparabili fra paesi.** 20/20/15/…/5 non
  hanno base empirica (l'OECD/JRC nota che l'equal/expert-weighting "può
  mascherare l'assenza di base statistica"). Peggio: i pesi sono
  **rinormalizzati sui pillar disponibili** (`dalio.py:359-362`), quindi un
  paese senza il pillar *banking* redistribuisce quel peso sugli altri →
  **il composite significa cose diverse per paesi diversi** e l'ordinamento
  della tabella Overview non è strettamente comparabile.
- **Indicatori correlati → doppio conteggio + compensabilità.** Più misure di
  leva nel pillar debt_cycle, più misure di crescita nel growth pillar gonfiano
  il segnale; l'aggregazione additiva permette compensazione (nel campione: un
  geopolitical z=0,68 compensa un debt_cycle z=−0,26). L'OECD chiede di
  correggere o giustificare esplicitamente la correlazione.
- **Potere discriminante basso.** Dev.std del composite = **0,30**, range
  [−0,79, +0,59]. Dopo clip+doppia media il segnale è compresso verso lo zero:
  gran parte delle differenze di ranking fra paesi **cadono entro il rumore**
  della standardizzazione single-date/small-N. Presentare una classifica
  ordinata dà un'impressione di precisione superiore all'informazione reale.

### 2.5 — [MEDIA] Classificatore a soglie: falsa precisione, niente isteresi

`classify_cycle_phase` è un albero di soglie crisp (debito >100, >130,
credit_gap >10, dsr percentile >0,80, …) senza **isteresi né banda di
incertezza**. Effetti:
- **Misclassificazione ai punti di svolta**: un paese oscilla di fase per
  variazioni marginali/di revisione dati anno su anno.
- **Ordine di precedenza carico di prior**: il check deleveraging (step 2)
  precede il check high-debt → Giappone/Grecia (debito alto ma calante) vanno
  in deleveraging, non in late-cycle. È una scelta di design legittima ma **non
  validata**, con conseguenze forti sulle etichette aggregate.
- **Contrazioni supply-driven non distinte da quelle debt-driven**: Kuwait e
  Qatar risultano `CONTRACTION` per tagli OPEC sul petrolio (crescita reale
  negativa), non per dinamiche di debito — ma la fase "contrazione" nel
  framework Dalio ha una lettura da ciclo del debito che qui non si applica.

### 2.6 — [ALTA, trasversale] Nessuna validazione out-of-sample

I test (`tests/test_pipeline.py:88-91`) verificano **solo che la pipeline giri**
(numero paesi, arità righe, freschezza forecast). **Non esiste alcun backtest**
che controlli se il classificatore avrebbe segnalato USA 2008, Grecia 2010,
Argentina 2018, ecc. È il difetto più serio dal punto di vista scientifico:

- La letteratura EWS è netta — anche modelli sofisticati hanno performance
  out-of-sample "piuttosto scarse", indicatori utili in una crisi falliscono
  nella successiva (Berg–Pattillo su KLR). (bis.org/publ/confer08m.pdf)
- La stessa DSA dell'IMF "lancia falsi allarmi (tipo I) e manca crisi (tipo II),
  ciascuno in circa un terzo dei casi", e la sofisticazione econometrica **non**
  migliora il potere predittivo su modelli a 3 variabili.
  (imf.org PP 2021/003)
- Il modello di Dalio stesso è criticato come **pattern-matching narrativo non
  falsificabile** (razor "troppo spesso", timeline mobili). Recensioni 2026 di
  *How Countries Go Broke* e analisi del track record (2008 azzeccata; 1981–82
  e 2015 mancate) sostengono che il valore predittivo/di asset-allocation è
  debole. (independent.org/tir/2026-spring/how-countries-go-broke;
  awealthofcommonsense.com/2025/03/predicting-a-financial-crisis)

Codificare un metodo intrinsecamente non-falsificabile in **etichette nette**
senza backtest ne amplifica il rischio: trasferisce a chi legge una fiducia che
il metodo non ha guadagnato empiricamente.

---

## 3. Cosa è invece corretto (da non toccare)

- **DSR letto come percentile della propria storia** (non livello assoluto):
  metodologicamente aderente a Dalio e alla realtà (20% è normale per NL/CH).
- **Debito come traiettoria pluriennale** (non variazione a 1 anno): giusto in
  linea di principio — il problema è *solo* la quota di forecast (§2.3).
- **Separazione esplicita composite (forza relativa) vs fase/regime (ciclo)**:
  la documentazione lo dichiara correttamente; il fix è renderlo vero anche
  nella presentazione (§4).
- **Flag di dati stale rispetto alla frontiera cross-country**: buona pratica di
  data quality.
- **Caveat qualitativi per SGP/ARG/HKG/NOR**: giusti — vanno solo elevati da
  nota di dettaglio a modificatori visibili negli aggregati.

---

## 4. Raccomandazioni (prioritizzate)

**P0 — chiudono difetti concreti, basso rischio**

1. **Costo del debito ≠ policy rate** (§2.2). Sostituire `nom_rate` con il
   rendimento sovrano ~10Y (o tasso implicito = spesa per interessi / stock di
   debito, entrambi già ottenibili da WEO/FRED). In assenza, usare policy +
   spread sovrano. Elimina l'appiattimento dell'area euro e la "beautiful"
   argentina spuria.
2. **Marcare le fasi forecast-driven** (§2.3). Calcolare la fase **anche solo
   sugli actual** (slope su [ry−5, ry]) e segnalare quando actual e
   actual+forecast divergono ("deleveraging *atteso*, non ancora *realizzato*").
   Minimo: mostrare la quota di punti forecast nello slope.
3. **Retrocedere "BEAUTIFUL/UGLY" a modificatore, non fase**, quando il calo del
   debito è guidato da inflazione a doppia cifra (gate su inflazione < soglia,
   es. 15%). Coerente con la definizione di Dalio (aggiustamento *bilanciato*).

**P1 — irrobustiscono la statistica**

4. **Standardizzazione robusta** (§2.4): mediana/MAD invece di media/std, o
   winsorizzazione prima dello z, per il composite cross-country.
5. **Comparabilità del composite**: non rinormalizzare i pesi sui pillar
   disponibili senza segnalarlo; oppure imporre una copertura minima di pillar
   e marcare i composite a copertura parziale come non pienamente comparabili.
6. **Sensitivity/uncertainty analysis sui pesi** (raccomandazione OECD/JRC):
   pubblicare il range del ranking di ogni paese al variare dei pesi entro
   bande plausibili. Se il rank è instabile, dichiararlo nel report.
7. **Allineare il four-box a Bridgewater** (§2.1): definire crescita e
   inflazione come **sorpresa rispetto al consenso/atteso** (es. actual vs
   ultima previsione WEO precedente per lo stesso anno), oppure — se si tiene
   l'output gap — **rimuovere la mappatura diretta sull'asset allocation
   Bridgewater** e presentarla come "regime descrittivo", non prescrittivo.

**P2 — validazione (il lavoro scientificamente decisivo)**

8. **Backtest storico** (§2.6): ricostruire il panel a vintage annuali e
   verificare quante crisi note (Reinhart-Rogoff/Laeven-Valencia banking &
   sovereign crises dataset) il classificatore avrebbe segnalato con ≥1 anno di
   anticipo, e il tasso di falsi allarmi. Riportare esplicitamente tipo I/II —
   anche un risultato mediocre è informazione onesta e batte l'assenza di dato.
9. **Isteresi/bande** nell'albero delle fasi per ridurre il flip-flop ai
   confini; output opzionale di una fase con "confidenza" invece che netta.
10. **Nota di limiti nel report**: la tab Methodology è ottima ma "vende" il
    metodo; aggiungere una sezione "Limiti e validità" che dichiari: composite =
    forza relativa non-predittiva, four-box non-Bridgewater (se resta output
    gap), fasi forecast-dependent, nessun backtest (finché §8 non è fatto).

---

## 5. Riferimenti

**Fonti primarie / istituzionali**
- Dalio, *Principles for Navigating Big Debt Crises* — principles.com/big-debt-crises; bridgewater.com/big-debt-crises
- Bridgewater, *The All Weather Story* — bridgewater.com/research-and-insights/the-all-weather-story
- OECD/JRC, *Handbook on Constructing Composite Indicators* (2008) — oecd.org (…/9789264043466-en.pdf)
- Saisana, Saltelli, Tarantola (2005), *Uncertainty and sensitivity for composite indicators*, JRSS-A — rss.onlinelibrary.wiley.com/doi/10.1111/j.1467-985X.2005.00350.x
- IMF WP, *Optimism Bias in Growth Forecasts* (2020) — imf.org/en/publications/wp/issues/2020/11/08/…-49804
- Orphanides & van Norden (2002), *The Unreliability of Output-Gap Estimates in Real Time*, REStat — ideas.repec.org/a/tpr/restat/v84y2002i4p569-583.html; uh.edu/~oince/Output Gap Paper.pdf
- IMF (2021), *Review of the Debt Sustainability Framework* — imf.org PP 2021/003
- BIS, early-warning-systems performance — bis.org/publ/confer08m.pdf

**Commentario / critiche**
- Independent Institute (2026), rec. *How Countries Go Broke* — independent.org/tir/2026-spring/how-countries-go-broke
- Risk parity 2022 — markovprocesses.com/blog/is-2022-all-bad-weather-for-risk-parity; caia.org 2022
- Track record Dalio — awealthofcommonsense.com/2025/03/predicting-a-financial-crisis; edge-forex.com; Fortune 2025–2026 (…heart-attack…, …big-cycle…)
- EWS con forecast — link.springer.com/article/10.1007/s11079-019-09530-0

*Metodo di questa revisione: analisi diretta di `dalio.py`/`make_dalio_report.py`
e dei parametri `settings.yaml`; lettura del report `dalio_report_20260708`
(64 paesi) per verificare i riscontri sui dati reali; ricerca online su fonti
primarie e letteratura peer-reviewed su indicatori compositi, output-gap
real-time, bias WEO ed early-warning systems.*

# Niese – sparesimulator for barn

Fanen **Niese** i finance-dashboardet (`templates/index.html`, `#tab-niese`) simulerer langsiktig månedssparing
til et barn med medium-høy til høy risiko. Alt regnes i nettleseren (vanilla JS + Chart.js). Ingenting hentes fra
internett og ingenting lagres i `finance.db` – oppsettet huskes bare i nettleserens `localStorage`
(`niese-settings-v1`). «Tilbakestill alt til standard» gir deg standardverdiene igjen.

Koden ligger i to blokker i `index.html`:

- `NIESE-ENGINE-START … NIESE-ENGINE-END` – ren beregningslogikk uten DOM (Cholesky, egenverdier, PRNG, simulering, statistikk).
- `── NIESE: UI ──` – skjema, validering, tabeller og grafer.

## Antakelsene

Standardverdiene er **antakelser, ikke prognoser**, og kan endres i UI-et.

| Aktivum | Forv. avk. | Volatilitet | ÅK | Annet |
|---|---|---|---|---|
| Globalt indeksfond | 7 % | 15 % | 0,2 % | |
| Emerging markets | 7,5 % | 20 % | 0,3 % | |
| Teknologi-indeks | 9 % | 25 % | 0,3 % | |
| USA-indeks | 7,5 % | 16 % | 0,2 % | |
| Globalt giret 1,25x | (som globalt) | (som globalt) | 0,4 % | gearing 1,25, 4 % lånekost på den lånte delen |
| Bitcoin | 10 % | 60 % | 0 % | 1 % kjøpskostnad |
| Rolig fond (glidebane) | 3 % | 4 % | 0,2 % | |

Korrelasjoner: globalt–USA 0,95, globalt–tech 0,85, globalt–EM 0,75, tech–USA 0,85, bitcoin–tech 0,4,
bitcoin–øvrige aksjer 0,3. Ikke oppgitt i spesifikasjonen, så dette er egne antakelser: EM–tech 0,70, EM–USA 0,70,
rolig fond 0,10 mot aksjer og 0,05 mot bitcoin.

Modellen:

- **Månedlige lognormale avkastninger.** `ln(1 + r) ~ N(m, s²)` med `s = σ/√12`. Standardtolkningen er at «forventet
  avkastning» er det aritmetiske snittet: `m = (ln(1 + μ) − σ²/2) / 12`. Da blir den typiske (median) årlige veksten
  lavere enn μ – for bitcoin (10 % / 60 %) omtrent −8 % per år. Velg «Median (geometrisk)» hvis du heller mener μ som
  medianvekst (`m = ln(1 + μ)/12`).
- **Rekkefølge hver måned:** glidebane-rebalansering → innskudd (minus kjøpskostnad) → avkastning → ÅK/12.
- **Ingen rebalansering mellom aktiva.** Hvert aktivum vokser for seg, bortsett fra flyttingen til rolig fond i glidebanen.
- **Kjøpskostnad** i kr trekkes på hvert månedlige innskudd i aktivumet. En kostnad i % trekkes også når glidebanen selger aktivumet.
- **Glidebane:** de siste N årene (standard 4) øker målandelen i rolig fond lineært måned for måned opp til sluttandelen
  (standard 70 %). Både nye innskudd og eksisterende beholdning flyttes, pro rata fra de risikable aktivaene.
- **«Opprinnelig»-porteføljen** har to globale fond fra ulike leverandører med samme parametre. Det er matematisk det
  samme som ett globalt fond med 2/3 av beløpet, så det legges inn slik.
- **Krakk før 18:** de siste 12 månedene erstattes av et jevnt fall (standard −40 % for aksjer og −70 % for krypto over året).
  Rentefond får vanlig tilfeldig avkastning. Det girede fondet faller mer enn det underliggende.
- **Maks fall** regnes på kronebeløpet i kontoen (innskudd inkludert). «Medianscenarioet» er snittet av banene som ender
  mellom 45. og 55. persentil, og «10.-persentilen» snittet av banene mellom 5. og 15. persentil. Én enkelt bane er for tilfeldig til å si noe.
- **Felles tilfeldige tall:** alle varianter og porteføljer bruker samme seed, så forskjellene kommer av porteføljen og ikke av flaks.
- **Ikke med:** skatt (f.eks. ASK), inflasjon, tykke haler, og korrelasjoner som endrer seg over tid. Alle beløp er nominelle kroner.

## Hvorfor korrelasjon betyr mye for tech + bitcoin

Spredning reduserer risiko bare når aktivaene *ikke* faller samtidig. Porteføljens varians er
`Σ wᵢwⱼσᵢσⱼρᵢⱼ`, så et høyt ρ mellom to aktiva med høy volatilitet (tech 25 %, bitcoin 60 %) gir mye mer samlet
svingning enn det samme med lavt ρ. Et teknologifond er 0,85 korrelert med et globalt fond, så «Tech-yolo» får mest
av den samme aksjerisikoen, bare forsterket. Bitcoin ser lite korrelert ut i rolige perioder (0,3–0,4), men i kriser
har korrelasjonen mellom risikoaktiva historisk steget. Da forsvinner spredningseffekten akkurat når du trenger den.
Prøv å sette bitcoin–tech til 0,7 i matrisen og se hvordan 10.-persentilen og sannsynligheten for å ende under innskutt
beløp endrer seg.

Matrisen må være positiv semidefinitt (ellers finnes det ingen verden der alle korrelasjonene gjelder samtidig).
UI-et sjekker dette med egenverdier. Hvis matrisen er ugyldig, peker det ut trioen som bryter, og viser hvilket
intervall den tredje korrelasjonen må ligge i.

## Hvorfor gearing kan tape seg inn over tid

Et 1,25x-fond rebalanseres hele tiden. Det gir 1,25 × *hver måneds* avkastning, ikke 1,25 × avkastningen over 18 år.
Faller markedet 20 % og stiger så 25 %, er det ugirede fondet tilbake på start. Det girede fondet faller 25 % og stiger
31,25 %, og ender på −1,6 %. Dette *volatility drag* er omtrent `½ · (L² − L) · σ²` per år (≈ 0,35 % for L = 1,25,
σ = 15 %). I tillegg kommer lånekostnaden på den lånte delen (`(L − 1) × 4 % = 1 %`) og høyere ÅK (+0,2 %).
Til sammen er det mer enn ekstraavkastningen gearingen gir i et typisk scenario.

Resultat-fanen viser dette direkte. Den simulerer 1 kr investert ved start i det girede og det ugirede fondet med de samme
tilfeldige tallene, og sammenligner giret median mot «1,25 × ugiret median», både per år og totalt over perioden. Med
standardantakelsene taper giret fond ca. 1,5 prosentpoeng per år mot den naive forventningen, og slår det ugirede fondet
bare i under halvparten av scenarioene. Den deterministiske beregningen har ingen svingninger, så der ser gearing
bedre ut enn den er.

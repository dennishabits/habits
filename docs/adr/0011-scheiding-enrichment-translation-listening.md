# 0011 — Scheiding van enrichment, translation en listening

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `pipeline`, `architectuur`

## Context

In vroege ontwerpen was de verleiding aanwezig om meerdere verantwoordelijkheden in één service te combineren (bijv. data ophalen én transformeren én doorsturen). Een service met meerdere verantwoordelijkheden is moeilijker te testen, te debuggen en te vervangen.

## Beslissing

Elke service heeft één verantwoordelijkheid. Enrichment haalt externe data op. Translation transformeert naar generiek formaat. Listening stuurt data naar ontvangende partijen. Deze verantwoordelijkheden worden nooit gecombineerd in één service.

## Gevolgen

- Strikte scheiding maakt elke service onafhankelijk vervangbaar zonder impact op de rest van de pipeline.
- Debugging is eenvoudiger: een defect kan snel worden gelokaliseerd tot één verantwoordelijkheidslaag.
- Het aantal services groeit lineair met het aantal externe systemen (bijv. `acuity-enricher`, `sportivity-enricher`). Dit is een bewuste trade-off. *[afgeleid]*
- Zie ook ADR-0010 (event-driven): de scheiding werkt alleen omdat services via Pub/Sub zijn ontkoppeld.

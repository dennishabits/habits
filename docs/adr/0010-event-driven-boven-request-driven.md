# 0010 — Event-driven boven request-driven

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `pipeline`, `architectuur`

## Context

Een pipeline waarbij services elkaar direct via HTTP aanroepen koppelt ze aan elkaars beschikbaarheid. Een traag of uitgevallen service blokkeert de hele keten. Bovendien maakt directe koppeling het lastig om nieuwe consumers toe te voegen zonder bestaande services aan te passen.

## Beslissing

Alle communicatie tussen services verloopt via Pub/Sub topics, niet via directe HTTP-aanroepen tussen services.

## Gevolgen

- Services zijn onafhankelijk van elkaars beschikbaarheid — een uitgevallen service blokkeert geen andere services.
- Nieuwe consumers (listeners) kunnen worden toegevoegd door een nieuwe subscriber toe te voegen op een bestaand topic, zonder de producerende service te wijzigen.
- Pub/Sub biedt at-least-once delivery; services moeten idempotent zijn bij duplicate events. *[afgeleid]*
- De DLQ (dead-letter queue) vangt berichten op die meermaals falen en maakt handmatige of geautomatiseerde retry mogelijk.

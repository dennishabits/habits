# 0005 — Rapportagelaag voor de eigenaar is een open ontwerpkeuze

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `strategie`, `open`

## Context

De eigenaar (koper van Habits, zie ADR-0002) heeft een andere informatiebehoefte dan de manager: retentie en ROI op locatieniveau, niet operationele details. Te vroeg specificeren leidt tot bouwen voor een aanname die niet door praktijkervaring is gevalideerd.

## Beslissing

De eigenaar heeft mogelijk een andere rapportagelaag nodig dan de manager. Dit is nog niet uitgewerkt en wordt bepaald op basis van wat we leren in de praktijk met Basecamp Fitness als testomgeving.

## Gevolgen

- Geen eigenaar-specifieke rapportagelaag is gebouwd totdat praktijkdata de behoefte valideert.
- Basecamp Fitness (primaire testomgeving) levert de input om deze keuze te maken. *[afgeleid]*
- Dit is een bewust opengelaten ontwerpkeuze — agents die een rapportagefeature voor de eigenaar ontwerpen moeten dit ADR raadplegen en de status controleren voordat ze verder gaan. *[afgeleid]*

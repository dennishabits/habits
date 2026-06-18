# 0006 — Conversational UX als richting

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `strategie`, `ux`

## Context

Adoptie is de grootste risicofactor bij het introduceren van nieuwe tooling in een gym-omgeving. Medewerkers zijn geen software-professionals en hebben weinig tijd om een nieuw systeem te leren. De vraag was welk interactiemodel de adoptiedrempel het laagst houdt.

## Beslissing

De primaire interactie verloopt via gesprekken — nu via Slack. Managers en medewerkers hoeven geen nieuw systeem te leren. Hoe dit er voor de eigenaar uitziet is een open ontwerpkeuze (zie ADR-0005).

## Gevolgen

- Slack is de primaire interface voor medewerkers en managers. Nieuwe features die interactie vereisen, worden standaard via Slack aangeboden. *[afgeleid]*
- De `slack-agent` en `habits-coach-reply` services zijn de primaire interactielaag.
- Een interface die zich gedraagt als een gesprek verlaagt de adoptiedrempel maximaal.
- De eigenaar-interface is nog niet bepaald — conversational UX is wel de richting, de concrete implementatie voor de eigenaar is open. *[afgeleid]*

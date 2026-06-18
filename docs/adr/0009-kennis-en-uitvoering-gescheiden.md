# 0009 — Kennis en uitvoering zijn gescheiden

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `strategie`, `ai`, `architectuur`

## Context

Als domeinkennis (welke interventie werkt, welke timing effectief is) in code is ingebakken, vereist elke aanpassing een deployment. Dit maakt het systeem traag in leren en afhankelijk van een ontwikkelaar voor elke inhoudelijke wijziging.

## Beslissing

Domeinkennis over wat werkt — welke interventie past bij welk ledenprofiel, welke timing effectief is — staat los van de code die het uitvoert. Kennis leeft in data en documenten. Code implementeert.

## Gevolgen

- Prompts, taakconfigs en tenant-instellingen leven in Firestore — aanpasbaar zonder deployment (zie ADR-0014).
- Het systeem kan leren en aanpassen zonder codewijziging. Dit is de voorwaarde voor een zelflerend systeem.
- ADR-documenten en Firestore-configuratie zijn de primaire kennisopslag; agenten lezen dit als context. *[afgeleid]*
- Code is uitvoering; als kennis en uitvoering door elkaar lopen, wordt het systeem moeilijker te verbeteren en te auditen. *[afgeleid]*

# 0008 — All-in op AI

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `strategie`, `ai`

## Context

De waardepropositie van Habits is continuous improvement. Dat is alleen schaalbaar als het systeem zelf verbetert — niet als elke verbetering handmatig wordt doorgevoerd door een ontwikkelaar of operator.

## Beslissing

Habits wordt gebouwd als een AI-first systeem. Het systeem leert van gedrag, verbetert zichzelf via feedbackloops, en heeft over tijd minder menselijke sturing nodig — niet meer. Handmatige configuratie is een tijdelijke oplossing, nooit een eindtoestand.

## Gevolgen

- Agents (zoals `slack-agent`) zijn geen bijzaak maar een kernonderdeel van het product.
- De architectuur is erop gericht dat kennis in data en documenten leeft (zie ADR-0009), zodat het systeem kan leren zonder deployment. *[afgeleid]*
- Een systeem dat afhankelijk blijft van menselijke configuratie schaalt niet naar meerdere tenants.
- Evaluatielaag en goal registry zijn vereiste bouwstenen — zonder meetbare doelen kan het systeem niet leren. (Zie BACKLOG.md: *Evaluatielaag*.) *[afgeleid]*

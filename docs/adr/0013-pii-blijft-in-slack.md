# 0013 — PII blijft in Slack

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `privacy`, `security`

## Context

Medewerkers werken met persoonsgegevens (namen, e-mailadressen, gedragsdata van leden) via Slack. De vraag was welke systeemdelen deze data mogen verwerken en opslaan.

## Beslissing

Persoonsgegevens worden getoond in Slack maar nooit opgeslagen in Firestore-geschiedenis of meegegeven aan Gemini als context.

## Gevolgen

- Firestore `agent_sessions` en `error_log` bevatten geen namen, e-mailadressen of andere identificerende ledendata als volledige profielen.
- Gemini-aanroepen bevatten geen ruwe persoonsgegevens (zie ook ADR-0012).
- Slack is het operationele kanaal — persoonsgegevens zijn zichtbaar voor medewerkers die ermee werken, maar verlaten die grens niet via systeemdelen.
- Bij implementatie van nieuwe features: als een gegeven identificerend is op individueel niveau, hoort het niet in Firestore-history of LLM-context. *[afgeleid]*

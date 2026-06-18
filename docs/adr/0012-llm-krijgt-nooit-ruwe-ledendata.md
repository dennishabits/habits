# 0012 — LLM krijgt nooit ruwe ledendata

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `ai`, `privacy`, `bigquery`

## Context

LLM-aanroepen sturen data naar een externe dienst (Google Gemini API). Ruwe ledenprofielen bevatten persoonsgegevens die de privacygrens van het systeem niet mogen passeren. Bovendien levert deterministische SQL betere en controleerbare analyse dan LLM-redenering over ruwe tabellen.

## Beslissing

BigQuery handelt alle analyse af via deterministische SQL. Gemini ontvangt alleen geaggregeerde samenvattingen — nooit individuele ledenprofielen of persoonsgegevens.

## Gevolgen

- Analyses (bijv. HHI, taakprestaties) worden berekend in BigQuery views; Gemini ontvangt het resultaat, niet de brondata.
- In de staged investigation (Stage B/C) worden uit externe API-responses alleen booleans en identifiers geëxtraheerd — niet de ruwe profieldata — voordat die aan Gemini worden doorgegeven.
- PII verlaat het systeem niet via Gemini-context. Zie ook ADR-0013 (PII blijft in Slack).
- Deterministische SQL-logica in views is auditeerbaar en versioneerbaar; LLM-redenering over ruwe data niet. *[afgeleid]*

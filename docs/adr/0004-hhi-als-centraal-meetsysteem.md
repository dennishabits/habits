# 0004 — HHI als centraal meetsysteem

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `strategie`, `product`, `hhi`

## Context

Zonder een gedeeld meetsysteem is vergelijking tussen locaties en over tijd onmogelijk. Coaching en rapportage zouden elk een eigen interpretatie van "retentiegezondheid" hanteren.

## Beslissing

De Habit Health Index (HHI) is het primaire instrument om retentiegezondheid per lid en per locatie inzichtelijk te maken. Alle coaching en rapportage is hierop gebaseerd. De HHI meet over drie journeys (Onboarding, Member Maintenance, Reactivation) en drie dimensies (Retentiescore, Customer Success Score, Progressiescore).

## Gevolgen

- Vergelijking tussen locaties en over tijd is mogelijk via een gemeenschappelijke meetlat.
- Alle coaching- en rapportage-features bouwen op HHI-data; nieuwe features die dit niet doen passen niet in de productarchitectuur. *[afgeleid]*
- De HHI-berekening leeft in BigQuery (`hhi_week`, `hhi_opportunities` views) — wijzigingen aan de definitie vereisen view-updates en kunnen historische vergelijkingen verstoren. *[afgeleid]*
- Zonder gemeenschappelijke meetlat is continuous improvement niet schaalbaar naar meerdere tenants.

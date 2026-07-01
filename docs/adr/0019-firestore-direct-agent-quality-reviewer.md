# 0019 — Firestore direct voor agent-quality-reviewer — geen BigQuery-sync

- **Status**: Accepted
- **Datum**: 2026-07-01
- **Tags**: `architectuur`, `agent`, `firestore`, `bigquery`

## Context

De `agent-quality-reviewer` analyseert `error_log` en `agent_sessions` om structurele problemen in classificatie en diagnose te detecteren. Het gangbare patroon binnen Habits is dat analytische data naar BigQuery gaat (`raw_events`, `customers`, etc.) en daar via SQL wordt geaggregeerd.

Voor de kwaliteitsreview zou dat betekenen: een nachtelijke sync van `error_log` → BigQuery tabel, gevolgd door SQL-aggregaties in de reviewer. Dat is het standaardpatroon, maar het introduceert een extra GCP-resource, een sync-job, en een duplicaat van data die al volledig in Firestore staat.

## Beslissing

Geen BigQuery-sync. De `agent-quality-reviewer` laadt `error_log`- en `agent_sessions`-documenten direct via de Firestore Python-client en aggregeert in Python.

**Motivatie**: het huidige volume rechtvaardigt de infrastructuurinvestering niet. Bij één actieve tenant en een fractie van de verwerking die `error_log` bereikt, past het laden van alle relevante documenten ruim binnen een Cloud Function-aanroep. Het patroon is bewezen in `count_pipeline_drop_occurrences` in `slack-agent/main.py`. Een nachtelijke sync zou data dupliceren die al in Firestore beschikbaar is, enkel om SQL-aggregatie mogelijk te maken.

**Grens**: als het volume groeit naar meerdere actieve tenants met honderd of meer `error_log`-documenten per week, is heroverwegen aangewezen. Signaal: Python-aggregatie duurt langer dan 30 seconden of de memory-limit van de Cloud Function wordt geraakt.

## Gevolgen

- Geen nieuw GCP-resource nodig (geen BigQuery tabel, geen sync-job, geen extra scheduler).
- Python-aggregatie is minder declaratief dan SQL — complexe joins zijn onhandiger, maar de benodigde aggregaties (`COUNT` per categorie, `AVG` tijdsverschil) zijn triviaal in Python.
- Alle benodigde velden zijn direct beschikbaar in Firestore (`signal_type`, `root_cause_category`, `confidence`, `reopened`, `created_at`, `resolved_at`) — geen transformatielaag nodig. *[afhankelijk van schema-uitbreiding `error_log`, zie BACKLOG.md: Wekelijkse agent-kwaliteitsreview]*
- Bij toekomstige schaal: introduceer een nachtelijke sync en vervang de Python-aggregatie door SQL-views naar het bestaande `gym_analytics`-patroon. Dat is een additieve stap die geen bestaande code breekt.

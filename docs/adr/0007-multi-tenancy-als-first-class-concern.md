# 0007 — Multi-tenancy als first-class concern

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `multi-tenancy`, `architectuur`

## Context

Habits is een SaaS-product voor meerdere gymlocaties of ketens. Compromissen in tenant-isolatie die vroeg worden gemaakt zijn later vrijwel onmogelijk te repareren zonder data-migraties en downtime.

## Beslissing

Elke tenant — locatie of keten — heeft volledig geïsoleerde data, configuratie en doelen. Er is geen gedeelde state tussen tenants. Nieuwe tenants voegen data toe zonder architectuurwijziging.

## Gevolgen

- Alle Firestore-documenten, BigQuery-rijen en Pub/Sub-berichten zijn gekeyed op `tenant_id`. Cross-tenant queries bestaan niet.
- Tenant-configuratie (Slack-token, Acuity-credentials, enabledServices) leeft in `tenants/{tenant_id}` in Firestore.
- Nieuwe tenants kunnen worden toegevoegd door een Firestore-document aan te maken — geen code- of infrastructuurwijziging nodig. *[afgeleid]*
- De data-isolatie is een harde grens; elke agent of service die dit doorbreekt introduceert een privacy- en compliancerisico. *[afgeleid]*

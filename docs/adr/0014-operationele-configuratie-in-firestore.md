# 0014 — Operationele configuratie leeft in Firestore, niet in code

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `architectuur`, `firestore`

## Context

Als prompts, taakconfigs of tenant-instellingen in code zijn ingebakken, vereist elke aanpassing een deployment. Dit contrasteert met de kernbeslissing dat kennis en uitvoering gescheiden zijn (zie ADR-0009) en dat het systeem zichzelf moet kunnen aanpassen.

## Beslissing

Prompts, task configs en tenant-specifieke instellingen worden beheerd via Firestore. Aanpassingen hieraan vereisen geen deployment.

## Gevolgen

- Tenant-onboarding vereist geen codewijziging — een nieuw Firestore-document in `tenants/{tenant_id}` is voldoende.
- Prompts zijn aanpasbaar door de operator zonder developer-betrokkenheid.
- Configuratiewijzigingen zijn niet versioned in git. Als traceerbaarheid van config-historiek nodig wordt, is dat een aanvullende maatregel. *[afgeleid]*
- Zie ook: `tenants/{tenant_id}` bevat `slackToken`, `acuityConfig`, `sportivityToken`, `customerio`, `enabledServices`, `tasks` en `prompt_overrides`.

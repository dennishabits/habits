# 0018 — Orchestrator + write executor + tool registry + process registry als agent-architectuur

- **Status**: Accepted
- **Datum**: 2026-07-01
- **Tags**: `architectuur`, `ai`, `agent`, `multi-tenancy`

## Context

De `slack-agent` hardcodeert tool-specifieke logica inline: `_stage_a_for_task`, `investigate_stage_a_acuity`, `investigate_stage_b_identity`, en BigQuery-queries met `webhook_source = 'acuity'`. Elke nieuwe tool vereist nieuwe Python-branches in meerdere functies. Dit schendt ADR-0009 (kennis en uitvoering zijn gescheiden): domeinkennis over hoe een extern systeem te bevragen hoort niet in code te zitten.

Tegelijk deelt de orchestrator een service account met brede read-rechten op Acuity, Sportivity en Customer.io voor alle taken — ook voor de schrijfacties (`complete_task`, `publish_correction_event`) die pas na goedkeuring mogen plaatsvinden. Dat is een te breed credential-oppervlak.

Een eerder geopperde framing van "4 agents" was onjuist. Slechts één component redeneert (classificeert en beslist); de rest is data of een toegangscontrolesplitsing.

## Beslissing

De agent-laag bestaat uit **2 services** en **2 configuratieregistries**:

| Component | Type | Verantwoordelijkheid |
|---|---|---|
| **Orchestrator** | Service (huidig: `slack-agent`) | LLM intent-classificatie, process dispatch, read/diagnostisch uitvoering. Geen tenant-logica in code — tenant-context per request via `get_tenant_by_team_id`. |
| **Write executor** | Service (nieuw) | Schrijfacties met beperkte credentials, geïnvoceerd na goedkeuring. |
| **Tool registry** | Firestore `tool_registry/{tool_id}` | Declaratieve definitie per extern systeem: auth-patroon, capabilities (`verify_existence`, `fetch_profile`, `apply_action`). Shared definitie; credentials + activering per tenant in `tenants/{tenant_id}`. |
| **Process registry** | Firestore `process_registry/{process_id}` | Declaratieve definitie per procestype: stappen, intent, kanaalconfiguratie. Shared definitie; activering + kanaalconfiguratie per tenant via `slack_agent_channels`. |

**Vuistregel**: type-definitie = shared; credentials / activering / instantie = tenant-geïsoleerd. Geen gedeelde state tussen tenants.

Het schema voor beide registries wordt afgeleid van de 3 bestaande integraties (Acuity, Sportivity, Customer.io) — niet van een speculatief universeel framework.

## Gevolgen

- Een nieuwe tool toevoegen aan de stack vereist een registry-entry, geen code-wijziging in de orchestrator.
- De write executor krijgt minimale credentials — het principe of least privilege wordt hersteld.
- Stage C (pipeline trace) kan pas tool-agnostisch worden als trace-ID-propagatie bestaat; dit is een expliciete pre-conditie.
- De evaluatielaag past config/prompts aan op basis van gemeten uitkomsten — schrijft nooit enricher/translator code. Autonome verbetering ≠ autonome stack-uitbreiding.
- Bestaande `slack_agent_channels` / `get_channel_behavior` en `tenants/{tenant_id}` patronen blijven ongewijzigd — de registries zijn additioneel, geen vervanging.
- `PIPELINE_STAGES` en `_search_stage_logs` in `slack-agent/main.py` zijn vermoedelijk dead code uit het vroegere log-walking approach. Verifieer en verwijder conform de standaardprocedure (beschrijven + bevestigen) voordat de refactor begint. *[afgeleid]*

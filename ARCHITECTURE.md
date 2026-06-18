> Modus: Reference + Explanation — systeemstructuur, schemas en standaarden (Reference); ontwerprationale via ADRs (Explanation). Primaire laadcontext voor agents.

# Habits — Architecture

## Overzicht

Habits is gebouwd op een event-driven microservices-architectuur op Google Cloud Platform. Elke service heeft één verantwoordelijkheid. Enrichment (data ophalen bij externe APIs) is strikt gescheiden van translation (data transformeren naar generiek formaat) en van listening (data versturen naar ontvangende partijen).

**GCP project**: `solid-future-452906-a2`
**Regio**: `europe-west1`
**Runtime**: Cloud Functions Gen2, Python 3.12

---

## Agent-kennisarchitectuur

Habits is een AI-first systeem. Agents die het systeem doorontwikkelen of bedienen hebben drie typen kennis nodig. Elk document in dit project valt onder één van deze categorieën:

| Type | Functie | Documenten |
|---|---|---|
| **Grounding** | Wat is het systeem, voor wie, waarom zo gebouwd | `BUSINESS.md`, `ARCHITECTURE.md` |
| **Policy** | Hoe opereren agents binnen Habits — beslissingsbevoegdheid, escalatieregels, standaarden | `AGENT.md` *(nog aan te leggen)* |
| **Memory** | Doelen, metingen, wat heeft gewerkt — bijgehouden door de evaluatielaag | `GOALS.md` *(nog aan te leggen)*, BigQuery logs |

Een agent die één van deze drie typen mist heeft een blinde vlek die zich op onverwachte momenten manifesteert.

---

## Pipeline

```
Webhooks (Acuity / Sportivity / Customer.io / Leadform)
    ↓
webhook-dispatcher
    ↓
{source}-enricher  →  {source}-translations (Pub/Sub)
    ↓
{source}-translator  →  events (Pub/Sub)
    ↓
{output}-listener
```

Wanneer verrijking niet nodig is, publiceert de dispatcher direct naar `{source}-translations`.

---

## Services

### Inkomende laag

| Service | Trigger | Verantwoordelijkheid |
|---|---|---|
| `webhook-dispatcher` | HTTPS webhook | Ontvangt alle inkomende webhooks, routeert naar juiste Pub/Sub topic |

### Enrichment

| Service | Input topic | Output topic | Verantwoordelijkheid |
|---|---|---|---|
| `sportivity-enricher` | `sportivity-enrichments` | `sportivity-translations` | Haalt klant- en lidmaatschapsdata op bij Sportivity API |
| `acuity-enricher` | `acuity-enrichments` | `acuity-translations` | Haalt beschikbaarheidsdata op bij Acuity API |

### Translation

| Service | Input topic | Output topic | Verantwoordelijkheid |
|---|---|---|---|
| `sportivity-translator` | `sportivity-translations` | `events` | Vertaalt Sportivity-data naar generiek eventformaat |
| `crm-translator` | `crm-translations` | `events` | Routeert CRM-taken op email vs. customer ID; unwrapt CIO `payload.payload` double-nesting |
| `customerio-listener` | Customer.io webhook | `events` | Verwerkt uitgaande CIO-berichten; converteert epoch timestamps |

### Taakverwerking

| Service | Input | Verantwoordelijkheid |
|---|---|---|
| `task-scheduler` | `tasks` topic | Availability-gate via Acuity API voordat taken Slack bereiken |
| `slack-listener` | `events` topic | Rendert CRM-taken in Slack `#taken`; verwerkt taakcompletion en deduplicatie |
| `task-reminder` | Cloud Scheduler | Stuurt digest-herinneringen voor verlopen/onvoltooide taken |

**Reminder-schema**: ma–vr 11:00 en 16:30, za 11:00 (Amsterdam tijd)

### Rapportage & coaching

| Service | Trigger | Verantwoordelijkheid |
|---|---|---|
| `team-report` | Cloud Scheduler (`team-report-ochtend` 11:00, `team-report-middag` 20:00) | Queriet BigQuery views, roept Gemini aan, post management- en medewerkersrapporten naar Slack `#teamrapporten` |
| `habits-coach-reply` | Slack event | AI-coaching reply op basis van HHI-data en Firestore sessiestate |
| `habits-coach-weekly` | Cloud Scheduler | Genereert wekelijkse coaching-sessie op basis van HHI-opportunities |
| `coaching-listener` | `events` topic | Verwerkt coaching-gerelateerde events |

### Synchronisatie

| Service | Trigger | Verantwoordelijkheid |
|---|---|---|
| `bigquery-refresher` | `schedules` topic (nachtelijk) | Synchroniseert BigQuery klantdata → Customer.io |
| `customerio-slack-logger` | `events` topic | Logt alle uitgaande CIO-berichten naar Slack; filtert op `webhook_source == "customerio"` |

---

## Pub/Sub topics

| Topic | Doel |
|---|---|
| `events` | Generiek event-kanaal; alle downstream consumers luisteren hier |
| `tasks` | CRM-taken richting task-scheduler |
| `crm-translations` | Vertaalde CRM-events |
| `sportivity-enrichments` | Ruwe Sportivity-webhooks |
| `sportivity-translations` | Verrijkte Sportivity-events |
| `acuity-enrichments` | Ruwe Acuity-webhooks |
| `acuity-translations` | Verrijkte Acuity-events |
| `schedules` | Geplande taken (BigQuery-refresh, reminders) |
| `slack-interactions` | Slack button-interacties |

---

## BigQuery

**Dataset**: `gym_analytics`

### Tabellen

| Tabel | Inhoud |
|---|---|
| `customers` | Klantprofielen gesynchroniseerd vanuit Sportivity |
| `subscriptions` | Lidmaatschappen inclusief status, start- en einddatum |
| `suspensions` | Pauzes op lidmaatschappen |
| `raw_events` | Alle events die door de pipeline stromen (append-only) |
| `hhi_week` | Wekelijkse HHI-scores per lid |
| `hhi_opportunities` | Gesignaleerde retentiekansen per lid |

### Views

| View | Inhoud |
|---|---|
| `task_performance` | Taakcompletion, responstijd en uitval gereconstrueerd uit `raw_events`; gebruikt `effective_action_type` met backward-compatible fallback op `task_type` |
| `appointments` | Afspraken met duur, medewerker en of er een vervolgafspraak is gepland; followup-match via `LIKE '%fitcheck%'` voor activiteitnaamvarianten |
| `active_subscriptions` | Actieve lidmaatschappen op dit moment |
| `weekly_facts` | Wekelijkse operationele feiten per locatie |
| `sales_funnel_analysis` | Conversie van lead naar lidmaatschap per stap |

---

## Firestore

**Collections**

| Collectie | Inhoud |
|---|---|
| `tenants` | Slack bot-token, Acuity-configuratie per tenant |
| `slack_messages` | Opgeslagen Slack-berichten voor deduplicatie en state |
| `coaching_sessions` | Actieve en historische coaching-sessies per manager |
| `session_locks` | Vergrendelingen tijdens actieve sessies |
| `config` | Prompt-configuraties (`habits_coach_prompt`, `team_report_prompt` met `management_prompt` en `employee_prompt`) |

---

## Slack

| Kanaal | ID | Gebruik |
|---|---|---|
| `#taken` | `C0ATAT7UTE0` | CRM-taken voor medewerkers |
| `#bezoekers` | `C09CGLHBG6N` | Bezoekersregistratie |
| `#ledenadministratie` | `C010PNUAZP1` | Ledenadministratie |
| `#leads` | `C654VMGG7` | Inkomende leads |
| `#coaching` | `C0AJT9SKJ0J` | AI-coaching berichten |
| `#teamrapporten` | `C0B7NK3240K` | Management- en medewerkersrapporten |
| `#cio-log` | `C0B7T97GZ55` | Log van uitgaande Customer.io berichten |

---

## Externe integraties

| Systeem | Gebruik |
|---|---|
| **Sportivity** | Ledenadministratie — bron van klant- en lidmaatschapsdata |
| **Acuity** | Afsprakenbeheer — beschikbaarheidscheck voor taakplanning (appointment type `93522051`) |
| **Customer.io** | Marketing automation — campagnes, journeys, Liquid-templates |
| **Gemini** | LLM voor coaching en rapportage via `google-genai` SDK met `GEMINI_API_KEY`; werkend model: `gemini-2.5-flash` |

---

## Taakschema

Taken hebben een generiek schema zonder taaktype-conditionals. `action_type` staat op root-niveau van de payload en bepaalt welk completion-event de taak afsluit.

```
subject          — het lid of de lead
task_title       — titel van de taak
details          — array van {label, value, bold?}
task_icon        — emoji
task_label       — categorie-label
task_link        — link naar extern systeem
note             — optionele toelichting
visible          — false = opgeslagen in Firestore/BigQuery maar geen Slack-output
action_type      — bepaalt completion-logica: 'contact', 'appointment', 'subscription', 'review'
```

**Taaktypen** (vanuit medewerker-perspectief):

| Taaktype | Omschrijving | action_type | Completion event |
|---|---|---|---|
| `prospect_call` | Bel potentiële nieuwe klant | `contact` | `order` of `subscription_update` status `new` |
| `member_call` | Bel bestaand lid | `contact` | `appointment` status `new` |
| `member_talk` | Bezoekerstaak — bezoeker aanspreken voor FitCheck | `appointment` | `appointment` status `new` |
| `member_admin` | Administratieve verwerking (nieuw lid of opzegging) | `subscription` | `subscription_update` status `new` of `cancel` |
| `fitcheck` | FitCheck afspraak inplannen | `appointment` | `appointment` status `new` |
| `evaluation` | Evaluatie proefweek inplannen | `appointment` | `appointment` status `new` |
| `followup_appointment` | Vervolgafspraak bewaken (invisible) | `appointment` | `appointment` status `new` |

**Backward compatibility**: als `action_type` ontbreekt in de payload, leidt de `task_performance` view `effective_action_type` af uit `task_type` via een vaste mapping.

---

## Ontwikkelprincipes

- **Gen2 Cloud Functions** met `@functions_framework.cloud_event` decorator voor alle Pub/Sub-functies
- **Scheiding van verantwoordelijkheden**: enrichment ≠ translation ≠ listening
- **LLM krijgt nooit ruwe ledendata**: BigQuery handelt alle analyse af via deterministische SQL; Gemini krijgt alleen geaggregeerde samenvattingen
- **PII blijft in Slack**: nooit in Firestore-geschiedenis of Gemini-context
- **Altijd lowercase emails** voor Firestore-lookups en BigQuery-joins
- **Acuity tijden zijn Amsterdam lokaal** — altijd `AMSTERDAM_TZ`, nooit `timezone.utc`
- **Logging-standaard**: INPUT → ENRICHMENT_{SOURCE}_{TYPE} → TO_{TOPIC} in JSON
- **Foutafhandeling**: errors gelogd én gepubliceerd naar `events` topic met `email: dennis@habits.fit`; foutnotificaties gaan naar Slack user `U158QLHEF`
- **Gemini SDK**: gebruik `google-genai` met `GEMINI_API_KEY` — niet Vertex AI; `europe-west1` heeft geen beschikbare Gemini modellen via Vertex
- **Deployment**: `gcloud functions deploy [name] --gen2 --runtime=python312 --region=europe-west1 --source=. --entry-point=[function] --trigger-topic=[topic] --project=solid-future-452906-a2`
- **GEMINI_API_KEY ophalen bij deployment**: `--set-env-vars GEMINI_API_KEY=$(gcloud functions describe habits-coach-reply --gen2 --region=europe-west1 --project=solid-future-452906-a2 --format="value(serviceConfig.environmentVariables.GEMINI_API_KEY)")`

---

## Kwaliteitseisen

| Kwaliteitsdoel | Omschrijving | Criterium |
|---|---|---|
| Tenant-isolatie | Geen gedeelde state tussen tenants | Alle Firestore-documenten, BigQuery-rijen en Pub/Sub-berichten zijn gekeyed op `tenant_id`; geen cross-tenant queries |
| Service-verantwoordelijkheid | Elke service heeft één verantwoordelijkheid | Enrichment ≠ translation ≠ listening — nooit gecombineerd in één service |
| PII-grens | Persoonsgegevens verlaten de Slack-context niet | PII staat nooit in Firestore-history, BigQuery `raw_events` als geïdentificeerd profiel, of Gemini-context |
| LLM-datahygiëne | LLM ontvangt nooit ruwe ledendata | Gemini-input bevat uitsluitend geaggregeerde samenvattingen of niet-identificerende beslissingsresultaten (booleans, categorieën) |
| Deployment-uniformiteit | Runtime is voorspelbaar en consistent | Alle services: Gen2 Cloud Functions, Python 3.12, `europe-west1` — geen Gen1, geen Cloud Run tenzij expliciet noodzakelijk |
| Configureerbaar zonder deployment | Operationele parameters aanpasbaar zonder codewijziging | Prompts, taakconfigs en tenant-instellingen leven in Firestore |
| Herleidbaarheid | Elke agent-actie is traceerbaar | Alle discrepantie-acties gelogd in `error_log` en `agent_sessions`; elke service logt INPUT en outputs als gestructureerde JSON |

---

## Risico's & technische schuld

| Risico / Schuld | Omschrijving | Status |
|---|---|---|
| Pipeline log-walking zonder trace-ID | Stage C (staged investigation) doorzoekt Cloud Logging per service op `customer_id`/email. Werkt voor incidenteel gebruik; schaalt niet bij hoog volume. Structurele fix: trace-ID-propagatie door de pipeline. | Technische schuld — geen backlog-item |
| Sportivity herprobeert webhooks niet | Bij pipeline-downtime gaat een Sportivity-webhook permanent verloren. DLQ helpt alleen voor berichten die al in Pub/Sub zitten. | Zie BACKLOG.md: *Sportivity reconciliatie-job* |
| Proliferatie van agent-services | `habits-coach-reply` en `slack-agent` volgen hetzelfde basispatroon in aparte services. Zonder ingreep groeit het aantal agent-services lineair met het aantal processen. | Zie BACKLOG.md: *Eén configureerbare slack-agent* |
| Identiteitsassumpties in de pipeline | De pipeline keyt op `customer_id` of `email`. Bij een klant met meerdere e-mailadressen valt completion-matching stil. Stage 1 detecteert dit; geautomatiseerd herstel ontbreekt nog. | Zie BACKLOG.md: *Integratieagent* |

---

## Beslissingen

Alle architectuurbeslissingen zijn gedocumenteerd als ADRs. Raadpleeg de [ADR-index](docs/adr/README.md) voor een overzicht.

- Zie [ADR-0010](docs/adr/0010-event-driven-boven-request-driven.md) — Event-driven boven request-driven
- Zie [ADR-0011](docs/adr/0011-scheiding-enrichment-translation-listening.md) — Scheiding van enrichment, translation en listening
- Zie [ADR-0012](docs/adr/0012-llm-krijgt-nooit-ruwe-ledendata.md) — LLM krijgt nooit ruwe ledendata
- Zie [ADR-0013](docs/adr/0013-pii-blijft-in-slack.md) — PII blijft in Slack
- Zie [ADR-0014](docs/adr/0014-operationele-configuratie-in-firestore.md) — Operationele configuratie leeft in Firestore, niet in code
- Zie [ADR-0015](docs/adr/0015-gen2-cloud-functions-standaard-runtime.md) — Gen2 Cloud Functions als standaard runtime
- Zie [ADR-0016](docs/adr/0016-action-type-op-root-niveau.md) — action_type op root-niveau van CRM task payload
- Zie [ADR-0017](docs/adr/0017-gemini-google-genai-sdk.md) — Gemini via google-genai SDK, niet Vertex AI

---

## Glossary

| Term | Definitie |
|---|---|
| **HHI (Habit Health Index)** | Centraal meetsysteem voor retentiegezondheid per lid en per locatie. Drie journeys: Onboarding, Member Maintenance, Reactivation. Drie dimensies: Retentiescore, Customer Success Score, Progressiescore. |
| **Journey** | Een van de drie retentiefasen: Onboarding (nieuwe leden, eerste 90 dagen), Member Maintenance (actieve leden), Reactivation (leden die gestopt zijn met bezoeken). |
| **Tenant** | Een locatie of keten met volledig geïsoleerde data, configuratie en doelen. Primaire testomgeving: Basecamp Fitness (`bZxqF49CzTXpBz1px3K0`). |
| **Enrichment** | Een service die een inkomend event verrijkt met externe API-data en publiceert naar een `{source}-translations` topic. |
| **Translation** | Een service die een verrijkt event omzet naar het generieke event-formaat en publiceert naar het `events` topic. |
| **Listening** | Een service die events van het `events` topic ontvangt en doorstuurt naar een externe partij (Slack, Customer.io, BigQuery). |
| **action_type** | Veld op root-niveau van een CRM-taakpayload. Bepaalt welk event-type de taak afsluit: `contact`, `appointment`, `subscription`, `review`. |
| **effective_action_type** | Afgeleid veld in de `task_performance` BigQuery view. Backward-compatible fallback als `action_type` ontbreekt — afgeleid uit `task_type` via een vaste mapping. |
| **doc_id** | Samengestelde Firestore-sleutel voor CRM-taken in `slack_messages`: `{tenant_id}_{channel_id}_{customer_id_of_hash_email}_{task_date}`. `task_date` altijd in Amsterdam-tijdzone. |
| **visible** | Boolean op een CRM-taak. `false` = taak opgeslagen in Firestore en BigQuery maar geen Slack-output. |
| **FitCheck** | Periodieke conditiemeting/check-in afspraak bij de sportschool. Matching via `LIKE '%fitcheck%'` in BigQuery views vanwege naamvarianten over tijd. |
| **Reboot** | Reactivatie-afspraak voor leden die lang niet geweest zijn. |
| **enabledServices** | Optioneel veld in `tenants/{tenant_id}`. Lijst van actieve externe systemen voor deze tenant (`acuity`, `sportivity`, `customerio`). Als het veld ontbreekt, valt de agent terug op credential-aanwezigheid. |

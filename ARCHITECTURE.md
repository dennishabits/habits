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

## Beslissingen

**Event-driven boven request-driven**
Alle communicatie tussen services verloopt via Pub/Sub topics, niet via directe HTTP-aanroepen tussen services.

*Waarom*: event-driven architectuur maakt services onafhankelijk van elkaar. Een service die faalt blokkeert geen andere services. Nieuwe consumers kunnen worden toegevoegd zonder bestaande services aan te passen.

**Scheiding van enrichment, translation en listening**
Elke service heeft één verantwoordelijkheid. Enrichment haalt externe data op. Translation transformeert naar generiek formaat. Listening stuurt data naar ontvangende partijen. Deze verantwoordelijkheden worden nooit gecombineerd in één service.

*Waarom*: een service met meerdere verantwoordelijkheden is moeilijker te testen, te debuggen en te vervangen. Strikte scheiding maakt het systeem voorspelbaar en vervangbaar per onderdeel.

**LLM krijgt nooit ruwe ledendata**
BigQuery handelt alle analyse af via deterministische SQL. Gemini ontvangt alleen geaggregeerde samenvattingen — nooit individuele ledenprofielen of persoonsgegevens.

*Waarom*: PII mag het systeem niet verlaten via LLM-aanroepen. Bovendien levert deterministische SQL betere en controleerbare analyse dan LLM-redenering over ruwe data.

**PII blijft in Slack**
Persoonsgegevens worden getoond in Slack maar nooit opgeslagen in Firestore-geschiedenis of meegegeven aan Gemini als context.

*Waarom*: Slack is het operationele kanaal waar medewerkers werken. Firestore en LLM-context zijn systeemdelen buiten die grens — daar hoort PII niet.

**Operationele configuratie leeft in Firestore, niet in code**
Prompts, task configs en tenant-specifieke instellingen worden beheerd via Firestore. Aanpassingen hieraan vereisen geen deployment.

*Waarom*: kennis en uitvoering zijn gescheiden. Als configuratie in code zit, vereist elke aanpassing een deployment en een ontwikkelaar. In Firestore kan het systeem zichzelf aanpassen zonder codewijziging.

**Gen2 Cloud Functions als standaard runtime**
Alle services draaien als Cloud Functions Gen2 met Python 3.12. Geen Gen1, geen Cloud Run tenzij een specifieke beperking van Functions dat vereist.

*Waarom*: consistentie in runtime verlaagt de operationele complexiteit. Gen2 met het `@functions_framework.cloud_event` patroon is de standaard voor alle Pub/Sub-functies.

**action_type op root-niveau van CRM task payload**
`action_type` bepaalt welk completion-event een taak afsluit en staat altijd op root-niveau van de payload, niet genest onder `payload.payload`. Backward compatibility via `effective_action_type` fallback in de `task_performance` view.

*Waarom*: completion-logica moet onafhankelijk zijn van taaktype-namen die over tijd kunnen wijzigen. Door `action_type` expliciet mee te sturen vanuit Customer.io blijft de BigQuery view stabiel bij nieuwe taaktypen.

**Gemini via google-genai SDK, niet Vertex AI**
Alle Gemini-aanroepen gebruiken de `google-genai` SDK met een `GEMINI_API_KEY`. Vertex AI is niet bruikbaar in `europe-west1` voor Gemini modellen.

*Waarom*: `europe-west1` heeft geen beschikbare publisher models via Vertex AI. De `google-genai` SDK met API key werkt wel en is consistent met hoe `habits-coach-reply` al opereerde.

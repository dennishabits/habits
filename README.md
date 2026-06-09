# Habits

Habits is een AI-first SaaS-platform voor gymoperators. Het systeem start een continue verbeteringscyclus waarbij retentie en teamprestaties structureel verbeteren op basis van data — niet eenmalig ontworpen, maar continu bijgesteld.

## Wat het doet

- **Ledenretentie** — de Habit Health Index (HHI) maakt retentierisico per lid inzichtelijk over drie journeys en drie dimensies. AI-coaching via Slack begeleidt managers bij het verbeteren van de onderliggende processen.
- **Teamprestaties** — operationele taakuitvoering wordt inzichtelijk gemaakt via dagelijkse rapporten per dagdeel. FitCheck-feedback koppelt terug naar medewerkersniveau.

## Architectuur

Event-driven microservices op Google Cloud Platform. Elke service heeft één verantwoordelijkheid.

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

**Stack**
- Google Cloud Functions Gen2, Python 3.12, `europe-west1`
- Pub/Sub voor communicatie tussen services
- BigQuery voor analytics en rapportage
- Firestore voor operationele state en configuratie
- Gemini (`gemini-2.5-flash`) via `google-genai` SDK
- Slack als primaire gebruikersinterface

## Documentatie

| Document | Inhoud |
|---|---|
| `BUSINESS.md` | Missie, propositie, doelgroep en strategische beslissingen |
| `ARCHITECTURE.md` | Services, pipeline, datamodel en ontwikkelprincipes |
| `OPERATIONS.md` | CRM task payloads, afspraaktypes, Firestore config en prompt richtlijnen |
| `BACKLOG.md` | Ideeën en features die nog niet worden uitgevoerd |
| `AGENT.md` | Beslissingsbevoegdheid en werkstandaarden voor agents |

## Deployment

```bash
gcloud functions deploy [naam] \
  --gen2 \
  --runtime=python312 \
  --region=europe-west1 \
  --source=. \
  --entry-point=[functienaam] \
  --trigger-topic=[topic] \
  --project=solid-future-452906-a2
```

GEMINI_API_KEY meegeven bij deployment:

```bash
--set-env-vars GEMINI_API_KEY=$(gcloud functions describe habits-coach-reply \
  --gen2 --region=europe-west1 \
  --project=solid-future-452906-a2 \
  --format="value(serviceConfig.environmentVariables.GEMINI_API_KEY)")
```

## Primaire testomgeving

Basecamp Fitness — tenant `bZxqF49CzTXpBz1px3K0`

# Habits — Agent Policy

## Doel

Dit document beschrijft hoe agents opereren binnen het Habits-project. Het definieert beslissingsbevoegdheid, escalatieregels en werkstandaarden. Een agent die dit document niet heeft gelezen mag geen acties uitvoeren.

Lees altijd ook `ARCHITECTURE.md` en `BUSINESS.md` voordat je begint. Raadpleeg `BACKLOG.md` voordat je een nieuwe feature ontwerpt.

---

## Kennisarchitectuur

| Document | Type | Functie |
|---|---|---|
| `BUSINESS.md` | Grounding | Wat is Habits, voor wie, waarom zo gebouwd |
| `ARCHITECTURE.md` | Grounding | Hoe het systeem is gebouwd, welke services, welke standaarden |
| `OPERATIONS.md` | Grounding | Operationele configuratie, payload-formaten, prompt-richtlijnen |
| `BACKLOG.md` | Grounding | Ideeën en features die nog niet worden uitgevoerd |
| `AGENT.md` | Policy | Hoe agents opereren — dit document |
| `GOALS.md` | Memory | Meetbare doelen per tenant *(nog aan te leggen)* |

---

## Fase: Human-in-the-loop

Habits bevindt zich in de eerste agent-fase. Agents bouwen en analyseren zelfstandig, maar deployment vereist expliciete goedkeuring van Dennis voordat het wordt uitgevoerd.

---

## Beslissingsbevoegdheid

### Zelfstandig uitvoeren

Een agent mag het volgende doen zonder voorafgaande goedkeuring:

- Code lezen en analyseren
- Bugs identificeren en de oorzaak traceren
- Fixes schrijven en toelichten
- `requirements.txt` aanpassen
- `main.py` aanpassen
- Deployment commands voorbereiden en presenteren
- Logs lezen en interpreteren
- BigQuery views en queries analyseren
- Firestore `config` collectie lezen en schrijven (prompts, thresholds)
- Firestore-structuur inspecteren (read-only, alleen `config`)

### Goedkeuring vereist

De agent stelt voor, legt uit, en wacht op expliciete bevestiging ("ja", "deploy", "go ahead") voordat:

- `gcloud functions deploy` wordt uitgevoerd
- `gcloud pubsub topics publish` wordt uitgevoerd voor testen

### Altijd escaleren naar Dennis

De agent voert deze acties nooit uit, ook niet na goedkeuring op andere punten:

- Aanmaken of verwijderen van Pub/Sub topics
- Aanmaken of verwijderen van Cloud Scheduler jobs
- Wijzigingen aan Firestore-schema of collectiestructuur
- Lezen uit `tenants`, `slack_messages`, `coaching_sessions`, `session_locks` — deze collecties bevatten PII en tokens
- Toevoegen of wijzigen van secrets of environment variables
- Wijzigingen die meerdere services tegelijk raken
- Aanmaken van nieuwe GCP resources (buckets, datasets, etc.)

Bij twijfel: escaleer. Een verkeerde deployment is omkeerbaar. Een verkeerde infra-wijziging vaak niet.

---

## Werkwijze

### Bij een debug-taak

1. Lees `ARCHITECTURE.md` — begrijp de service en zijn verantwoordelijkheid
2. Lees de `main.py` van de betreffende service
3. Lees de relevante logs (via `gcloud functions logs read`)
4. Identificeer de **root cause** — nooit een symptoom fixen zonder de oorzaak te begrijpen
5. Schrijf de fix met uitleg
6. Presenteer het deployment command — wacht op goedkeuring
7. Na goedkeuring: deploy en verifieer via logs
8. Na succesvolle verificatie: commit en push naar GitHub met een beschrijvende commit message
9. Nooit pushen zonder succesvolle verificatie — GitHub is de source of truth voor wat werkt

### Bij een nieuwe feature

1. Raadpleeg `BACKLOG.md` — staat het er al in?
2. Raadpleeg `ARCHITECTURE.md` — past het in de bestaande pipeline?
3. Bespreek het ontwerp eerst — schrijf geen code zonder alignment op de aanpak
4. Volg de scheiding: enrichment ≠ translation ≠ listening
5. Volg de logging-standaard: INPUT → ENRICHMENT_{SOURCE}_{TYPE} → TO_{TOPIC}
6. Schrijf complete, deploybare code — geen snippets
7. Presenteer het deployment command — wacht op goedkeuring
8. Na goedkeuring: deploy en verifieer via logs
9. Na succesvolle verificatie: commit en push naar GitHub met een beschrijvende commit message

### Bij een onduidelijke taak

Stel één gerichte vraag. Niet meerdere vragen tegelijk. Niet gokken en hopen.

---

## Technische standaarden

Alle standaarden staan in `ARCHITECTURE.md` onder "Ontwikkelprincipes". Samenvatting van de meest kritieke:

- **Gen2 Cloud Functions** met `@functions_framework.cloud_event` decorator voor alle Pub/Sub-functies
- **Gemini**: gebruik `google-genai` SDK met `GEMINI_API_KEY` — niet Vertex AI
- **Werkend model**: `gemini-2.5-flash`
- **Emails**: altijd `LOWER()` voor joins en lookups
- **Acuity tijden**: altijd Amsterdam lokaal (`AMSTERDAM_TZ`), nooit UTC
- **Foutafhandeling**: error gelogd én gepubliceerd naar `events` topic met `email: dennis@habits.fit`
- **PII**: nooit in Firestore-geschiedenis of Gemini-context

### Deployment command (standaard)

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

### GEMINI_API_KEY meegeven bij deployment

```bash
--set-env-vars GEMINI_API_KEY=$(gcloud functions describe habits-coach-reply \
  --gen2 --region=europe-west1 \
  --project=solid-future-452906-a2 \
  --format="value(serviceConfig.environmentVariables.GEMINI_API_KEY)")
```

### Logs lezen

```bash
gcloud functions logs read [naam] \
  --gen2 \
  --region=europe-west1 \
  --project=solid-future-452906-a2 \
  --limit=50
```

---

## Wat een agent nooit doet

- Code verwijderen zonder eerst te beschrijven wat het doet en om bevestiging te vragen
- Aannames maken over welk model, topic of kanaal gebruikt wordt — altijd verifiëren in `ARCHITECTURE.md`
- Partial code delen — altijd complete, deploybare bestanden
- Meerdere vragen tegelijk stellen
- Een fix deployen zonder goedkeuring
- Werken aan meerdere services tegelijk zonder expliciete opdracht

---

## Escalatiepad

Bij twijfel over beslissingsbevoegdheid: niet uitvoeren, maar presenteren aan Dennis met een korte toelichting waarom dit buiten de zelfstandige bevoegdheid valt.

Format voor escalatie:
```
⚠️ Escalatie vereist
Actie: [wat de agent wil doen]
Reden: [waarom dit buiten zelfstandige bevoegdheid valt]
Voorstel: [wat Dennis moet beslissen]
```

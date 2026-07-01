> Modus: How-to + Reference — werkwijze en beslissingsbevoegdheid (How-to); schemas en standaarden (Reference). Secties zijn gelabeld.

# Habits — Agent Policy

## Doel

Dit document beschrijft hoe agents opereren binnen het Habits-project. Het definieert beslissingsbevoegdheid, escalatieregels en werkstandaarden. Een agent die dit document niet heeft gelezen mag geen acties uitvoeren.

Lees altijd ook `ARCHITECTURE.md` en `BUSINESS.md` voordat je begint. Raadpleeg `BACKLOG.md` voordat je een nieuwe feature ontwerpt.

---

## Kennisarchitectuur

*[Reference]*

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

*[Reference]*

Habits bevindt zich in de eerste agent-fase. Agents bouwen en analyseren zelfstandig, maar deployment vereist expliciete goedkeuring van Dennis voordat het wordt uitgevoerd.

---

## Beslissingsbevoegdheid

*[Reference]*

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
- Firestore-structuur inspecteren (read-only)

### Goedkeuring vereist

De agent stelt voor, legt uit, en wacht op expliciete bevestiging ("ja", "deploy", "go ahead") voordat:

- `gcloud functions deploy` wordt uitgevoerd
- `gcloud pubsub topics publish` wordt uitgevoerd voor testen
- Bestanden worden gecommit en gepusht naar GitHub
- Een Firestore taakdocument direct wordt bijgewerkt buiten de normale pipeline
- Een correctie-event wordt gepubliceerd om een taak alsnog te voltooien

### Altijd escaleren naar Dennis

De agent voert deze acties nooit uit, ook niet na goedkeuring op andere punten:

- Aanmaken of verwijderen van Pub/Sub topics
- Aanmaken of verwijderen van Cloud Scheduler jobs
- Wijzigingen aan Firestore-schema of collectiestructuur
- Toevoegen of wijzigen van secrets of environment variables
- Wijzigingen die meerdere services tegelijk raken
- Aanmaken van nieuwe GCP resources (buckets, datasets, etc.)
- Acties in externe systemen met destructief effect (Customer.io merge, Acuity e-mailadres wijzigen)

Bij twijfel: escaleer. Een verkeerde deployment is omkeerbaar. Een verkeerde infra-wijziging vaak niet.

---

## Werkwijze

### Bij een debug-taak

*[How-to]*

1. Lees `ARCHITECTURE.md` — begrijp de service en zijn verantwoordelijkheid
2. Lees de `main.py` van de betreffende service
3. Lees de relevante logs (via `gcloud functions logs read`)
4. Identificeer de **root cause** — nooit een symptoom fixen zonder de oorzaak te begrijpen
5. Schrijf de fix met uitleg
6. Presenteer het deployment command — wacht op goedkeuring
7. Na goedkeuring: deploy en verifieer via logs

### Bij een nieuwe feature

*[How-to]*

1. Raadpleeg `BACKLOG.md` — staat het er al in?
2. Raadpleeg `ARCHITECTURE.md` — past het in de bestaande pipeline?
3. Bespreek het ontwerp eerst — schrijf geen code zonder alignment op de aanpak
4. Volg de scheiding: enrichment ≠ translation ≠ listening
5. Volg de logging-standaard: INPUT → ENRICHMENT_{SOURCE}_{TYPE} → TO_{TOPIC}
6. Schrijf complete, deploybare code — geen snippets

### Bij een onduidelijke taak

*[How-to]*

Stel één gerichte vraag. Niet meerdere vragen tegelijk. Niet gokken en hopen.

---

## Foutafhandeling via slack-agent

*[Reference]*

De `slack-agent` handelt discrepanties in taakverwerking af via de Slack-thread van de betreffende taak. Dit proces volgt vaste regels.

### Triggerherkenning

Niet elk bericht in een taak-thread is een systeemfout. De agent onderscheidt:

- **Discrepantiesignaal** (`discrepancy`) — medewerker geeft aan dat een taak gedaan is maar niet als voltooid staat, ook impliciet ("afspraak staat ingepland") → onderzoek starten
- **Operationele opmerking** (`operational`) — medewerker meldt iets over de uitvoering terwijl de taak nog niet klaar is ("hij nam niet op") → registreren, geen reactie
- **Onduidelijk** (`unclear`) — bericht gaat over de taak maar intentie is niet helder → de agent stelt een contextuele vraag terug (gegenereerd door Gemini op basis van het bericht, geen vaste zin)
- **Niet relevant** (`irrelevant`) — mention (@naam), intern overleg, vraag aan collega, off-topic → stilletjes negeren, geen reactie

Pure `@mention` berichten worden afgevangen vóór de Gemini-aanroep (pre-filter).

### Onderzoeksproces

Het onderzoek is gelaagd. De agent stopt zodra de root cause gevonden is.

**Voor `action_type == "appointment"` (member_talk, fitcheck, evaluation, followup_appointment):**

Drie geordende stages — elke stage is alleen conclusief als het bewijs aanwezig is. Stop zodra een stage een oorzaak oplevert.

1. **Stage A — Acuity bronwaarheid**: bevraag de Acuity API op actieve afspraken voor het e-mailadres van de klant. Als er geen afspraak bestaat → de medewerker heeft zich vergist of de afspraak staat onder een ander e-mailadres. Sla het `datetimeCreated`-tijdstip op als anker voor Stage C.
2. **Stage B — Identiteitsreconciliatie**: bevraag de `customers` tabel in BigQuery op klanten met dezelfde naam maar een afwijkend e-mailadres. Een duplicaat duidt op een dubbel account dat de pipeline-matching verstoort. Controleer welk account het actieve abonnement heeft. Zie de resolutieprocedure hieronder bij `identity_mismatch`.
3. **Stage C — BigQuery pipeline-analyse**: bevraag `raw_events` op appointment-events voor het e-mailadres of `customer_id` van de klant. Controleer daarna de `appointments` view op hetzelfde e-mailadres of `customer_id`. Als de afspraak in `raw_events` staat maar niet in de `appointments` view, of als `customer_id = NULL` staat op het event (`is_known_customer = false`), is dat het drop-punt.

**Voor alle andere `action_type` waarden:**

1. **Firestore** — taakstate, action_type, customer_id, email, created_at, expired, completed
2. **BigQuery** — events rondom die taak in het relevante tijdvenster (completion events, expiry events)

De agent raadpleegt Firestore (`tenants/{tenant_id}`) en het `enabledServices` veld om te bepalen welke externe systemen actief zijn voor deze tenant voordat hij bronsystemen benadert.

### Herstelproces

De agent herstelt autonoom wanneer de root cause duidelijk is en de actie geen neveneffecten heeft buiten de taak zelf.

**Voorkeursvolgorde:**
1. **`pipeline_event`** — publiceer het correcte completion-event via het `events` topic én zet de taak direct op voltooid in Firestore + Slack
2. **`firestore_direct`** — zet de taak direct op voltooid in Firestore + Slack, zonder pipeline-event (bijv. als de pipeline al correct verwerkt heeft maar de taakstatus niet bijgewerkt is)
3. **`external_system`** of onbekende root cause — escaleer naar Dennis

Na autonoom herstel wordt de medewerker gevraagd te bevestigen ("De taak is nu correct verwerkt — klopt dit?"). Bij bevestiging wordt `employee_confirmed: true` gezet in de foutlog.

**Uitzondering — `action_type == "appointment"` (Stage 1):** herstel is uitgeschakeld voor de meeste diagnoses. De staged investigation produceert altijd een diagnose met `resolution_possible: false` en `needs_dennis_approval: true`. De agent rapporteert de bevindingen aan de medewerker en Dennis, maar onderneemt geen corrigerende actie. Remediatie wordt toegevoegd in Stage 2 zodra diagnoses vertrouwd zijn op live threads.

**Uitzondering: `identity_mismatch` resolutieprocedure** — bij een geïdentificeerd dubbel account voert de agent de volgende stappen uit *na akkoord van Dennis*:
1. Corrigeer het e-mailadres van de toekomstige afspraak in Acuity naar het e-mailadres van het actieve abonnement (`PUT /api/v1/appointments/{id}` met `{"email": "..."}`)
2. Update `next_checkin_at`, `next_checkin_name`, `next_checkin_employee` handmatig in de `customers` tabel in BigQuery voor de actieve klant
3. Sluit de taak af via Firestore + Slack (`completed: true`)
4. Meld aan Dennis: het dubbele Sportivity-account kan **niet** via API worden verwijderd (405 Method Not Allowed) — handmatige actie in Sportivity UI vereist. Customer.io merge is **niet beschikbaar** op het huidige plan (404) — profielen blijven gescheiden.

Systeemwijzigingen (code, deployment) vereisen altijd akkoord van Dennis.
Acties in externe systemen (Acuity e-mailadres, Customer.io merge) vereisen altijd akkoord van Dennis.

### Communicatie richting medewerker

De medewerker ziet geen technische details. De agent communiceert in drie stappen:

1. **Tijdens onderzoek**: "We kijken wat er is misgegaan."
2. **Na diagnose**: één zin wat er is misgegaan, in begrijpelijke taal.
3. **Na herstel**: "De taak is nu correct verwerkt — klopt dit?"

Geen jargon, geen excuses, geen architectuuruitleg.

### Toekomstige generalisatie via tool registry

De staged investigation is nu hardcoded voor Acuity, Sportivity en Customer.io (`_stage_a_for_task`, `investigate_stage_a_acuity`, `investigate_stage_b_identity`). Dit is technische schuld — elke nieuwe tool vereist nieuwe Python-branches, in strijd met ADR-0009. De geplande tool registry (Firestore `tool_registry/{tool_id}`) vervangt deze inline logica door declaratieve capability-definities; de orchestrator leest welke capabilities een tool biedt en roept ze generiek aan. Zie ADR-0018 en BACKLOG.md (*Tool registry + process registry*).

Stage C kan pas tool-agnostisch worden als trace-ID-propagatie bestaat — het bevraagt nu hardcoded `webhook_source`-kolommen in BigQuery.

### Escalatie bij vastlopen

Als na twee onderzoeksstappen de root cause niet gevonden is, escaleert de agent naar Dennis met:

```
⚠️ Escalatie foutonderzoek
Taak: [doc_id]
Signaal: [wat de medewerker meldde]
Onderzocht: [wat is bekeken]
Gevonden: [wat is gevonden]
Ontbreekt: [waarom de oorzaak nog onduidelijk is]
```

### Regels

1. **Eén probleem tegelijk** — één discrepantie per thread, geen cross-taak conclusies
2. **Gelaagd onderzoek** — stop zodra de root cause bekend is
3. **Diagnose is autonoom, herstel ook** — mits root cause duidelijk en geen neveneffecten buiten de taak
4. **Taak voltooien mag autonoom** — via `complete_task` (Firestore + Slack update) na `pipeline_event` of `firestore_direct` diagnose
5. **Elke actie is herleidbaar** — alles wordt gelogd in `error_log` en `agent_sessions`
6. **Escaleer bij vastlopen** — na twee stappen zonder antwoord, of bij `external_system` / onbekende root cause
7. **Twijfel? Vraag terug** — één contextuele vraag gegenereerd door Gemini, nooit aannames
8. **Patroon → structurele vlag** — bij elke `pipeline_drop_*` diagnose telt de agent het historisch aantal occurrences voor deze tenant (`pipeline_drop_count` in `error_log`). Als het totaal >5 of de occurrences ≥2 verschillende dagen beslaan: informeer de medewerker dat het probleem vaker voorkomt en dat een structurele oplossing wordt opgepakt; stuur Dennis een DM met het patroon en een verwijzing naar de *Acuity reconciliatie-job* in de backlog.

---

## Firestore foutlog

*[Reference]*

Elke foutafhandeling wordt vastgelegd in `error_log/{doc_id}` voor leren en auditing.

### Schema

| Veld | Type | Inhoud |
|---|---|---|
| `tenant_id` | string | Tenant waar de fout optrad |
| `task_doc_id` | string | Firestore doc_id van de betreffende taak |
| `signal` | string | Wat de medewerker meldde |
| `signal_type` | string | `discrepancy`, `operational`, `unclear`, `irrelevant` |
| `investigation_steps` | array | Welke bronnen zijn geraadpleegd in volgorde |
| `root_cause` | string | Vastgestelde oorzaak |
| `root_cause_category` | string | `timezone_mismatch`, `duplicate_email`, `late_completion`, `pipeline_error`, `appointment_not_in_source`, `identity_mismatch`, `unknown_customer_no_id` (afspraak binnengekomen met `is_known_customer = false` — `customer_id = NULL` in raw_events, scheduled query sloeg afspraak over voor `next_checkin_at`), `pipeline_drop_webhook_dispatcher`, `pipeline_drop_acuity_enricher`, `pipeline_drop_acuity_translator`, `pipeline_drop_bigquery_listener`, `pipeline_drop_customerio_listener`, `unknown` |
| `resolution` | string | Welke actie is uitgevoerd |
| `resolution_method` | string | `pipeline_event`, `firestore_direct`, `external_system`, `escalated` |
| `approved_by` | string | `agent` (autonoom) of `dennis` (na akkoord) |
| `employee_confirmed` | boolean | Heeft de medewerker bevestigd dat het klopt |
| `staged_findings` | dict \| null | Stage A/B/C resultaten voor appointment-discrepanties — `{stage_a, stage_b, stage_c}`; null voor andere action types |
| `pipeline_drop_count` | int \| null | Cumulatief aantal `pipeline_drop_*` events voor deze tenant t/m en met dit event; null voor andere categorieën |
| `reopened` | boolean | `true` als een medewerker na een auto-resolved sessie de taak heropende (negatieve tak `handle_awaiting_confirmation`) |
| `confidence` | string | Diagnose-confidence: `high`, `medium`, `low` — afkomstig van de LLM-diagnose |
| `original_error_log_doc_id` | string \| null | Doc-ID van het `error_log`-document dat aan deze heropening voorafging; null bij eerste optreden |
| `created_at` | timestamp | Moment van signaal |
| `resolved_at` | timestamp | Moment van afsluiting |

### Gebruik

De foutlog is de primaire bron voor patroonherkenning. Periodiek worden terugkerende `root_cause_category` waarden geanalyseerd om structurele fixes te prioriteren.

---

## Firestore sessiestate

*[Reference]*

De `slack-agent` slaat per thread een sessie op in `agent_sessions/{tenant_id}_{thread_ts}`.

### Schema

| Veld | Type | Inhoud |
|---|---|---|
| `tenant_id` | string | Tenant |
| `channel_id` | string | Slack channel |
| `thread_ts` | string | Thread timestamp (ook doc-ID suffix) |
| `task_doc_id` | string | Firestore doc_id van de betreffende taak |
| `status` | string | `investigating`, `unclear`, `awaiting_employee_confirmation`, `awaiting_dennis_approval`, `resolved` |
| `signal` | string | Eerste bericht van de medewerker |
| `signal_type` | string | `discrepancy`, `operational`, `unclear`, `irrelevant` |
| `conversation` | array | `{role, content, timestamp}` — volledige gesprekshistorie |
| `diagnosis` | dict | Gemini-diagnosresultaat |
| `resolution_method` | string | Gekozen herstelroute |
| `error_log_doc_id` | string | Verwijzing naar bijbehorend `error_log` document |
| `created_at` | timestamp | Moment van aanmaak |
| `updated_at` | timestamp | Laatste wijziging |

### Statusovergangen

`investigating` → diagnose klaar → `awaiting_employee_confirmation` (autonoom herstel) of `awaiting_dennis_approval` (escalatie)
`awaiting_employee_confirmation` → bevestigd → `resolved` / ontkend → `investigating`
`unclear` → herclassificatie → `investigating` of `resolved`

Schrijven naar `agent_sessions` is autonome actie — geen goedkeuring vereist.

---

## Technische standaarden

*[Reference]*

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

**Bekende entry points per service** (gebruik exact deze namen — een verkeerde naam geeft een `MissingTargetException` bij elke invocation):

| Service | `--entry-point` |
|---|---|
| `slack-listener` | `slack_crm_pipeline` |
| `slack-agent` | `slack_agent` |
| `acuity-enricher` | `acuity_enricher` |
| `acuity-translator` | `acuity_pipeline` |
| `sportivity-enricher` | `sportivity_enricher` |
| `sportivity-translator` | `sportivity_pipeline` |
| `customerio-listener` | `customerio_listener` |
| `customerio-translator` | `customerio_pipeline` |
| `coaching-listener` | `coaching_listener` |
| `habits-coach-reply` | `habits_coach_reply` |
| `habits-coach-weekly` | `habits_coach_weekly` |
| `agent-quality-reviewer` | `agent_quality_reviewer` |

### Dead-letter queue (events topic)

Als slack-listener een bericht 5 keer niet kan verwerken, belandt het in `events-dead-letter`. Inspecteer zo:

```bash
gcloud pubsub subscriptions pull events-dead-letter-viewer \
  --limit=10 --auto-ack \
  --project=solid-future-452906-a2
```

Om een bericht te replayen: publiceer het handmatig opnieuw naar het `events` topic na de oorzaak te hebben opgelost.

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

## Mogelijke dead code — verificatie vereist

*[Reference]*

`PIPELINE_STAGES` (lijst van servicenamen) en `_search_stage_logs` (Cloud Logging-query per stage) in `slack-agent/main.py` lijken afkomstig van het vroegere log-walking approach, dat is vervangen door `investigate_stage_c_bigquery`. Ze worden niet gebruikt in de huidige staged investigation.

**Actie vereist**: verifieer of deze symbolen nog worden aangeroepen voordat ze worden verwijderd. Verwijdering verloopt via de standaardprocedure: beschrijf wat de code doet, vraag bevestiging, verwijder dan pas. Zie AGENT.md-regel "Code verwijderen".

---

## Wat een agent nooit doet

*[Reference]*

- Code verwijderen zonder eerst te beschrijven wat het doet en om bevestiging te vragen
- Aannames maken over welk model, topic of kanaal gebruikt wordt — altijd verifiëren in `ARCHITECTURE.md`
- Partial code delen — altijd complete, deploybare bestanden
- Meerdere vragen tegelijk stellen
- Een fix deployen zonder goedkeuring
- Werken aan meerdere services tegelijk zonder expliciete opdracht
- Een taak direct als voltooid markeren buiten de slack-agent correctieflow om, zonder akkoord van Dennis
- Acties uitvoeren in externe systemen (Acuity, Sportivity, Customer.io) zonder akkoord van Dennis

---

## Escalatiepad

*[How-to]*

Bij twijfel over beslissingsbevoegdheid: niet uitvoeren, maar presenteren aan Dennis met een korte toelichting waarom dit buiten de zelfstandige bevoegdheid valt.

Format voor escalatie:
```
⚠️ Escalatie vereist
Actie: [wat de agent wil doen]
Reden: [waarom dit buiten zelfstandige bevoegdheid valt]
Voorstel: [wat Dennis moet beslissen]
```

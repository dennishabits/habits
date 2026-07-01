> Modus: Reference — toekomstig werk. Agents raadplegen dit vóór het ontwerpen van nieuwe features.

# Habits — Backlog

## Gebruik

Dit document bevat ideeën en features die waardevol zijn maar nog niet worden uitgevoerd. Agents en ontwikkelaars raadplegen dit document altijd voordat een nieuwe feature wordt ontworpen — om te voorkomen dat het wiel opnieuw wordt uitgevonden en om bestaande ideeën mee te nemen in nieuwe ontwerpen.

Een item op de backlog is geen commitment. Het is een vastgelegd idee met genoeg context om later snel op te pakken.

## Formaat

```
### [Titel]
**Categorie**: feature / architectuur / onderzoek
**Waarde**: waarom is dit waardevol?
**Context**: wat weten we al, wat is de aanleiding?
**Afhankelijkheden**: wat moet er eerst zijn voordat dit opgepakt kan worden?
```

---

## Features

### Shift-attributie voor medewerkersrapporten
**Categorie**: feature
**Waarde**: maakt gepersonaliseerde coaching per medewerker mogelijk in teamrapporten — nu zijn rapporten anoniem omdat shift-data niet betrouwbaar beschikbaar is.
**Context**: FitCheck-feedback is al gekoppeld aan medewerkers. Het ontbrekende stuk is koppeling van shift-planning aan uitgevoerde taken.
**Afhankelijkheden**: betrouwbare shift-data vanuit roostersysteem of handmatige invoer.

### Manager-observaties als coaching-databron
**Categorie**: feature
**Waarde**: derde databron naast HHI en FitCheck — maakt coaching gebaseerd op directe observatie mogelijk.
**Context**: coaching steunt nu op HHI-data en FitCheck-feedback. Manager-observaties zijn de ontbrekende kwalitatieve laag.
**Afhankelijkheden**: invoerformaat bepalen; integratie in coaching-sessie flow.

### Rapportagelaag voor eigenaar / franchisenemer
**Categorie**: feature
**Waarde**: de koper van Habits heeft een andere informatiebehoefte dan de manager — retentie en ROI op locatieniveau, niet operationele details.
**Context**: bewust opengelaten als ontwerpkeuze totdat we meer leren van Basecamp Fitness als testomgeving.
**Afhankelijkheden**: voldoende data en inzicht in wat de eigenaar daadwerkelijk wil zien.

### Customer.io campagnes via MCP server
**Categorie**: feature
**Waarde**: agents kunnen campagnes en templates direct aanmaken en bewerken in Customer.io zonder UI — versnelt iteratie op de CRM-laag significant.
**Context**: Customer.io heeft een MCP server aangekondigd die de volledige Agent-kracht krijgt. Nu al bruikbaar via de App API voor broadcasts en templates.
**Afhankelijkheden**: Customer.io MCP server volledig uitgerold.

### slack-agent Stage 2: remediatie na appointment-diagnoses
**Categorie**: feature
**Waarde**: sluit de feedbackloop die Stage 1 opent — diagnoses worden vertrouwd en de agent kan autonoom of semi-autonoom herstellen afhankelijk van de root cause.
**Context**: Stage 1 (deployed 2026-06-18) produceert voor `action_type == "appointment"` een diagnose via Acuity (Stage A), identiteitsreconciliatie (Stage B) en pipeline log-walk (Stage C), maar neemt geen corrigerende actie. Stage 2 voegt remediatie toe per root cause: `appointment_not_in_source` → eerlijke afsluiting met melding aan medewerker; `identity_mismatch` → escalatie naar Dennis voor account-merge; `pipeline_drop_{stage}` → replay-strategie bepalen en uitvoeren. Remediatie mag pas worden ingeschakeld als de diagnoses op live threads vertrouwd zijn gebleken.
**Afhankelijkheden**: voldoende live Stage 1 diagnoses om patronen te valideren; akkoord Dennis per remediatie-type.

### Automatisch e-mailadres corrigeren in Acuity via slack-agent
**Categorie**: feature
**Waarde**: sluit de feedbackloop bij onbekende e-mailadressen — de agent detecteert de discrepantie, zoekt het juiste e-mailadres op via telefoon-lookup in BigQuery, en past het e-mailadres in Acuity aan via de API na akkoord van de medewerker in de Slack-thread.
**Context**: de `acuity-enricher` signaleert al onbekende e-mailadressen als een `crm_task` in `#taken`. Twee scenario's: (A) telefoon matcht wel in BigQuery → correct e-mailadres bekend, agent kan direct corrigeren; (B) geen match → medewerker moet handmatig zoeken. De Acuity API biedt `PATCH /appointments/{id}` voor e-mailadres updates. Dit item is onderdeel van de bredere integratieagent capability.
**Afhankelijkheden**: `slack-agent` service.

### Integratieagent: signaleren en oplossen van datakwaliteitsproblemen
**Categorie**: feature
**Waarde**: integratieproblemen tussen systemen worden automatisch gedetecteerd, onderzocht en opgelost — met human-in-the-loop waar nodig. Vervangt handmatig debuggen en voorkomt stille fouten die leiden tot verkeerde retentiedata.
**Context**: Habits koppelt vier systemen: Sportivity (bron van waarheid voor klantdata), Acuity (afspraken), Customer.io (marketing automation) en BigQuery (analyse). Datakwaliteitsproblemen — zoals een klant met twee e-mailadressen — ontstaan onvermijdelijk door de indirecte koppeling tussen die systemen. De agent moet in staat zijn om: (1) de root cause te traceren via BigQuery-lookup, (2) het juiste e-mailadres op te halen uit Sportivity, (3) het aan te passen in Acuity via `PATCH /appointments/{id}`, (4) een dubbel profiel te detecteren in Customer.io via de App API, en (5) profielen te mergen via `POST /api/v1/merge_customers` na akkoord. Alle destructieve stappen vereisen expliciete bevestiging via de Slack-thread.
**Afhankelijkheden**: `slack-agent` service; Customer.io App API access.

### Acuity reconciliatie-job
**Categorie**: feature
**Waarde**: voorkomt permanent verlies van afsprakentaken als een Acuity-webhook niet aankomt — zonder dat een medewerker de discrepantie hoeft te melden. Sluit afsprakentaken autonoom af zodra Acuity de afspraak bevestigt maar de pipeline deze nooit ontving.
**Context**: geïdentificeerd op 2026-06-18 via staged investigation (`pipeline_drop_webhook_dispatcher`). De `slack-agent` detecteert nu per geval dat een Acuity-webhook niet is ontvangen, maar heeft geen structurele fallback. De oplossing is een periodieke job (Cloud Scheduler + Cloud Function) die: (1) alle open appointment-taken in Firestore ophaalt die ouder zijn dan X uur; (2) voor elke taak de Acuity API bevraagt; (3) als Acuity een afspraak bevestigt die de pipeline nooit ontving, sluit de taak autonoom via een synthetisch completion-event; (4) logt gevonden discrepanties naar BigQuery. Prioritering op basis van `pipeline_drop_count` in `error_log`: bij >5 totale occurrences of ≥2 verschillende dagen → structurele aanpak verplicht.
**Afhankelijkheden**: Acuity API toegang (al beschikbaar via `acuity-enricher`); Cloud Scheduler job (vereist goedkeuring Dennis).

### Sportivity reconciliatie-job
**Categorie**: feature
**Waarde**: voorkomt permanent verlies van `subscription_update` events als Sportivity een webhook stuurt terwijl de pipeline tijdelijk down is.
**Context**: geïdentificeerd op 2026-06-13. Sportivity herprobeert webhooks niet — als het bericht het `sportivity-enricher` niet bereikt, bestaat er geen herstelmechanisme. Oplossing: periodieke job die open `member_admin` en `subscription_change` taken checkt via de Sportivity API en bij al-verwerkte status een synthetisch completion-event publiceert.
**Afhankelijkheden**: Sportivity API toegang (al beschikbaar via `sportivity-enricher`); Cloud Scheduler job (vereist goedkeuring Dennis).

---

## Architectuur

### Feedbackloop sluiten: taak → lid-uitkomst
**Categorie**: architectuur
**Waarde**: zonder deze koppeling meet het systeem activiteit, niet impact. Dit is de voorwaarde voor een zelflerend systeem.
**Context**: `crm_task_id` moet worden gekoppeld aan subscription- en appointment-events die als completion gelden. `task_performance` view moet worden uitgebreid met uitkomst-metrics.
**Afhankelijkheden**: schema-aanpassing BigQuery; update `task_performance` view.

### Evaluatielaag
**Categorie**: architectuur
**Waarde**: vergelijkt wekelijks gemeten uitkomsten met de goal registry en genereert automatisch nieuwe taken voor het systeem. Maakt Habits zelfverbeterend.
**Context**: beschreven in de agent-architectuur als Laag 2. Vereist dat de feedbackloop eerst gesloten is en dat GOALS.md is gedefinieerd.
**Afhankelijkheden**: feedbackloop taak → lid-uitkomst; GOALS.md.

### `#coaching-drafts` privékanaal
**Categorie**: architectuur
**Waarde**: human-in-the-loop review voordat coaching-berichten medewerkers bereiken.
**Context**: gepland als onderdeel van de `slack-agent` service. Maakt gecontroleerde uitrol van AI-coaching mogelijk.
**Afhankelijkheden**: `slack-agent` service.

### Leren van team-antwoorden op coachingsvragen
**Categorie**: architectuur
**Waarde**: het coachingsbericht sluit af met een open vraag die het team beantwoordt in de thread. Door die antwoorden mee te geven aan het volgende rapport wordt coaching cumulatief.
**Context**: het employee-rapport vraagt het team al om te reageren in de thread. De antwoorden gaan nu nergens naartoe. Benodigde stappen: (1) `slack-agent` pikt thread-replies op rapport-berichten op; (2) antwoorden worden opgeslagen in Firestore per tenant; (3) `team-report` haalt de laatste N antwoorden op en geeft ze mee aan Gemini als context.
**Afhankelijkheden**: `slack-agent` service; Firestore opslag van thread-replies.

### Eén configureerbare slack-agent die alle processen afhandelt
**Categorie**: architectuur
**Waarde**: voorkomt proliferatie van losse agent-services per proces. De agent is generiek; processen zijn configuratie in Firestore per tenant.
**Context**: nu zijn `habits-coach-reply` en `slack-agent` twee aparte services die hetzelfde basispatroon volgen. Op termijn moet dit één service worden waarbij kanaal + intent bepalen welk proces wordt gestart. De huidige `slack-agent` is al gebouwd met deze scheiding in gedachten — de stap naar volledig configureerbare processen is daarmee klein. Zie ook: Tool registry + process registry hieronder.
**Afhankelijkheden**: voldoende proceservaring om het configuratieformaat te kunnen generaliseren; minimaal twee processen in productie.

### Secrets niet blootstellen in deployment output
**Categorie**: architectuur
**Waarde**: voorkomt dat API-keys en tokens in plaintext verschijnen in terminal output, logs of gedeelde sessies — nu lekt de `ANTHROPIC_API_KEY` mee in de `gcloud deploy` output.
**Context**: bij elke `gcloud functions deploy` wordt de volledige `serviceConfig` getoond inclusief environment variables. Keys die via `--set-env-vars` zijn gezet zijn direct zichtbaar. Oplossing: gebruik Secret Manager en refereer via `--set-secrets` in plaats van `--set-env-vars`.
**Afhankelijkheden**: geen.

### GitHub-authenticatie via SSH of credential manager
**Categorie**: architectuur
**Waarde**: verwijdert het GitHub PAT uit de git remote URL — credentials staan nu in plaintext in `.git/config`.
**Context**: de remote URL bevat een hardcoded personal access token. Vervangen door SSH-keys of macOS Keychain credential manager is veiliger en een beter patroon naarmate het team groeit.
**Afhankelijkheden**: geen.

### Tool registry + process registry (generalisatie slack-agent)
**Categorie**: architectuur
**Waarde**: elimineert hardcoded tool-branches in de orchestrator. Nieuwe tool onboarden = één registry-entry toevoegen, geen Python-wijziging. Consistent met ADR-0009 en ADR-0018.
**Context**: `_stage_a_for_task`, `investigate_stage_a_acuity`, `investigate_stage_b_identity` en BigQuery-queries met `webhook_source = 'acuity'` zitten inline in de slack-agent. Elke nieuwe tool vereist nu nieuwe branches. Oplossing: Firestore `tool_registry/{tool_id}` declareert per extern systeem auth-patroon en capabilities (`verify_existence`, `fetch_profile`, `apply_action`). Firestore `process_registry/{process_id}` declareert proceslogica. Schema afgeleid van de 3 bestaande integraties — geen speculatief universeel framework.
**Afhankelijkheden**: 3 bestaande integraties als stabiel contract vastgelegd; Stage C vereist ook trace-ID-propagatie voor volledige generalisatie.

### Write executor als aparte service
**Categorie**: architectuur
**Waarde**: minimaliseert het credential-oppervlak van de orchestrator. Schrijfacties (`complete_task`, `publish_correction_event`) draaien nu op hetzelfde service account als de read/diagnostische functies.
**Context**: de write executor is een aparte Cloud Function met beperkte credentials, uitsluitend geïnvoceerd na goedkeuring. Zie ADR-0018.
**Afhankelijkheden**: tool registry.

### Prompt-scoping: global vs. per-tenant (open beslissing)
**Categorie**: architectuur
**Waarde**: maakt het mogelijk dat tenants een afwijkende toon of prompts configureren zonder code-wijziging.
**Context**: `config/habits_coach_prompt` en `config/team_report_prompt` zijn globaal. Breekt zodra een tweede tenant een andere stijl wil. Richting: gedeelde default in `config/` + optionele override in `tenants/{tenant_id}`. Nog niet besloten.
**Afhankelijkheden**: geen technische blokkade; beslissing is leidend.

### `PIPELINE_STAGES` en `_search_stage_logs` — verifieer op dead code
**Categorie**: architectuur
**Waarde**: verwijdert verouderde code die verwarring schept bij toekomstige refactors van de staged investigation.
**Context**: `PIPELINE_STAGES` en `_search_stage_logs` in `slack-agent/main.py` zijn vermoedelijk overblijfselen van het vroegere log-walking approach, vervangen door `investigate_stage_c_bigquery`. Verifieer of ze nog worden aangeroepen. Standaardprocedure: beschrijven, bevestiging vragen, dan verwijderen.
**Afhankelijkheden**: verificatie — geen deployment-wijziging zonder bevestiging.

### Prompts slack-agent naar Firestore
**Categorie**: architectuur
**Waarde**: maakt het mogelijk om `CLASSIFICATION_PROMPT`, `INVESTIGATION_PROMPT`, `FOLLOWUP_NOTE_PROMPT` en `DATE_EXTRACTION_PROMPT` aan te passen zonder deployment. Blokkerende randvoorwaarde voor de wekelijkse agent-kwaliteitsreview — zonder dit kan geen voorstel uit de review worden doorgevoerd zonder nieuwe deployment.
**Context**: de vier prompts staan hardcoded in `slack-agent/main.py`. Dit schendt ADR-0014 (operationele configuratie leeft in Firestore). Patroon: naar `config/{prompt_id}` in Firestore, zelfde structuur als `config/habits_coach_prompt`. Fallback-strategie bij Firestore-read failure: `CLASSIFICATION_PROMPT` valt terug op hardcoded default (altijd actief op inkomende Slack-berichten); overige prompts falen hard met error naar Dennis. Raak een actieve, live service aan — vereist Dennis' akkoord voor deployment.
**Afhankelijkheden**: geen technische blokkade; design goedgekeurd 2026-07-01.

### Wekelijkse agent-kwaliteitsreview
**Categorie**: architectuur
**Waarde**: sluit de kwaliteitslus voor de slack-agent. Detecteert structurele problemen in classificatie en diagnose zonder dat Dennis handmatig logs doorspit — output is een concreet, gecijferd voorstel voor prompt- of drempelaanpassing.
**Context**: cross-tenant analyse op twee faalvlakken: (1) `CLASSIFICATION_PROMPT` — aandeel `unclear` in `signal_type`-verdeling; (2) `INVESTIGATION_PROMPT` + auto-resolve drempels — confidence calibratie (aandeel `confidence: high` auto-resolves met `reopened: true`). Deterministische Python-aggregatie op Firestore (`error_log`, `agent_sessions`), geen BigQuery-sync — zie ADR-0019. Gemini-synthese op geaggregeerde cijfers, nooit op ruwe signaal-tekst (PII-grens). Output: voorstel aan Dennis, geen automatische config-mutatie. Audit via `agent_reviews/{doc_id}`: voorstel, adoptie, recidive-vergelijking met vorige run. Losstaande function (`agent-quality-reviewer`) naar patroon `habits-coach-weekly` — migratie naar process registry later.
**Afhankelijkheden**: (1) prompts slack-agent naar Firestore — blokkeert uitvoerbaarheid van voorstellen; (2) schema-uitbreiding `error_log` — velden `reopened`, `confidence`, `original_error_log_doc_id` — blokkeert confidence calibratie.

---

## Onderzoek

### Welke HHI-interventies leiden aantoonbaar tot hogere retentie?
**Categorie**: onderzoek
**Waarde**: zonder deze kennis optimaliseert het systeem op activiteit, niet op uitkomst.
**Context**: zodra de feedbackloop gesloten is en voldoende historische data beschikbaar is, kan een analyse worden gedraaid op welke taaktypen en timings correleren met retentie na 30/90/180 dagen.
**Afhankelijkheden**: feedbackloop; minimaal 6 maanden historische data.

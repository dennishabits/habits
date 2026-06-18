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
**Context**: de `acuity-enricher` signaleert al onbekende e-mailadressen als een `crm_task` in `#taken`. Twee scenario's: (A) telefoon matcht wel in BigQuery → correct e-mailadres bekend, agent kan direct corrigeren; (B) geen match → medewerker moet handmatig zoeken. De Acuity API biedt `PATCH /appointments/{id}` voor e-mailadres updates. Dit item is onderdeel van de bredere integratieagent capability — zie "Integratieagent: signaleren en oplossen van datakwaliteitsproblemen".
**Afhankelijkheden**: `slack-agent` service.

### Integratieagent: signaleren en oplossen van datakwaliteitsproblemen
**Categorie**: feature
**Waarde**: integratieproblemen tussen systemen worden automatisch gedetecteerd, onderzocht en opgelost — met human-in-the-loop waar nodig. Vervangt handmatig debuggen en voorkomt stille fouten die leiden tot verkeerde retentiedata.
**Context**: Habits koppelt vier systemen: Sportivity (bron van waarheid voor klantdata), Acuity (afspraken), Customer.io (marketing automation) en BigQuery (analyse). Datakwaliteitsproblemen — zoals een klant met twee e-mailadressen — ontstaan onvermijdelijk door de indirecte koppeling tussen die systemen. De agent moet in staat zijn om: (1) de root cause te traceren via BigQuery-lookup, (2) het juiste e-mailadres op te halen uit Sportivity, (3) het aan te passen in Acuity via `PATCH /appointments/{id}`, (4) een dubbel profiel te detecteren in Customer.io via de App API, en (5) profielen te mergen via `POST /api/v1/merge_customers` na akkoord. Alle destructieve stappen (merge, delete) vereisen expliciete bevestiging via de Slack-thread.
**Afhankelijkheden**: `slack-agent` service; Customer.io App API access.

### Sportivity reconciliatie-job
**Categorie**: feature
**Waarde**: voorkomt permanent verlies van `subscription_update` events als Sportivity een webhook stuurt terwijl de pipeline tijdelijk down is. Sportivity herprobeert webhooks niet — als het bericht het `sportivity-enricher` niet bereikt, bestaat er geen herstelmechanisme. De DLQ helpt alleen voor berichten die al in Pub/Sub zitten; dit gaat over berichten die nooit aankomen.
**Context**: geïdentificeerd op 2026-06-13 bij analyse van gemiste taakafsluitingen. De oplossing is een periodieke job (Cloud Scheduler + Cloud Function) die:
1. Alle open `member_admin` en `subscription_change` taken in Firestore ophaalt die ouder zijn dan X uur (`created_at < now - Xh`, `completed: false`, `expired: false`)
2. Voor elke taak het huidige lidmaatschapsstatus ophaalt via de Sportivity API voor het bijbehorende e-mailadres
3. Als de status in Sportivity al verwerkt is (bijv. opzegging al ingevoerd), publiceert de job een synthetisch `subscription_update` event naar het `events` topic zodat de normale pipeline de taak alsnog afsluit
4. Logt gevonden discrepanties naar BigQuery voor patroonherkenning
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
**Categorie**: feature
**Waarde**: het coachingsbericht sluit af met een open vraag die het team beantwoordt in de thread. Door die antwoorden mee te geven aan het volgende rapport wordt coaching cumulatief — het systeem leert wat er speelt op de vloer en past zijn vragen en coaching aan op basis van eerder gegeven antwoorden.
**Context**: het employee-rapport vraagt het team al om te reageren in de thread. De antwoorden gaan nu nergens naartoe. De benodigde stappen: (1) `slack-agent` of `slack-listener` pikt thread-replies op rapport-berichten op, (2) antwoorden worden opgeslagen in Firestore per tenant, (3) `team-report` haalt de laatste N antwoorden op en geeft ze mee aan Gemini als context bij het genereren van het volgende rapport.
**Afhankelijkheden**: `slack-agent` service; Firestore opslag van thread-replies.

### Eén configureerbare slack-agent die alle processen afhandelt
**Categorie**: architectuur
**Waarde**: voorkomt proliferatie van losse agent-services per proces. De agent is generiek; processen zijn configuratie in Firestore per tenant. Maakt het toevoegen van nieuwe processen mogelijk zonder nieuwe deployments.
**Context**: nu zijn `habits-coach-reply` en `slack-agent` twee aparte services die hetzelfde basispatroon volgen: Slack bericht ontvangen → tenant lookup → intent herkenning → proces uitvoeren. Op termijn moet dit één service worden waarbij kanaal + intent bepalen welk proces wordt gestart. De assessment-laag (wat is de vraag?) en de process-laag (hoe beantwoord je die?) zijn gescheiden. Processen worden vastgelegd in `config/processes/{process_id}` met trigger_channels, trigger_intent, process_type en steps. De huidige `slack-agent` is al gebouwd met deze scheiding in gedachten — de stap naar volledig configureerbare processen is daarmee klein.
**Afhankelijkheden**: voldoende proceservaring om het configuratieformaat te kunnen generaliseren; minimaal twee processen in productie.

---

## Onderzoek

### Welke HHI-interventies leiden aantoonbaar tot hogere retentie?
**Categorie**: onderzoek
**Waarde**: zonder deze kennis optimaliseert het systeem op activiteit, niet op uitkomst.
**Context**: zodra de feedbackloop gesloten is en voldoende historische data beschikbaar is, kan een analyse worden gedraaid op welke taaktypen en timings correleren met retentie na 30/90/180 dagen.
**Afhankelijkheden**: feedbackloop; minimaal 6 maanden historische data.

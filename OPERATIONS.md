> Modus: Reference — operationele configuratie voor agents en ontwikkelaars: payload-formaten, Firestore-config, prompt-richtlijnen.

# Habits — Operations

## Gebruik

Dit document beschrijft operationele configuratie die agents en ontwikkelaars nodig hebben om taken uit te voeren zonder aanpassingen aan de code. Het bevat de exacte formaten, velden en richtlijnen voor de dagelijkse werking van het systeem.

---

## CRM Task Payloads (Customer.io)

Alle CRM-taken worden verstuurd vanuit Customer.io via een webhook naar `webhook-dispatcher`. `action_type` staat altijd op root-niveau van de payload — niet genest onder `payload`. Dit bepaalt welk completion-event de taak afsluit in de `task_performance` view.

### prospect_call

Bel een potentiële nieuwe klant en nodig uit voor een rondleiding.

```json
{
    "email": "{{customer.email}}",
    "task_type": "prospect_call",
    "action_type": "contact",
    "valid_minutes": 60,
    "payload": {
        "task_type": "prospect_call",
        "subject": "{{customer.firstname}} {{customer.lastname}}",
        "task_title": "Lead",
        "details": [
            {"label": "Telefoon", "value": "{{customer.phone_number}}"},
            {"label": "Email", "value": "{{customer.email}}"},
            {"label": "Product", "value": "{{event.product_interest}}"},
            {"label": "Bron", "value": "{{event.utm_medium | default: ''}}{% if event.utm_source != blank %}, {{event.utm_source}}{%endif%}"}
        ],
        "task_icon": "📞",
        "task_label": "Bel en nodig uit voor rondleiding",
        "note": "{% if event.message != blank %}{{event.message}}{%endif%}"
    }
}
```

### member_call (reactivatie — geen afspraak ingepland)

Bel een bestaand lid dat al lang niet geweest is.

```json
{
    "id": {{customer.id}},
    "task_type": "member_call",
    "action_type": "contact",
    "valid_minutes": 1440,
    "payload": {
        "task_type": "member_call",
        "subject": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?'}})",
        "task_title": "Reactivatie",
        "details": [],
        "task_icon": "📞",
        "task_label": "Bel en plan een Reboot afspraak in",
        "task_link": "https://basecamp.as.me/reboot?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}}",
        "note": "4 weken niet geweest.{% if customer.next_checkin_at != blank %} Laatste afspraak was op {{customer.next_checkin_at | date: '%d %b'}}.{%endif%}"
    }
}
```

### member_call (reactivatie — geen nieuwe FitCheck ingepland)

```json
{
    "id": "{{customer.id}}",
    "task_type": "member_call",
    "action_type": "contact",
    "valid_minutes": 1440,
    "payload": {
        "task_type": "member_call",
        "subject": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?'}})",
        "task_title": "Reactivatie",
        "details": [],
        "task_icon": "📅",
        "task_label": "Plan een Reboot afspraak in",
        "task_link": "https://basecamp.as.me/reboot?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}}",
        "note": "Geen nieuwe FitCheck ingepland.{% if customer.next_checkin_at != blank %} Laatste afspraak was op {{customer.next_checkin_at | date: '%d %b'}}.{%endif%}"
    }
}
```

### member_talk

Bezoekerstaak — bezoeker aanspreken op de vloer voor het inplannen van een FitCheck afspraak.

```json
{
    "id": {{customer.id}},
    "task_type": "member_talk",
    "action_type": "appointment",
    "valid_minutes": 45,
    "payload": {
        "task_type": "member_talk",
        "subject": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?'}})",
        "task_title": "FitCheck inplannen",
        "details": [],
        "task_icon": "📅",
        "task_label": "Plan een afspraak in",
        "task_link": "https://basecamp.as.me/FitCheck?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}}"
    }
}
```

### member_admin (nieuw lid)

Administratieve verwerking van een nieuw lid in Sportivity.

```json
{
    "email": "{{customer.email}}",
    "task_type": "member_admin",
    "action_type": "subscription",
    "valid_minutes": 1440,
    "payload": {
        "task_type": "member_admin",
        "subject": "{{customer.firstname}} {{customer.lastname}} ({%- assign birth_epoch = event.birth_date | plus: 0 -%}{%- assign seconds_per_year = 31557600 -%}{%- assign now_epoch = 'now' | date: '%s' | plus: 0 -%}{%- assign age = now_epoch | minus: birth_epoch | divided_by: seconds_per_year -%}{{ age | default: '?' | floor }})",
        "task_title": "Nieuw lid",
        "details": [
            {"label": "Email", "value": "{{customer.email}}"},
            {"label": "Geslacht", "value": "{% if customer.gender != blank %}{{customer.gender}}{%else%}{{event.gender}}{%endif%}"},
            {"label": "Adres", "value": "{% if customer.street != blank %}{{customer.street}}{%else%}{{event.street}}{%endif%} {% if customer.house_number != blank %}{{customer.house_number}}{%endif%}, {% if customer.zip != blank %}{{customer.zip}}{%else%}{{event.zip}}{%endif%} {% if customer.city != blank %}{{customer.city}}{%else%}{{event.city}}{%endif%}"},
            {"label": "Geboortedatum", "value": "{% if customer.birth_date != blank %}{{customer.birth_date | date: '%d-%m-%Y'}}{%else%}{{event.birth_date | date: '%d-%m-%Y'}}{%endif%}"},
            {"label": "Telefoon", "value": "{% if customer.phone_number != blank %}{{customer.phone_number}}{%else%}{{event.phone_number}}{%endif%}"},
            {"label": "Rekeningnummer", "value": "{% if customer.iban != blank %}{{customer.iban}}{%else%}{{event.iban}}{%endif%} tnv {% if customer.ccname != blank %}{{customer.ccname}}{%else%}{{event.ccname}}{%endif%}"},
            {"label": "Sportschool", "value": "{% if customer.brand != blank %}{{customer.brand}}{%else%}{{event.brand}}{%endif%}"},
            {"label": "Lidmaatschap", "value": "{{event.subscription_duration}} - €{{event.subscription_price}} per 4 weken - ({{event.contract}})", "bold": true},
            {"label": "Bron", "value": "{% if event.promotion != blank %}Actie: {{event.promotion}} • {% endif %}{% if event.traffic_source != blank %}{{event.traffic_source}}{% endif %}{% if event.pagename != blank %}, {{event.pagename}}{% endif %}"}
        ],
        "task_icon": "📋",
        "task_label": "Verwerk in ledenadministratie",
        "note": "{% if event.message != blank %}{{event.message}}{%endif%}"
    }
}
```

### member_admin (opzegging)

Administratieve verwerking van een opzegging in Sportivity.

```json
{
    "id": "{{customer.id}}",
    "email": "{{customer.email}}",
    "task_type": "member_admin",
    "action_type": "subscription",
    "valid_minutes": "1440",
    "payload": {
        "task_type": "member_admin",
        "subject": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?'}})",
        "task_title": "Opzegging",
        "details": [
            {"label": "Telefoon", "value": "{% if customer.phone_number != blank %}{{customer.phone_number}}{%else%}{{event.phone_number}}{%endif%}"},
            {"label": "Email", "value": "{{customer.email}}"}
        ],
        "task_icon": "📋",
        "task_label": "Check voorwaarden, verwerk in Sportivity en koppel terug aan lid.",
        "note": "{{ journey.cancellation_request | default: event.message }}"
    }
}
```

### evaluation

Evaluatie proefweek inplannen.

```json
{
    "id": {{customer.id}},
    "task_type": "evaluation",
    "action_type": "appointment",
    "valid_minutes": 1440,
    "subject": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?'}})",
    "task_title": "Evaluatie inplannen",
    "task_icon": "📅",
    "task_label": "Plan een afspraak in",
    "task_link": "https://basecamp.as.me/appointmentType=3265012?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}}",
    "note": "Dag 6 proefweek, maar geen evaluatie ingepland.",
    "details": [],
    "visible": true
}
```

### followup_appointment (FitCheck)

Invisible taak — bewaakt of er een nieuwe FitCheck afspraak ingepland is. Geen Slack-output.

```json
{
    "id": {{customer.id}},
    "context": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?' }})",
    "situation": "Er moet altijd een nieuwe FitCheck gepland worden.",
    "action": "<https://basecamp.as.me/FitCheck?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}} |Plan een nieuwe afspraak in>.",
    "action_type": "appointment",
    "activity": "{{event.activity}}",
    "valid_minutes": 45,
    "visible": false,
    "task_type": "followup_appointment"
}
```

### followup_appointment (Personal Training)

Invisible taak — bewaakt of er een nieuwe PT afspraak ingepland is. Geen Slack-output.

```json
{
    "id": {{customer.id}},
    "context": "{{customer.firstname}} {{customer.lastname}} ({{customer.age | default: '?' }})",
    "situation": "Er moet altijd een nieuwe PT afspraak gepland staan.",
    "action": "<https://basecamp.as.me/pt?firstName={{customer.firstname}}&lastName={% assign safe_lastname = customer.lastname | replace: ' ', '_' %}{{safe_lastname}}{% if customer.phone_number != blank %}&phone={{customer.phone_number}}{% endif %}&email={{customer.email}} |Plan een nieuwe afspraak in>.",
    "action_type": "appointment",
    "activity": "{{event.activity}}",
    "valid_minutes": 75,
    "visible": false,
    "task_type": "followup_appointment"
}
```

### fitcheck (expired)

Signaleert dat een FitCheck taak verlopen is.

```json
{
    "id": {{customer.id}},
    "action_type": "appointment",
    "task_type": "fitcheck",
    "status": "expired"
}
```

---

## Afspraaktypes

Acuity-afspraken worden opgeslagen in de `appointments` BigQuery view. Voor bepaalde activiteiten wordt bijgehouden of er een vervolgafspraak is ingepland (`followup_scheduled`).

### Followup-vereiste activiteiten

De `appointments` view bepaalt welke activiteiten een vervolgafspraak vereisen op basis van de aanwezigheid van `followup_appointment` CRM-taken in `raw_events`. Een activiteit verschijnt automatisch in `followup_required_activities` zodra er ooit een `followup_appointment` taak voor aangemaakt is.

**Huidige activiteiten met followup-vereiste:**
- `BioCircuit FitCheck` (en alle varianten — matching via `LIKE '%fitcheck%'`)

### Naamvarianten

Acuity-activiteitnamen wijzigen over tijd. De `appointments` view gebruikt een `LIKE '%fitcheck%'` match om alle historische varianten te ondervangen. Bij het toevoegen van nieuwe activiteiten met followup-vereiste: zorg dat de `followup_appointment` CRM-taak de exacte activiteitnaam meestuurt via `activity`.

---

## Firestore Configuratie

### `config/habits_coach_prompt`

| Veld | Type | Inhoud |
|---|---|---|
| `prompt` | string | Systeemprompt voor de AI-coaching sessies in `#coaching` |

### `config/team_report_prompt`

| Veld | Type | Inhoud |
|---|---|---|
| `management_prompt` | string | Prompt voor het managementrapport — zakelijk, cijfers centraal |
| `employee_prompt` | string | Prompt voor het medewerkersrapport — coachend, geen managementtaal |

### `tenants/{tenant_id}`

| Veld | Type | Inhoud |
|---|---|---|
| `slack_bot_token` | string | Slack bot token (`xoxb-...`) voor deze tenant |
| `slack_team_id` | string | Slack workspace ID |
| `slack_coach_channel` | string | Channel ID voor coaching (`#coaching`) |

---

## Prompt Richtlijnen

### Taaktype-omschrijvingen

Gebruik in prompts nooit de technische taaknamen. Gebruik altijd de omschrijving:

| Taaktype | Omschrijving voor gebruik in prompts |
|---|---|
| `prospect_call` | bel een potentiële nieuwe klant en nodig uit voor een rondleiding |
| `member_call` | bel een bestaand lid (reactivatie of follow-up) |
| `member_talk` | bezoekerstaak — bezoeker aanspreken voor FitCheck afspraak |
| `member_admin` | administratieve verwerking in ledenadministratie (nieuw lid of opzegging) |
| `fitcheck` | FitCheck afspraak inplannen |
| `evaluation` | evaluatie proefweek inplannen |

### Slack opmaak

Gebruik Slack-compatibele opmaak in alle prompts:
- `*vet*` voor koppen en nadruk
- Geen markdown headers met `#`
- Geen dubbele asterisken `**`

### Doelgroepen

**Management**: zakelijk, direct, cijfers centraal. Geen aansporingen of complimenten. Maximaal 150 woorden.

**Medewerkers (sportinstructeurs)**: coachend, direct, respectvol. Geen managementtaal, geen benchmarks, geen technische termen. Wissel de reflectievraag af tussen: tijdig zien van taken, prioritering tijdens drukke momenten, het gesprek aangaan met een bezoeker, wat een gemist contactmoment betekent voor een lid, hoe het team elkaar hierin ondersteunt. Maximaal 100 woorden.

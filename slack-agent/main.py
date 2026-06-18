import functions_framework
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from google.cloud import bigquery, firestore, pubsub_v1
from google.cloud import logging as gcp_logging
from google.genai import Client as GenAIClient

# Clients
bq_client = bigquery.Client()
fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()
logging_client = gcp_logging.Client(project="solid-future-452906-a2")

PROJECT_ID = "solid-future-452906-a2"
DATASET = "gym_analytics"
GEMINI_MODEL = "gemini-2.5-flash"
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# Pipeline stages walked in Stage C, in order
PIPELINE_STAGES = [
    "webhook-dispatcher",
    "acuity-enricher",
    "acuity-translator",
    "bigquery-listener",
    "customerio-listener",
]

CLASSIFICATION_PROMPT = """
Je bent een assistent die berichten van sportscholinstructeurs classificeert.

Context: de instructeur reageert in de thread van een openstaande taak. De taak staat nog open in het systeem.

Classificeer het bericht als:
- "discrepancy": de instructeur meldt (impliciet of expliciet) dat de taak al is uitgevoerd, terwijl die nog openstaat. Dit omvat korte voltooiingsmeldingen zoals "afspraak staat ingepland", "is gedaan", "al afgehandeld", "heb ik al gebeld", "staat al in het systeem" — berichten die impliceren dat de actie al heeft plaatsgevonden maar de taak nog openstaat.
- "negative_outcome": de instructeur geeft aan dat de taak is uitgevoerd maar het resultaat negatief was. Voorbeelden: "Heeft geen interesse", "Wil geen afspraak", "Heeft al een sportschool", "Niet geïnteresseerd", "Belt niet terug".
- "followup_requested": de instructeur geeft aan dat er vervolgcontact nodig is op een specifieke datum of in een specifieke week. Voorbeelden: "Terugbellen volgende week", "Bel haar in de week van 22 juni", "Over twee weken opnieuw proberen", "Bellen op 25 juni".
- "followup_vague": de instructeur geeft aan dat er mogelijk vervolgcontact nodig is maar zonder concrete datum of week. Voorbeelden: "Komt later terug", "Mogelijk interesse", "Wil nog nadenken", "Belt zelf nog", "Later proberen".
- "operational": de instructeur meldt iets over de uitvoering waarbij de taak nog NIET voltooid is en er geen vervolgafspraak is (bijv. "hij nam niet op, bel ik morgen terug", "probeer het vandaag nog").
- "unclear": het bericht gaat duidelijk over de taak, maar het is onduidelijk of die al gedaan is of niet.
- "irrelevant": het bericht heeft niets met de taakstatus te maken — een mention (@naam), intern overleg, een groet, een vraag aan een collega, of anderszins duidelijk buiten de taakcontext.

Twijfelregels:
- "negative_outcome" gaat voor "discrepancy" als het bericht een negatief resultaat bevat (geen interesse, al lid, wil niet).
- "followup_requested" vereist een concrete datum of tijdseenheid ("week van 22 juni", "volgende week", "over 2 weken"). Ontbreekt die → "followup_vague".
- Als een bericht een voltooiingsstatement bevat zonder negatief resultaat en zonder vervolg — kies "discrepancy".
- Als het bericht overduidelijk niet over de taak gaat — kies "irrelevant", niet "unclear".
- "unclear" alleen als het bericht wél over de taak lijkt te gaan maar de intentie echt onduidelijk is.

Voorbeelden:
- "@mark" → irrelevant
- "@lisa kun jij dit oppakken?" → irrelevant
- "hij nam niet op" → operational
- "afspraak staat ingepland" → discrepancy
- "heb ik gisteren al gedaan" → discrepancy
- "Heeft geen interesse" → negative_outcome
- "Wil nog nadenken" → followup_vague
- "Terugbellen week van 22 juni" → followup_requested
- "weet niet precies" → unclear

Als er gespreksgeschiedenis aanwezig is, gebruik die als context bij het classificeren.

Geef ALLEEN een JSON object terug, geen andere tekst:
{
  "classification": "discrepancy" | "negative_outcome" | "followup_requested" | "followup_vague" | "operational" | "unclear" | "irrelevant",
  "reason": "korte uitleg",
  "clarifying_question": "alleen invullen bij unclear: een concrete vraag die helpt de intentie te achterhalen. Max 1 zin, in het Nederlands, gericht op de taakcontext."
}
"""

INVESTIGATION_PROMPT = """
Je bent een diagnose-agent voor een gym management systeem. Je onderzoekt discrepanties in taakverwerking.

Je hebt toegang tot:
- Firestore taakstate (taakinformatie)
- BigQuery events (pipeline events rondom de taak)

BELANGRIJK: Baseer elke conclusie uitsluitend op concrete feiten uit de data. Speculeer niet op basis van wat de medewerker beweert.

Regels:
- Analyseer de beschikbare BigQuery events en Firestore data
- Elke conclusie moet onderbouwd zijn met bewijs dat je zelf ziet in de data
- Als de data de conclusie niet ondersteunt — kies "unknown" en escaleer
- Communiceer kalm en concreet richting de medewerker
- Geen jargon, geen architectuuruitleg

Kritische regels over taakstatus:
- Het veld `expired` (true/false) is de ENIGE indicator of een taak verlopen is. Als `expired: false`, is de taak NIET verlopen.
- Het veld `expires_at` is alleen administratieve metadata en heeft GEEN invloed op taakgedrag. Gebruik het NIET om te concluderen dat een taak verlopen is.
- `late_completion` mag ALLEEN gebruikt worden als `expired: true` én de actie aantoonbaar ná die vervaldatum heeft plaatsgevonden.

Root cause categorieën:
- pipeline_error: het voltooiingsevent staat wél in BigQuery maar de taak is toch niet voltooid. Vereist dat het event daadwerkelijk zichtbaar is in de BigQuery data.
- timezone_mismatch: datum/tijdproblemen door timezone conversie — alleen als er concrete tijdsverschillen zichtbaar zijn in de data.
- duplicate_email: klant heeft meerdere e-mailadressen — alleen als er events zijn voor verschillende emailadressen van dezelfde persoon.
- late_completion: actie uitgevoerd ná de vervaldatum — ALLEEN te gebruiken als `expired: true` én events de actietijd bevestigen.
- unknown: gebruik dit als de data de oorzaak niet onderbouwt. Dit is de veilige standaard bij twijfel.

KRITISCH — ontbrekende events zijn GEEN diagnose:
Als er geen relevante events in BigQuery staan, is de diagnose altijd "unknown". "Het event ontbreekt" is een observatie, niet een oorzaak. De vraag is dan: waarom ontbreekt het event? Dat vereist handmatig onderzoek — escaleer altijd naar Dennis met een duidelijke omschrijving van wat er verwacht werd en wat er gevonden is.

Resolution methods:
- pipeline_event: stuur een correctie-event via de pipeline om de taak te voltooien
- firestore_direct: markeer de taak direct als voltooid in Firestore (geen pipeline actie nodig)
- external_system: actie vereist in een extern systeem (Acuity, Sportivity, Customer.io)
- escalate: oorzaak onbekend of herstel niet mogelijk zonder menselijke input

Wanneer needs_dennis_approval FALSE is (agent lost autonoom op):
- root_cause is AANTOONBAAR vastgesteld via concrete events in de BigQuery data
- confidence is "high"
- resolution_method is pipeline_event of firestore_direct
- er zijn geen neveneffecten buiten de taakstatus zelf

Wanneer needs_dennis_approval TRUE is (escaleer naar Dennis) — gebruik dit als:
- root_cause is unknown
- confidence is "medium" of "low"
- er zijn geen BigQuery events die de claim van de medewerker onderbouwen
- resolution_method is external_system of escalate
- de fix heeft effecten buiten deze taak
- je twijfelt over de juiste aanpak

Geef een JSON object terug:
{
  "employee_message": "bericht voor de medewerker (max 2 zinnen, geen jargon)",
  "root_cause": "beschrijving van de oorzaak",
  "root_cause_category": "pipeline_error|timezone_mismatch|duplicate_email|late_completion|unknown",
  "evidence": "welke specifieke BigQuery events of Firestore velden de conclusie onderbouwen. Schrijf 'Geen ondersteunende events gevonden' als dat zo is.",
  "confidence": "high|medium|low",
  "resolution_possible": true|false,
  "resolution_method": "pipeline_event|firestore_direct|external_system|escalate",
  "resolution_description": "wat er gedaan moet worden om te herstellen",
  "needs_dennis_approval": true|false,
  "escalation_summary": "alleen invullen als needs_dennis_approval true is"
}
"""


FOLLOWUP_NOTE_PROMPT = """
Je schrijft een korte briefing voor een instructeur die een geplande follow-up taak oppakt.

Je krijgt berichten uit de Slack-thread van de oorspronkelijke taak, plus de datum waarop de follow-up is ingepland.

Schrijf 2-3 zinnen in het Nederlands, vanuit het perspectief van iemand die de follow-up oppakt op die datum. Gebruik de verleden tijd voor wat er eerder is besproken ("heeft contact gehad", "zou verhuisd zijn"). Vermeld wat er besproken is, wat de situatie was, en wat de afspraak was. Houd het feitelijk en beknopt — geen aanspreekvormen, geen groeten.

Geef ALLEEN een JSON object terug, geen andere tekst:
{"note": "de briefing tekst"}
"""


DATE_EXTRACTION_PROMPT = """
Je bent een datum-extractie-assistent. Extraheer een concrete vervolgdatum uit een Nederlands bericht.

Gebruik de meegeleverde huidige datum als referentie voor relatieve uitdrukkingen:
- "week van [dag] [maand]" → gebruik de maandag van die week
- "volgende week" → maandag van de volgende kalenderweek
- "over twee weken" → 14 dagen vanaf vandaag, afgerond naar de maandag van die week
- Concrete datum ("25 juni", "donderdag 3 juli") → gebruik direct

Geef ALLEEN een JSON object terug, geen andere tekst:
{
  "date": "YYYY-MM-DD",
  "readable": "maandag 22 juni"
}

Als er geen datum te extraheren is, geef dan:
{
  "date": null,
  "readable": null
}
"""


def log(data: dict):
    sys.stdout.write(json.dumps(data, default=str) + "\n")
    sys.stdout.flush()


def publish_error_event(service_name: str, error_description: str):
    try:
        topic_path = publisher.topic_path(PROJECT_ID, "events")
        error_payload = {
            "envelope": {
                "webhook_source": service_name,
                "event_type": "service_error",
                "tenant_id": "system"
            },
            "payload": {
                "service": service_name,
                "error": f"**{error_description}**",
                "notification_email": "dennis@habits.fit",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        publisher.publish(topic_path, json.dumps(error_payload).encode("utf-8"))
    except Exception as e:
        log({"PUBLISH_ERROR_FAILED": str(e)})


# ── SLACK ─────────────────────────────────────────────────────────────────────

def slack_post(token: str, channel: str, text: str, thread_ts: str = None) -> dict:
    payload = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
    response = session.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=10
    )
    return response.json()


def slack_chat_update(token: str, channel: str, ts: str, text: str) -> dict:
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
    response = session.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel, "ts": ts, "text": text, "mrkdwn": True},
        timeout=10
    )
    return response.json()


def slack_dm_dennis(token: str, dennis_user_id: str, text: str) -> dict:
    return slack_post(token=token, channel=dennis_user_id, text=text)


def send_dennis_investigation_report(
    token: str, dennis_user_id: str,
    task_doc_id: str, user_message: str,
    events: list, diagnosis: dict, resolution_taken: str,
    staged_findings: dict = None
):
    if events:
        lines = [f"  • {e.get('received_at', '?')} – {e.get('event_type', '?')}" for e in events[:5]]
        events_summary = f"{len(events)} events:\n" + "\n".join(lines)
        if len(events) > 5:
            events_summary += f"\n  … en {len(events) - 5} meer"
    else:
        events_summary = "Geen BigQuery events bevraagd (staged investigation gebruikt)" if staged_findings else "Geen events gevonden in BigQuery — onderzoek waarom er geen event is aangemaakt (webhook niet gefired? pipeline dropped? bronsysteem?)"

    confidence = diagnosis.get("confidence", "?")
    root_cause_category = diagnosis.get("root_cause_category", "unknown")
    evidence = diagnosis.get("evidence", "niet opgegeven")

    report = (
        f"*Diagnose rapport* — `{task_doc_id}`\n"
        f"*Signaal:* {user_message}\n"
        f"*BigQuery:* {events_summary}\n"
        f"*Root cause:* `{root_cause_category}` — {diagnosis.get('root_cause', '')}\n"
        f"*Bewijs:* {evidence}\n"
        f"*Confidence:* {confidence}\n"
        f"*Actie:* {resolution_taken}"
    )

    if staged_findings:
        sa = staged_findings.get("stage_a") or {}
        sb = staged_findings.get("stage_b")
        sc = staged_findings.get("stage_c")

        if sa.get("skipped"):
            stage_a_line = "⏭️ overgeslagen (Acuity niet geconfigureerd)"
        elif sa.get("found"):
            appt = sa.get("appointment") or {}
            stage_a_line = f"✅ afspraak gevonden — type: {appt.get('type', '?')}, datum: {appt.get('datetime', '?')}, aangemaakt: {appt.get('created_at', '?')}"
        elif sa.get("error"):
            stage_a_line = f"⚠️ fout bij bevragen: {sa['error']}"
        else:
            stage_a_line = "❌ geen actieve afspraak gevonden in Acuity"

        if sb is None:
            stage_b_line = "⏭️ niet uitgevoerd (Stage A niet conclusief)"
        elif sb.get("mismatch_found"):
            stage_b_line = f"❌ mismatch: {sb.get('mismatch_description', '')}"
        elif sb.get("error"):
            stage_b_line = f"⚠️ fout: {sb['error']}"
        else:
            stage_b_line = "✅ identiteit consistent"

        if sc is None:
            stage_c_line = "⏭️ niet uitgevoerd (Stage B conclusief)"
        elif sc.get("error"):
            stage_c_line = f"⚠️ fout bij log-query: {sc['error']}"
        elif sc.get("first_missing_stage"):
            stage_c_line = f"❌ drop bij `{sc['first_missing_stage']}` — {sc.get('evidence', '')}"
        else:
            stage_c_line = f"⚠️ aanwezig in alle stages — {sc.get('evidence', '')}"

        report += (
            f"\n\n*Staged investigation (Acuity appointment):*\n"
            f"  Stage A (Acuity bron): {stage_a_line}\n"
            f"  Stage B (Identity): {stage_b_line}\n"
            f"  Stage C (Pipeline): {stage_c_line}"
        )
        if sc and sc.get("stages_checked"):
            report += f"\n  Stages gecontroleerd: {', '.join(sc['stages_checked'])}"

    slack_dm_dennis(token=token, dennis_user_id=dennis_user_id, text=report)
    log({"TO_SLACK_DENNIS_REPORT": {"task_doc_id": task_doc_id, "confidence": confidence, "root_cause_category": root_cause_category}})


# ── FIRESTORE ─────────────────────────────────────────────────────────────────

def get_tenant_by_team_id(slack_team_id: str) -> tuple:
    docs = fs_client.collection("tenants") \
        .where(filter=firestore.FieldFilter("slack_team_id", "==", slack_team_id)) \
        .limit(1) \
        .stream()
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return doc.id, data
    return None, None


def get_channel_behavior(tenant: dict, channel_id: str) -> str | None:
    channels = tenant.get("slack_agent_channels", [])
    for ch in channels:
        if ch.get("channel_id") == channel_id:
            return ch.get("behavior")
    return None


def get_task_by_message_ts(tenant_id: str, message_ts: str) -> tuple:
    docs = fs_client.collection("slack_messages") \
        .where(filter=firestore.FieldFilter("tenant_id", "==", tenant_id)) \
        .where(filter=firestore.FieldFilter("message_ts", "==", message_ts)) \
        .limit(1) \
        .stream()
    for doc in docs:
        return doc.id, doc.to_dict()
    return None, None


def get_session(tenant_id: str, thread_ts: str) -> tuple:
    doc_id = f"{tenant_id}_{thread_ts}"
    doc = fs_client.collection("agent_sessions").document(doc_id).get()
    if doc.exists:
        return doc_id, doc.to_dict()
    return doc_id, None


def create_session(doc_id: str, data: dict):
    fs_client.collection("agent_sessions").document(doc_id).set(data)
    log({"TO_FIRESTORE_SESSION_CREATED": {"doc_id": doc_id, "status": data.get("status")}})


def update_session(doc_id: str, updates: dict):
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    fs_client.collection("agent_sessions").document(doc_id).update(updates)
    log({"TO_FIRESTORE_SESSION_UPDATED": {"doc_id": doc_id, "keys": list(updates.keys())}})


def append_to_session_conversation(doc_id: str, role: str, content: str):
    entry = {"role": role, "content": content, "timestamp": datetime.now(timezone.utc).isoformat()}
    fs_client.collection("agent_sessions").document(doc_id).update({
        "conversation": firestore.ArrayUnion([entry]),
        "updated_at": datetime.now(timezone.utc).isoformat()
    })


def complete_task(task_doc_id: str, task_data: dict, slack_token: str, label: str = None):
    task_action = task_data.get("task_action", "")
    visitor_name = task_data.get("visitor_name", "Bezoeker")
    if label is None:
        label = f"{task_action} ingepland" if task_action else "Taak voltooid"

    task_doc_ref = fs_client.collection("slack_messages").document(task_doc_id)

    if not task_data.get("visible", True):
        task_doc_ref.update({"completed": True, "expired": False, "completed_at": firestore.SERVER_TIMESTAMP})
        log({"TO_FIRESTORE_TASK_COMPLETED": {"doc_id": task_doc_id, "visible": False}})
        return

    message_ts = task_data.get("message_ts")
    channel = task_data.get("channel")

    if message_ts and channel:
        result = slack_chat_update(
            token=slack_token, channel=channel, ts=message_ts,
            text=f"✅ ~{visitor_name}~ • {label}"
        )
        log({"TO_SLACK_CHAT_UPDATE": {"doc_id": task_doc_id, "ok": result.get("ok"), "label": label}})

    task_doc_ref.update({"completed": True, "expired": False, "completed_at": firestore.SERVER_TIMESTAMP})
    log({"TO_FIRESTORE_TASK_COMPLETED": {"doc_id": task_doc_id, "visitor_name": visitor_name, "label": label}})


def count_pipeline_drop_occurrences(tenant_id: str) -> tuple:
    """Returns (total_count, distinct_day_count) for all pipeline_drop_* events for this tenant."""
    try:
        docs = fs_client.collection("error_log") \
            .where(filter=firestore.FieldFilter("tenant_id", "==", tenant_id)) \
            .stream()
        days = set()
        total = 0
        for doc in docs:
            data = doc.to_dict()
            if not data.get("root_cause_category", "").startswith("pipeline_drop"):
                continue
            total += 1
            created_at = data.get("created_at")
            if hasattr(created_at, "date"):
                days.add(created_at.date())
            elif created_at:
                try:
                    days.add(datetime.fromisoformat(str(created_at)).date())
                except Exception:
                    pass
        return total, len(days)
    except Exception as e:
        log({"PIPELINE_DROP_COUNT_ERROR": str(e)})
        return 0, 0


def write_error_log(doc_id: str, data: dict):
    fs_client.collection("error_log").document(doc_id).set(data)
    log({"TO_FIRESTORE_ERROR_LOG": {"doc_id": doc_id}})


def update_error_log(doc_id: str, updates: dict):
    fs_client.collection("error_log").document(doc_id).update(updates)
    log({"TO_FIRESTORE_ERROR_LOG_UPDATED": {"doc_id": doc_id, "keys": list(updates.keys())}})


# ── BIGQUERY ──────────────────────────────────────────────────────────────────

def get_events_for_task(tenant_id: str, customer_id: str, email: str, created_at: datetime) -> list:
    window_start = created_at - timedelta(hours=1)
    window_end = created_at + timedelta(hours=48)

    conditions = ["tenant_id = @tenant_id", "received_at BETWEEN @window_start AND @window_end"]
    params = [
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
        bigquery.ScalarQueryParameter("window_start", "TIMESTAMP", window_start.isoformat()),
        bigquery.ScalarQueryParameter("window_end", "TIMESTAMP", window_end.isoformat()),
    ]

    if customer_id:
        conditions.append("customer_id = @customer_id")
        params.append(bigquery.ScalarQueryParameter("customer_id", "STRING", str(customer_id)))
    elif email:
        conditions.append("LOWER(email) = @email")
        params.append(bigquery.ScalarQueryParameter("email", "STRING", email.lower()))

    query = f"""
    SELECT
        event_type,
        received_at,
        customer_id,
        email,
        JSON_VALUE(raw_payload, '$.status') AS status,
        JSON_VALUE(raw_payload, '$.task_type') AS task_type,
        JSON_VALUE(raw_payload, '$.action_type') AS action_type,
        JSON_VALUE(raw_payload, '$.type') AS appointment_type,
        event_id
    FROM `{PROJECT_ID}.{DATASET}.raw_events`
    WHERE {' AND '.join(conditions)}
    ORDER BY received_at ASC
    LIMIT 50
    """

    job_config = bigquery.QueryJobConfig(query_parameters=params)
    results = bq_client.query(query, job_config=job_config).result()
    events = [dict(row) for row in results]

    log({"QUERY_BIGQUERY_EVENTS": {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "email": email,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "event_count": len(events)
    }})

    return events


# ── GEMINI ────────────────────────────────────────────────────────────────────

def call_gemini_json(system_prompt: str, user_message: str) -> dict:
    client = GenAIClient(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[{"role": "user", "parts": [{"text": user_message}]}],
        config={"system_instruction": system_prompt, "response_mime_type": "application/json"}
    )
    raw = response.text.strip() if response.text else "{}"
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        log({"GEMINI_JSON_PARSE_ERROR": {"raw": raw[:200]}})
        return {}


def _prefilter_irrelevant(message: str) -> bool:
    import re
    stripped = message.strip()
    if re.fullmatch(r'(@\w+\s*)+', stripped):
        return True
    words = stripped.split()
    if words and words[0].startswith('@') and len(words) <= 2:
        return True
    return False


def classify_message(message: str, conversation: list = None) -> dict:
    if _prefilter_irrelevant(message):
        result = {"classification": "irrelevant", "reason": "pre-filter: mention or off-topic pattern"}
        log({"CLASSIFICATION": result})
        return result

    user_content = message
    if conversation:
        history_lines = [f"{m['role']}: {m['content']}" for m in conversation[-5:]]
        user_content = f"Gespreksgeschiedenis:\n{chr(10).join(history_lines)}\n\nNieuw bericht: {message}"
    result = call_gemini_json(CLASSIFICATION_PROMPT, user_content)
    log({"CLASSIFICATION": result})
    return result


def investigate_discrepancy(task_data: dict, events: list, employee_message: str) -> dict:
    context = {
        "employee_message": employee_message,
        "task": {
            "task_type": task_data.get("task_type"),
            "action_type": task_data.get("action_type"),
            "customer_id": task_data.get("customer_id"),
            "email": task_data.get("email"),
            "created_at": str(task_data.get("created_at")),
            # expired is the authoritative state; expires_at is admin metadata only and is intentionally omitted
            "is_expired": bool(task_data.get("expired", False)),
            "is_completed": bool(task_data.get("completed", False)),
            "task_action": task_data.get("task_action"),
            "visible": task_data.get("visible"),
        },
        "pipeline_events": events
    }
    result = call_gemini_json(INVESTIGATION_PROMPT, json.dumps(context, default=str))
    log({"INVESTIGATION_RESULT": result})
    return result


# ── CORRECTION ────────────────────────────────────────────────────────────────

def publish_correction_event(tenant_id: str, task_data: dict, task_doc_id: str):
    action_type = task_data.get("action_type", "")
    customer_id = task_data.get("customer_id")
    email = task_data.get("email", "")

    if action_type == "appointment":
        event_type = "appointment"
        payload = {
            "status": "new",
            "type": task_data.get("task_action", "FitCheck"),
            "activity": task_data.get("task_action", "FitCheck")
        }
    elif action_type == "subscription":
        event_type = "subscription_update"
        payload = {"status": "new"}
    else:
        event_type = "appointment"
        payload = {"status": "new"}

    correction_envelope = {
        "webhook_source": "slack-agent",
        "tenant_id": tenant_id,
        "event_type": event_type,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
        "email": email,
        "correction": True,
        "corrected_task_doc_id": task_doc_id,
        "payload": payload
    }

    topic_path = publisher.topic_path(PROJECT_ID, "events")
    publisher.publish(topic_path, json.dumps(correction_envelope).encode("utf-8"))

    log({"TO_EVENTS_CORRECTION": {
        "tenant_id": tenant_id,
        "task_doc_id": task_doc_id,
        "event_type": event_type,
        "customer_id": customer_id
    }})


# ── TASK LIFECYCLE EVENTS ────────────────────────────────────────────────────

def _task_template(task_data: dict) -> dict:
    return {k: v for k, v in {
        "task_type": task_data.get("task_type"),
        "action_type": task_data.get("action_type"),
        "valid_minutes": task_data.get("valid_minutes"),
        "task_title": task_data.get("task_title"),
        "task_icon": task_data.get("task_icon"),
        "task_label": task_data.get("task_label"),
    }.items() if v is not None}


def publish_task_event(tenant_id: str, task_doc_id: str, task_data: dict, event_type: str, extra_payload: dict = None):
    envelope = {
        "webhook_source": "slack-agent",
        "tenant_id": tenant_id,
        "event_type": event_type,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": task_data.get("customer_id") or task_data.get("email"),
        "email": task_data.get("email"),
        "payload": {
            "task_type": task_data.get("task_type"),
            **(extra_payload or {})
        }
    }
    envelope = {k: v for k, v in envelope.items() if v is not None}
    topic_path = publisher.topic_path(PROJECT_ID, "events")
    publisher.publish(topic_path, json.dumps(envelope, default=str).encode("utf-8"))
    log({"TO_EVENTS_TASK_EVENT": {"event_type": event_type, "task_doc_id": task_doc_id}})


def extract_followup_date(message: str) -> dict:
    today = datetime.now(AMSTERDAM_TZ).strftime("%Y-%m-%d")
    result = call_gemini_json(DATE_EXTRACTION_PROMPT, f"Vandaag is {today}.\nBericht: {message}")
    log({"DATE_EXTRACTION": result})
    return result


def generate_followup_note(conversation: list, user_message: str, followup_readable: str) -> str:
    employee_messages = [m["content"] for m in (conversation or []) if m.get("role") == "employee"]
    all_messages = employee_messages + ([user_message] if user_message not in employee_messages else [])
    context = "\n".join(f"- {m}" for m in all_messages)
    user_content = f"Follow-up datum: {followup_readable}\n\nBerichten uit de thread:\n{context}"
    result = call_gemini_json(FOLLOWUP_NOTE_PROMPT, user_content)
    note = result.get("note")
    log({"FOLLOWUP_NOTE_GENERATED": {"note": note}})
    return note or "\n".join(all_messages)


def handle_negative_outcome(
    tenant_id: str, tenant: dict, channel_id: str, thread_ts: str,
    user_message: str, session_doc_id: str, task_doc_id: str, task_data: dict, now: str
):
    slack_token = tenant.get("slack_bot_token")
    complete_task(task_doc_id, task_data, slack_token, label="Geen interesse")
    publish_task_event(tenant_id, task_doc_id, task_data, "task_completed", {
        "outcome": "negative",
        "note": user_message
    })
    reply = "Begrepen — taak afgesloten als afgehandeld zonder vervolg."
    slack_post(token=slack_token, channel=channel_id, text=reply, thread_ts=thread_ts)
    update_session(session_doc_id, {
        "status": "resolved",
        "conversation": firestore.ArrayUnion([
            {"role": "agent", "content": reply, "timestamp": now}
        ])
    })
    log({"NEGATIVE_OUTCOME_RESOLVED": {"task_doc_id": task_doc_id}})


def handle_followup_requested(
    tenant_id: str, tenant: dict, channel_id: str, thread_ts: str,
    user_message: str, session_doc_id: str, task_doc_id: str, task_data: dict, now: str,
    conversation: list = None
):
    slack_token = tenant.get("slack_bot_token")
    date_result = extract_followup_date(user_message)
    followup_date = date_result.get("date")
    followup_readable = date_result.get("readable") or followup_date

    if not followup_date:
        question = "Wanneer wil je deze persoon terugbellen? Geef een concrete datum of week, dan maak ik een nieuwe taak aan."
        slack_post(token=slack_token, channel=channel_id, text=question, thread_ts=thread_ts)
        update_session(session_doc_id, {
            "status": "pending_followup",
            "pending_intent": "followup_requested",
            "conversation": firestore.ArrayUnion([
                {"role": "agent", "content": question, "timestamp": now}
            ])
        })
        log({"FOLLOWUP_DATE_EXTRACTION_FAILED": {"session_doc_id": session_doc_id}})
        return

    note = generate_followup_note(conversation, user_message, followup_readable)

    complete_task(task_doc_id, task_data, slack_token, label=f"Terugbellen {followup_readable}")
    publish_task_event(tenant_id, task_doc_id, task_data, "task_completed", {
        "outcome": "followup_planned",
        "note": note
    })
    original_payload = task_data.get("payload", {})
    publish_task_event(tenant_id, task_doc_id, task_data, "task_followup_requested", {
        "followup_date": followup_date,
        "note": note,
        "original_task_doc_id": task_doc_id,
        **_task_template(task_data),
        **{k: v for k, v in {
            "product_interest": original_payload.get("product_interest"),
            "utm_medium": original_payload.get("utm_medium"),
            "utm_source": original_payload.get("utm_source"),
        }.items() if v is not None},
    })
    reply = f"Begrepen — ik plan een nieuwe taak op {followup_readable}."
    slack_post(token=slack_token, channel=channel_id, text=reply, thread_ts=thread_ts)
    update_session(session_doc_id, {
        "status": "resolved",
        "conversation": firestore.ArrayUnion([
            {"role": "agent", "content": reply, "timestamp": now}
        ])
    })
    log({"FOLLOWUP_REQUESTED_RESOLVED": {"task_doc_id": task_doc_id, "followup_date": followup_date}})


def handle_followup_vague(slack_token: str, channel_id: str, thread_ts: str, session_doc_id: str):
    question = "Wanneer wil je deze persoon terugbellen? Geef een concrete datum of week, dan maak ik een nieuwe taak aan."
    slack_post(token=slack_token, channel=channel_id, text=question, thread_ts=thread_ts)
    log({"TO_SLACK_FOLLOWUP_QUESTION": {"session_doc_id": session_doc_id}})


# ── SESSION STATE HANDLERS ────────────────────────────────────────────────────

def handle_awaiting_confirmation(
    session_doc_id: str,
    session: dict,
    slack_token: str,
    channel_id: str,
    thread_ts: str,
    user_message: str
):
    """Employee responded to 'De taak is nu correct verwerkt — klopt dit?'"""
    msg_lower = user_message.lower()
    affirmative = any(w in msg_lower for w in ["ja", "klopt", "correct", "goed", "ok", "prima", "juist", "yes", "👍"])
    negative = any(w in msg_lower for w in ["nee", "niet", "fout", "nope", "wrong", "onjuist", "👎"])

    now = datetime.now(timezone.utc).isoformat()

    if affirmative:
        update_session(session_doc_id, {
            "status": "resolved",
            "conversation": firestore.ArrayUnion([
                {"role": "employee", "content": user_message, "timestamp": now}
            ])
        })
        error_log_doc_id = session.get("error_log_doc_id")
        if error_log_doc_id:
            update_error_log(error_log_doc_id, {
                "employee_confirmed": True,
                "resolved_at": now
            })
        log({"SESSION_RESOLVED_BY_EMPLOYEE": {"session_doc_id": session_doc_id}})

    elif negative:
        update_session(session_doc_id, {
            "status": "investigating",
            "conversation": firestore.ArrayUnion([
                {"role": "employee", "content": user_message, "timestamp": now}
            ])
        })
        slack_post(
            token=slack_token, channel=channel_id,
            text="We kijken nogmaals wat er mis is gegaan.",
            thread_ts=thread_ts
        )
        log({"SESSION_REOPENED": {"session_doc_id": session_doc_id}})

    else:
        slack_post(
            token=slack_token, channel=channel_id,
            text="Klopt de verwerking nu, of is er nog iets mis?",
            thread_ts=thread_ts
        )
        append_to_session_conversation(session_doc_id, "employee", user_message)
        log({"SESSION_CONFIRMATION_UNCLEAR": {"session_doc_id": session_doc_id}})


# ── STAGED INVESTIGATION (appointment case) ───────────────────────────────────

def _service_enabled(tenant: dict, service: str) -> bool:
    """
    Check if a service is enabled for this tenant.
    Respects the enabledServices field if present; falls back to credential presence.
    """
    enabled = tenant.get("enabledServices")
    if enabled is not None:
        return service in enabled
    if service == "acuity":
        return bool(tenant.get("acuityConfig", {}).get("apiKey"))
    if service == "sportivity":
        return bool(tenant.get("sportivityToken"))
    if service == "customerio":
        return bool(tenant.get("customerio", {}).get("app_api_key"))
    return False


def investigate_stage_a_acuity(tenant: dict, email: str) -> dict:
    if not _service_enabled(tenant, "acuity"):
        log({"STAGE_A_SKIPPED": {"reason": "acuity not enabled"}})
        return {"found": False, "appointment": None, "skipped": True, "error": None}

    acuity_config = tenant.get("acuityConfig", {})
    api_key = acuity_config.get("apiKey")
    user_id = acuity_config.get("userId")

    if not api_key or not user_id:
        log({"STAGE_A_ERROR": {"reason": "missing acuity credentials"}})
        return {"found": False, "appointment": None, "skipped": False, "error": "missing acuity credentials"}

    try:
        now_ams = datetime.now(AMSTERDAM_TZ)
        min_date = (now_ams - timedelta(days=90)).strftime("%Y-%m-%d")
        max_date = (now_ams + timedelta(days=180)).strftime("%Y-%m-%d")

        resp = requests.get(
            "https://acuityscheduling.com/api/v1/appointments",
            auth=(user_id, api_key),
            params={"email": email.lower(), "max": 20, "minDate": min_date, "maxDate": max_date},
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        appointments = resp.json()

        active = [a for a in appointments if not a.get("canceled", False)]

        log({"STAGE_A_QUERY": {
            "email": email,
            "total_returned": len(appointments),
            "active_count": len(active),
            "window": f"{min_date} – {max_date}"
        }})

        if not active:
            return {"found": False, "appointment": None, "skipped": False, "error": None}

        # Sort by datetime descending — most upcoming / most recent first
        active.sort(key=lambda a: a.get("datetime", ""), reverse=True)
        appt = active[0]

        # Extract only non-PII scheduling metadata — no name, phone, notes
        result = {
            "id": appt.get("id"),
            "type": appt.get("type"),
            "datetime": appt.get("datetime"),
            "created_at": appt.get("createdAt"),
            "appointment_type_id": appt.get("appointmentTypeID"),
        }
        log({"STAGE_A_RESULT": {
            "found": True,
            "appointment_id": result["id"],
            "appointment_type": result["type"],
            "appointment_datetime": result["datetime"],
            "created_at": result["created_at"]
        }})
        return {"found": True, "appointment": result, "skipped": False, "error": None}

    except Exception as e:
        log({"STAGE_A_ERROR": {"error": str(e)}})
        return {"found": False, "appointment": None, "skipped": False, "error": str(e)}


def investigate_stage_b_identity(tenant: dict, customer_id: str, task_email: str) -> dict:
    """
    Checks whether the customer_id in the task maps to the expected email
    in Sportivity and Customer.io. A mismatch means the customer exists
    under two identities, which would cause pipeline matching to fail.
    PII rule: only the boolean mismatch result and a non-PII description are returned.
    """
    mismatches = []
    task_email_lower = (task_email or "").lower()

    # Sportivity — only possible with a customer_id; no customer_id means not in Sportivity
    if customer_id and _service_enabled(tenant, "sportivity"):
        sportivity_token = tenant.get("sportivityToken")
        if sportivity_token:
            try:
                url = f"https://www.sportivity.info/sportivity-api/Customers/{customer_id}?Mem=true"
                resp = requests.get(
                    url,
                    headers={"accept": "application/json", "X-API-TOKEN": sportivity_token},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sportivity_email = (
                        data.get("Email") or data.get("email") or ""
                    ).lower()
                    if sportivity_email and task_email_lower and sportivity_email != task_email_lower:
                        mismatches.append("Sportivity email voor dit customer_id verschilt van het email in de taak")
                    log({"STAGE_B_SPORTIVITY": {
                        "customer_id": customer_id,
                        "emails_match": sportivity_email == task_email_lower
                    }})
                elif resp.status_code == 404:
                    log({"STAGE_B_SPORTIVITY": {"not_found": True, "customer_id": customer_id}})
                else:
                    log({"STAGE_B_SPORTIVITY_ERROR": {"status": resp.status_code}})
            except Exception as e:
                log({"STAGE_B_SPORTIVITY_ERROR": {"error": str(e)}})

    # Customer.io App API
    if _service_enabled(tenant, "customerio"):
        cio_key = tenant.get("customerio", {}).get("app_api_key")
        if cio_key and customer_id:
            try:
                resp = requests.get(
                    f"https://api.customer.io/v1/customers/{customer_id}",
                    headers={"Authorization": f"Bearer {cio_key}"},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # App API wraps the profile under "customer"
                    customer = data.get("customer", data)
                    cio_email = (customer.get("email") or "").lower()
                    if cio_email and task_email_lower and cio_email != task_email_lower:
                        mismatches.append("Customer.io email voor dit customer_id verschilt van het email in de taak")
                    log({"STAGE_B_CUSTOMERIO": {
                        "customer_id": customer_id,
                        "emails_match": cio_email == task_email_lower
                    }})
                elif resp.status_code == 404:
                    log({"STAGE_B_CUSTOMERIO": {"not_found": True, "customer_id": customer_id}})
                else:
                    log({"STAGE_B_CUSTOMERIO_ERROR": {"status": resp.status_code}})
            except Exception as e:
                log({"STAGE_B_CUSTOMERIO_ERROR": {"error": str(e)}})

    mismatch_found = len(mismatches) > 0
    description = "; ".join(mismatches) if mismatches else "identiteit consistent"
    log({"STAGE_B_RESULT": {"mismatch_found": mismatch_found}})
    return {
        "identity_clean": not mismatch_found,
        "mismatch_found": mismatch_found,
        "mismatch_description": description,
        "skipped": False,
        "error": None
    }


def _search_stage_logs(stage_name: str, search_terms: list, start_time: datetime, end_time: datetime) -> tuple:
    """
    Returns (found: bool | None, entry_count: int).
    None means the query itself failed — caller treats this as indeterminate.
    """
    terms = [t for t in search_terms if t]
    filter_parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{stage_name}"',
        f'timestamp>="{start_time.isoformat()}"',
        f'timestamp<="{end_time.isoformat()}"',
    ]
    if terms:
        # OR-join search terms so either customer_id or email triggers a hit
        terms_clause = " OR ".join(f'"{t}"' for t in terms)
        filter_parts.append(f"({terms_clause})")

    filter_str = "\n".join(filter_parts)
    try:
        entries = list(logging_client.list_entries(filter_=filter_str, page_size=5))
        return len(entries) > 0, len(entries)
    except Exception as e:
        log({"STAGE_C_LOG_QUERY_ERROR": {"stage": stage_name, "error": str(e)}})
        return None, 0


def investigate_stage_c_pipeline(customer_id: str, email: str, anchor_time: datetime) -> dict:
    window_start = anchor_time - timedelta(minutes=5)
    window_end = anchor_time + timedelta(hours=2)

    search_terms = []
    if customer_id:
        search_terms.append(str(customer_id))
    if email:
        search_terms.append(email.lower())

    stages_checked = []
    first_missing = None
    query_errors = []

    for stage in PIPELINE_STAGES:
        found, count = _search_stage_logs(stage, search_terms, window_start, window_end)
        log({"STAGE_C_STAGE_RESULT": {"stage": stage, "found": found, "entry_count": count}})

        if found is None:
            # Log query failed for this stage — record but don't treat as a drop
            query_errors.append(stage)
            continue

        stages_checked.append(stage)
        if not found:
            first_missing = stage
            break

    if first_missing:
        preceding = [s for s in stages_checked if s != first_missing]
        evidence = (
            f"Event aanwezig in: {', '.join(preceding) if preceding else 'geen'}. "
            f"Absent bij: {first_missing}. "
            f"Tijdvenster: {window_start.isoformat()} – {window_end.isoformat()}."
        )
    elif query_errors:
        evidence = f"Log-queries mislukten voor: {', '.join(query_errors)}. Stages gecontroleerd: {', '.join(stages_checked)}."
    elif len(stages_checked) == len(PIPELINE_STAGES):
        evidence = (
            f"Event aanwezig in alle {len(PIPELINE_STAGES)} pipeline-stages. "
            f"Anomalie: taak is toch niet voltooid — handmatig onderzoek vereist."
        )
    else:
        evidence = f"Gecontroleerde stages: {', '.join(stages_checked)}. Geen drop-punt gevonden."

    log({"STAGE_C_RESULT": {
        "first_missing_stage": first_missing,
        "stages_checked": stages_checked,
        "query_errors": query_errors
    }})
    return {
        "first_missing_stage": first_missing,
        "stages_checked": stages_checked,
        "query_errors": query_errors,
        "evidence": evidence,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "error": "; ".join(query_errors) if query_errors and not stages_checked else None
    }


def investigate_discrepancy_appointment(
    tenant_id: str, tenant: dict, task_data: dict, employee_message: str
) -> dict:
    """
    Three-stage source-truth-first investigation for action_type == 'appointment'.
    Always returns resolution_possible: False and needs_dennis_approval: True (Stage 1).
    Staged findings are included for the Dennis report and error_log.
    PII rule: no raw API profiles are persisted or passed to Gemini.
    """
    email = (task_data.get("email") or "").lower()
    customer_id = task_data.get("customer_id")

    log({"APPOINTMENT_INVESTIGATION_START": {
        "task_type": task_data.get("task_type"),
        "customer_id": customer_id,
        "email": email
    }})

    # ── Stage A: Acuity source truth ──────────────────────────────────────────
    stage_a = investigate_stage_a_acuity(tenant, email)

    if not stage_a.get("found"):
        if stage_a.get("skipped"):
            employee_msg = "We kunnen de afspraken niet controleren op dit moment. Melding gemaakt van het probleem."
            category = "unknown"
            confidence = "low"
            root_cause = "Acuity niet geconfigureerd voor deze tenant — Stage A overgeslagen"
        elif stage_a.get("error"):
            employee_msg = "Er is een technisch probleem bij het controleren van de afspraak. Melding gemaakt van het probleem."
            category = "unknown"
            confidence = "low"
            root_cause = f"Stage A mislukt door technische fout: {stage_a['error']}"
        else:
            employee_msg = "De afspraak staat niet in het systeem onder dit e-mailadres. Mogelijk is die ingepland onder een ander e-mailadres of staat er toch geen afspraak."
            category = "appointment_not_in_source"
            confidence = "high"
            root_cause = f"Geen actieve afspraak gevonden in Acuity voor e-mailadres {email} in het venster −90/+180 dagen"

        log({"APPOINTMENT_INVESTIGATION_CONCLUDED": {"stage": "A", "category": category}})
        return {
            "root_cause_category": category,
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence": f"Acuity bevraagd voor {email}; {stage_a.get('error') or 'geen actieve afspraken gevonden'}",
            "needs_dennis_approval": True,
            "resolution_possible": False,
            "resolution_method": "escalate",
            "resolution_description": "Controleer bij de medewerker of de afspraak onder een ander e-mailadres staat, of dat er inderdaad geen afspraak is",
            "employee_message": employee_msg,
            "staged_findings": {"stage_a": stage_a, "stage_b": None, "stage_c": None}
        }

    appt = stage_a["appointment"]

    # ── Stage B: Identity reconciliation ─────────────────────────────────────
    stage_b = investigate_stage_b_identity(tenant, str(customer_id) if customer_id else None, email)

    if stage_b.get("mismatch_found"):
        log({"APPOINTMENT_INVESTIGATION_CONCLUDED": {"stage": "B", "category": "identity_mismatch"}})
        return {
            "root_cause_category": "identity_mismatch",
            "root_cause": f"Identiteitsmismatch: {stage_b['mismatch_description']}",
            "confidence": "high",
            "evidence": stage_b["mismatch_description"],
            "needs_dennis_approval": True,
            "resolution_possible": False,
            "resolution_method": "escalate",
            "resolution_description": "Account-merge vereist in Sportivity en/of Customer.io — Dennis beslist over de aanpak",
            "employee_message": "We zien de afspraak staan in Acuity, maar er lijkt een verschil in gegevens te zitten tussen de systemen. Melding gemaakt van het probleem.",
            "staged_findings": {"stage_a": stage_a, "stage_b": stage_b, "stage_c": None}
        }

    # ── Stage C: Pipeline log walk ────────────────────────────────────────────
    created_at_str = appt.get("created_at")
    try:
        anchor_time = datetime.fromisoformat(str(created_at_str).replace("Z", "+00:00"))
        if anchor_time.tzinfo is None:
            anchor_time = anchor_time.replace(tzinfo=timezone.utc)
    except Exception:
        anchor_time = datetime.now(timezone.utc) - timedelta(hours=1)
        log({"STAGE_C_ANCHOR_FALLBACK": {"created_at_str": created_at_str}})

    stage_c = investigate_stage_c_pipeline(
        str(customer_id) if customer_id else None, email, anchor_time
    )

    if stage_c.get("first_missing_stage"):
        drop_stage = stage_c["first_missing_stage"]
        category = f"pipeline_drop_{drop_stage.replace('-', '_')}"
        log({"APPOINTMENT_INVESTIGATION_CONCLUDED": {"stage": "C", "category": category}})
        return {
            "root_cause_category": category,
            "root_cause": f"Event weggevallen bij pipeline-stage '{drop_stage}'",
            "confidence": "medium",
            "evidence": stage_c["evidence"],
            "needs_dennis_approval": True,
            "resolution_possible": False,
            "resolution_method": "escalate",
            "resolution_description": f"Pipeline drop bij '{drop_stage}' — Dennis onderzoekt de oorzaak en bepaalt replay-strategie",
            "employee_message": "We zien de afspraak staan in Acuity, maar hebben deze hier niet ontvangen. Melding gemaakt van het probleem.",
            "staged_findings": {"stage_a": stage_a, "stage_b": stage_b, "stage_c": stage_c}
        }

    # All stages present — event flowed through the full pipeline but task didn't close
    log({"APPOINTMENT_INVESTIGATION_CONCLUDED": {"stage": "C", "category": "unknown", "reason": "all stages present"}})
    return {
        "root_cause_category": "unknown",
        "root_cause": "Afspraak aanwezig in Acuity, identiteit consistent, event aanwezig in alle pipeline-stages — anomalie",
        "confidence": "low",
        "evidence": stage_c["evidence"],
        "needs_dennis_approval": True,
        "resolution_possible": False,
        "resolution_method": "escalate",
        "resolution_description": "Handmatig onderzoek door Dennis — alle systemen tonen de afspraak maar de taak is niet gesloten",
        "employee_message": "We zien de afspraak staan in Acuity, maar kunnen niet achterhalen waar de verwerking is gestopt. Melding gemaakt van het probleem.",
        "staged_findings": {"stage_a": stage_a, "stage_b": stage_b, "stage_c": stage_c}
    }


# ── TASK INVESTIGATION HANDLER ────────────────────────────────────────────────

def handle_task_investigation(
    tenant_id: str,
    tenant: dict,
    channel_id: str,
    thread_ts: str,
    user_message: str,
    event_ts: str
):
    slack_token = tenant.get("slack_bot_token")
    dennis_user_id = tenant.get("slack_dennis_user_id", "U158QLHEF")
    now = datetime.now(timezone.utc).isoformat()

    # Step 1: Check for existing session
    session_doc_id, session = get_session(tenant_id, thread_ts)

    if session:
        status = session.get("status")
        log({"SESSION_FOUND": {"doc_id": session_doc_id, "status": status}})

        if status == "pending_followup":
            append_to_session_conversation(session_doc_id, "employee", user_message)
            task_doc_id = session.get("task_doc_id")
            task_doc = fs_client.collection("slack_messages").document(task_doc_id).get() if task_doc_id else None
            task_data = task_doc.to_dict() if task_doc and task_doc.exists else {}
            handle_followup_requested(
                tenant_id, tenant, channel_id, thread_ts, user_message,
                session_doc_id, task_doc_id, task_data, now,
                conversation=session.get("conversation", [])
            )
            return

        if status == "awaiting_employee_confirmation":
            handle_awaiting_confirmation(
                session_doc_id, session, slack_token, channel_id, thread_ts, user_message
            )
            return

        if status == "awaiting_dennis_approval":
            append_to_session_conversation(session_doc_id, "employee", user_message)
            log({"SESSION_DENNIS_PENDING_MESSAGE_LOGGED": {"session_doc_id": session_doc_id}})
            return

        if status == "resolved":
            task_doc_id_check = session.get("task_doc_id")
            task_is_open = False
            if task_doc_id_check:
                task_doc_check = fs_client.collection("slack_messages").document(task_doc_id_check).get()
                if task_doc_check.exists:
                    td = task_doc_check.to_dict()
                    if not td.get("completed", False):
                        task_is_open = True
            if not task_is_open:
                log({"SESSION_ALREADY_RESOLVED": {"session_doc_id": session_doc_id}})
                return
            log({"SESSION_RESOLVED_BUT_TASK_OPEN": {"session_doc_id": session_doc_id, "task_doc_id": task_doc_id_check}})

        # status in ("investigating", "unclear") — re-classify with conversation context
        conversation = session.get("conversation", [])
        classification = classify_message(user_message, conversation)
        signal_type = classification.get("classification", "unclear")

        if signal_type == "irrelevant":
            log({"SKIPPED": {"reason": "irrelevant message in existing session", "session_doc_id": session_doc_id}})
            return

        append_to_session_conversation(session_doc_id, "employee", user_message)

        if signal_type == "operational":
            update_session(session_doc_id, {"status": "resolved"})
            log({"SESSION_OPERATIONAL_UPDATE": {"session_doc_id": session_doc_id}})
            return

        if signal_type == "unclear":
            update_session(session_doc_id, {"status": "unclear"})
            question = classification.get("clarifying_question") or "Bedoel je dat de taak al gedaan is maar nog openstaat?"
            slack_post(token=slack_token, channel=channel_id, text=question, thread_ts=thread_ts)
            log({"TO_SLACK_CLARIFICATION": {"thread_ts": thread_ts, "question": question}})
            return

        if signal_type == "negative_outcome":
            task_doc_id = session.get("task_doc_id")
            task_doc = fs_client.collection("slack_messages").document(task_doc_id).get() if task_doc_id else None
            task_data = task_doc.to_dict() if task_doc and task_doc.exists else {}
            update_session(session_doc_id, {"status": "resolving"})
            handle_negative_outcome(tenant_id, tenant, channel_id, thread_ts, user_message, session_doc_id, task_doc_id, task_data, now)
            return

        if signal_type == "followup_requested":
            task_doc_id = session.get("task_doc_id")
            task_doc = fs_client.collection("slack_messages").document(task_doc_id).get() if task_doc_id else None
            task_data = task_doc.to_dict() if task_doc and task_doc.exists else {}
            update_session(session_doc_id, {"status": "resolving"})
            handle_followup_requested(tenant_id, tenant, channel_id, thread_ts, user_message, session_doc_id, task_doc_id, task_data, now, conversation=session.get("conversation", []))
            return

        if signal_type == "followup_vague":
            update_session(session_doc_id, {"status": "pending_followup", "pending_intent": "followup_requested"})
            handle_followup_vague(slack_token, channel_id, thread_ts, session_doc_id)
            return

        # signal_type == "discrepancy" — continue investigation with task from session
        update_session(session_doc_id, {"status": "investigating"})
        task_doc_id = session.get("task_doc_id")
        task_doc = fs_client.collection("slack_messages").document(task_doc_id).get() if task_doc_id else None
        task_data = task_doc.to_dict() if task_doc and task_doc.exists else {}

    else:
        # No session — fresh start
        log({"SESSION_NOT_FOUND": {"doc_id": session_doc_id}})

        task_doc_id, task_data = get_task_by_message_ts(tenant_id, thread_ts)

        if not task_doc_id:
            log({"NO_TASK_FOUND": {"thread_ts": thread_ts}})
            return

        log({"TASK_FOUND": {
            "doc_id": task_doc_id,
            "task_type": task_data.get("task_type"),
            "completed": task_data.get("completed"),
            "expired": task_data.get("expired")
        }})

        classification = classify_message(user_message)
        signal_type = classification.get("classification", "unclear")

        if signal_type == "irrelevant":
            log({"SKIPPED": {"reason": "irrelevant message, no session created", "thread_ts": thread_ts}})
            return

        _status_map = {
            "discrepancy": "investigating",
            "negative_outcome": "resolving",
            "followup_requested": "resolving",
            "followup_vague": "pending_followup",
        }
        create_session(session_doc_id, {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "task_doc_id": task_doc_id,
            "status": _status_map.get(signal_type, signal_type),
            "signal": user_message,
            "signal_type": signal_type,
            "pending_intent": "followup_requested" if signal_type == "followup_vague" else None,
            "conversation": [{"role": "employee", "content": user_message, "timestamp": now}],
            "diagnosis": None,
            "resolution_method": None,
            "error_log_doc_id": None,
            "created_at": now,
            "updated_at": now
        })

        if signal_type == "operational":
            write_error_log(f"{task_doc_id}_{event_ts}", {
                "tenant_id": tenant_id,
                "task_doc_id": task_doc_id,
                "signal": user_message,
                "signal_type": "operational",
                "investigation_steps": [],
                "root_cause": None,
                "root_cause_category": None,
                "resolution": None,
                "resolution_method": None,
                "approved_by": "agent",
                "employee_confirmed": None,
                "created_at": now,
                "resolved_at": now
            })
            return

        if signal_type == "unclear":
            question = classification.get("clarifying_question") or "Bedoel je dat de taak al gedaan is maar nog openstaat?"
            slack_post(token=slack_token, channel=channel_id, text=question, thread_ts=thread_ts)
            log({"TO_SLACK_CLARIFICATION": {"thread_ts": thread_ts, "question": question}})
            return

        if signal_type == "negative_outcome":
            handle_negative_outcome(tenant_id, tenant, channel_id, thread_ts, user_message, session_doc_id, task_doc_id, task_data, now)
            return

        if signal_type == "followup_requested":
            handle_followup_requested(tenant_id, tenant, channel_id, thread_ts, user_message, session_doc_id, task_doc_id, task_data, now)
            return

        if signal_type == "followup_vague":
            handle_followup_vague(slack_token, channel_id, thread_ts, session_doc_id)
            return

    # ── Full discrepancy investigation ────────────────────────────────────────

    slack_post(
        token=slack_token, channel=channel_id,
        text="We kijken wat er is misgegaan.",
        thread_ts=thread_ts
    )
    log({"TO_SLACK_INVESTIGATING": {"thread_ts": thread_ts}})

    customer_id = task_data.get("customer_id")
    email = task_data.get("email")
    created_at = task_data.get("created_at")
    action_type = task_data.get("action_type")

    if hasattr(created_at, "isoformat"):
        created_at_dt = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
    else:
        created_at_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    investigation_steps = ["firestore"]

    if action_type == "appointment":
        # Staged source-truth-first investigation — no BigQuery query
        investigation_steps.append("acuity_stage_a")
        diagnosis = investigate_discrepancy_appointment(tenant_id, tenant, task_data, user_message)
        events = []
        staged_findings = diagnosis.get("staged_findings")
        sf = staged_findings or {}
        if sf.get("stage_b") is not None:
            investigation_steps.append("identity_stage_b")
        if sf.get("stage_c") is not None:
            investigation_steps.append("pipeline_stage_c")
    else:
        # Non-appointment: existing BigQuery + Gemini path
        events = get_events_for_task(tenant_id, str(customer_id) if customer_id else None, email, created_at_dt)
        investigation_steps.append("bigquery")
        diagnosis = investigate_discrepancy(task_data, events, user_message)
        staged_findings = None

    if not diagnosis:
        error_text = "Er is een technisch probleem opgetreden bij het analyseren. Probeer het opnieuw."
        slack_post(token=slack_token, channel=channel_id, text=error_text, thread_ts=thread_ts)
        update_session(session_doc_id, {
            "status": "investigating",
            "conversation": firestore.ArrayUnion([
                {"role": "agent", "content": error_text, "timestamp": datetime.now(timezone.utc).isoformat()}
            ])
        })
        return

    root_cause = diagnosis.get("root_cause", "")
    root_cause_category = diagnosis.get("root_cause_category", "unknown")
    resolution_possible = diagnosis.get("resolution_possible", False)
    resolution_method = diagnosis.get("resolution_method", "escalate")
    needs_dennis = diagnosis.get("needs_dennis_approval", True)
    confidence = diagnosis.get("confidence", "low")
    employee_message_text = diagnosis.get("employee_message", "Er is iets misgegaan in de verwerking.")

    # Patroondetectie voor pipeline_drop — communiceer herhaling aan medewerker en Dennis
    pipeline_drop_prev_count = 0
    pipeline_drop_prev_days = 0
    is_recurring_pipeline_drop = False
    if root_cause_category.startswith("pipeline_drop"):
        pipeline_drop_prev_count, pipeline_drop_prev_days = count_pipeline_drop_occurrences(tenant_id)
        total_with_current = pipeline_drop_prev_count + 1
        is_recurring_pipeline_drop = total_with_current > 5 or pipeline_drop_prev_days >= 2
        if is_recurring_pipeline_drop:
            employee_message_text = (
                f"{employee_message_text} "
                f"Dit probleem is al {total_with_current} keer opgetreden — we pakken dit structureel op."
            )
        log({"PIPELINE_DROP_PATTERN": {
            "total_with_current": total_with_current,
            "distinct_days": pipeline_drop_prev_days,
            "is_recurring": is_recurring_pipeline_drop
        }})

    update_session(session_doc_id, {
        "diagnosis": diagnosis,
        "resolution_method": resolution_method,
        "conversation": firestore.ArrayUnion([
            {"role": "agent", "content": employee_message_text, "timestamp": datetime.now(timezone.utc).isoformat()}
        ])
    })

    slack_post(
        token=slack_token, channel=channel_id,
        text=employee_message_text,
        thread_ts=thread_ts
    )
    log({"TO_SLACK_DIAGNOSIS": {"thread_ts": thread_ts, "message": employee_message_text}})

    error_log_doc_id = f"{task_doc_id}_{event_ts}"
    resolution = None
    approved_by = None

    # For action_type == "appointment" in Stage 1, the diagnosis contract guarantees
    # resolution_possible: False and needs_dennis_approval: True, so can_auto_resolve
    # is always False for that path without needing an explicit gate here.
    can_auto_resolve = (
        resolution_possible
        and not needs_dennis
        and confidence == "high"
        and resolution_method in ("pipeline_event", "firestore_direct")
    )

    if can_auto_resolve:
        if resolution_method == "pipeline_event":
            publish_correction_event(tenant_id, task_data, task_doc_id)
            resolution = "Correctie-event gepubliceerd via pipeline + taak voltooid"
        else:
            resolution = "Taak direct voltooid in Firestore"

        complete_task(task_doc_id, task_data, slack_token)
        approved_by = "agent"

        confirmation_text = "De taak is nu correct verwerkt — klopt dit?"
        slack_post(
            token=slack_token, channel=channel_id,
            text=confirmation_text,
            thread_ts=thread_ts
        )
        log({"TO_SLACK_RESOLVED": {"thread_ts": thread_ts}})

        update_session(session_doc_id, {
            "status": "awaiting_employee_confirmation",
            "error_log_doc_id": error_log_doc_id,
            "conversation": firestore.ArrayUnion([
                {"role": "agent", "content": confirmation_text, "timestamp": datetime.now(timezone.utc).isoformat()}
            ])
        })

        send_dennis_investigation_report(
            token=slack_token, dennis_user_id=dennis_user_id,
            task_doc_id=task_doc_id, user_message=user_message,
            events=events, diagnosis=diagnosis,
            resolution_taken=f"✅ Autonoom opgelost — {resolution}",
            staged_findings=staged_findings
        )

    elif resolution_possible:
        fix_proposal = diagnosis.get("resolution_description") or diagnosis.get("escalation_summary") or root_cause
        resolution = f"Voorstel ter goedkeuring: {fix_proposal}"
        approved_by = "dennis"
        log({"TO_DENNIS_APPROVAL_REQUESTED": {"task_doc_id": task_doc_id}})

        update_session(session_doc_id, {
            "status": "awaiting_dennis_approval",
            "error_log_doc_id": error_log_doc_id
        })

        send_dennis_investigation_report(
            token=slack_token, dennis_user_id=dennis_user_id,
            task_doc_id=task_doc_id, user_message=user_message,
            events=events, diagnosis=diagnosis,
            resolution_taken=f"⚠️ Goedkeuring gevraagd — {fix_proposal}",
            staged_findings=staged_findings
        )

    else:
        resolution = f"Geen fix mogelijk: {root_cause or 'oorzaak onbekend'}"
        approved_by = None
        log({"TO_DENNIS_INFORMED": {"task_doc_id": task_doc_id}})

        update_session(session_doc_id, {
            "status": "resolved",
            "error_log_doc_id": error_log_doc_id
        })

        send_dennis_investigation_report(
            token=slack_token, dennis_user_id=dennis_user_id,
            task_doc_id=task_doc_id, user_message=user_message,
            events=events, diagnosis=diagnosis,
            resolution_taken=f"ℹ️ Onderzoek afgerond — geen automatische fix mogelijk: {root_cause or 'oorzaak onbekend'}",
            staged_findings=staged_findings
        )

    write_error_log(error_log_doc_id, {
        "tenant_id": tenant_id,
        "task_doc_id": task_doc_id,
        "signal": user_message,
        "signal_type": "discrepancy",
        "investigation_steps": investigation_steps,
        "root_cause": root_cause,
        "root_cause_category": root_cause_category,
        "resolution": resolution,
        "resolution_method": resolution_method,
        "approved_by": approved_by,
        "employee_confirmed": False,
        "staged_findings": staged_findings,
        "pipeline_drop_count": (pipeline_drop_prev_count + 1) if root_cause_category.startswith("pipeline_drop") else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": datetime.now(timezone.utc).isoformat() if approved_by == "agent" else None
    })

    if is_recurring_pipeline_drop:
        total_with_current = pipeline_drop_prev_count + 1
        slack_dm_dennis(
            token=slack_token, dennis_user_id=dennis_user_id,
            text=(
                f"⚠️ Patroon gedetecteerd: `{root_cause_category}` is nu *{total_with_current}x* "
                f"opgetreden over *{pipeline_drop_prev_days} dag(en)*. "
                f"Drempel voor structurele aanpak bereikt — zie backlog: *Acuity reconciliatie-job*."
            )
        )
        log({"TO_DENNIS_PIPELINE_DROP_PATTERN": {
            "total": total_with_current,
            "days": pipeline_drop_prev_days
        }})


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

@functions_framework.http
def slack_agent(request):
    body = request.get_json(silent=True) or {}

    log({"INPUT": {
        "type": body.get("type"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }})

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}, 200

    event = body.get("event", {})
    event_type = event.get("type")
    subtype = event.get("subtype")

    if event_type != "message":
        log({"SKIPPED": {"reason": "not a message event"}})
        return {"status": "ignored"}, 200

    if subtype in ("message_deleted", "message_changed", "bot_message"):
        log({"SKIPPED": {"reason": f"subtype {subtype}"}})
        return {"status": "ignored"}, 200

    if event.get("bot_id"):
        log({"SKIPPED": {"reason": "bot message"}})
        return {"status": "ignored"}, 200

    thread_ts = event.get("thread_ts")
    event_ts = event.get("ts")
    if not thread_ts or thread_ts == event_ts:
        log({"SKIPPED": {"reason": "not a thread reply"}})
        return {"status": "ignored"}, 200

    user_message = event.get("text", "").strip()
    slack_team_id = body.get("team_id")
    channel_id = event.get("channel")

    log({"EVENT": {
        "team_id": slack_team_id,
        "channel": channel_id,
        "thread_ts": thread_ts,
        "message_preview": user_message[:100]
    }})

    try:
        tenant_id, tenant = get_tenant_by_team_id(slack_team_id)
        if not tenant_id:
            log({"TENANT_NOT_FOUND": {"slack_team_id": slack_team_id}})
            return {"status": "tenant not found"}, 200

        behavior = get_channel_behavior(tenant, channel_id)
        if not behavior:
            log({"SKIPPED": {"reason": "channel not configured for slack-agent", "channel": channel_id}})
            return {"status": "ignored"}, 200

        log({"CHANNEL_BEHAVIOR": {"channel": channel_id, "behavior": behavior}})

        if behavior == "task_investigation":
            handle_task_investigation(
                tenant_id=tenant_id,
                tenant=tenant,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_message=user_message,
                event_ts=event_ts
            )

        return {"status": "ok"}, 200

    except Exception as e:
        import traceback
        log({"ERROR": str(e), "TRACEBACK": traceback.format_exc()})
        publish_error_event("slack-agent", str(e))
        return {"status": "error"}, 200

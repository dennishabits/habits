import functions_framework
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from google.cloud import bigquery, firestore, pubsub_v1
from google.genai import Client as GenAIClient

# Clients
bq_client = bigquery.Client()
fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()

PROJECT_ID = "solid-future-452906-a2"
DATASET = "gym_analytics"
GEMINI_MODEL = "gemini-2.5-flash"
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

CLASSIFICATION_PROMPT = """
Je bent een assistent die berichten van sportscholinstructeurs classificeert.

Context: de instructeur reageert in de thread van een openstaande taak. De taak staat nog open in het systeem.

Classificeer het bericht als:
- "discrepancy": de instructeur meldt (impliciet of expliciet) dat de taak al is uitgevoerd, terwijl die nog openstaat. Dit omvat korte voltooiingsmeldingen zoals "afspraak staat ingepland", "is gedaan", "al afgehandeld", "heb ik al gebeld", "staat al in het systeem", "komt volgende week" — berichten die impliceren dat de actie al heeft plaatsgevonden.
- "operational": de instructeur meldt iets over de uitvoering waarbij de taak nog NIET voltooid is (bijv. "hij nam niet op, bel ik morgen terug", "probeer het vandaag nog").
- "unclear": het is echt onduidelijk of de taak al gedaan is of niet.

Twijfelregel: als een bericht een voltooiingsstatement bevat — ook al ontbreekt de expliciete klacht over de openstaande taak — kies dan "discrepancy".

Als er gespreksgeschiedenis aanwezig is, gebruik die als context bij het classificeren.

Geef ALLEEN een JSON object terug, geen andere tekst:
{"classification": "discrepancy" | "operational" | "unclear", "reason": "korte uitleg"}
"""

INVESTIGATION_PROMPT = """
Je bent een diagnose-agent voor een gym management systeem. Je onderzoekt discrepanties in taakverwerking.

Je hebt toegang tot:
- Firestore taakstate (taakinformatie)
- BigQuery events (pipeline events rondom de taak)

Regels:
- Analyseer de beschikbare data en identificeer de root cause
- Communiceer kalm en concreet richting de medewerker
- Geen jargon, geen architectuuruitleg
- Stel één gerichte vraag als iets onduidelijk is

Root cause categorieën:
- timezone_mismatch: datum/tijdproblemen door timezone conversie
- duplicate_email: klant heeft meerdere e-mailadressen in verschillende systemen
- late_completion: afspraak ingepland na verlopen van de taak
- pipeline_error: technisch probleem in de verwerking
- unknown: oorzaak niet vast te stellen

Geef een JSON object terug:
{
  "employee_message": "bericht voor de medewerker (max 2 zinnen, geen jargon)",
  "root_cause": "beschrijving van de oorzaak",
  "root_cause_category": "timezone_mismatch|duplicate_email|late_completion|pipeline_error|unknown",
  "resolution_possible": true|false,
  "resolution_method": "pipeline_event|firestore_direct|external_system|escalate",
  "resolution_description": "wat er gedaan moet worden om te herstellen",
  "needs_dennis_approval": true|false,
  "escalation_summary": "alleen invullen als needs_dennis_approval true is"
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


def slack_dm_dennis(token: str, dennis_user_id: str, text: str) -> dict:
    return slack_post(token=token, channel=dennis_user_id, text=text)


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
        config={"system_instruction": system_prompt}
    )
    raw = response.text.strip() if response.text else "{}"
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log({"GEMINI_JSON_PARSE_ERROR": {"raw": raw[:200]}})
        return {}


def classify_message(message: str, conversation: list = None) -> dict:
    user_content = message
    if conversation:
        # Last 5 turns for context; role + content only — no structured PII
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
            "expires_at": str(task_data.get("expires_at")),
            "completed": task_data.get("completed"),
            "expired": task_data.get("expired"),
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
        # Employee says resolution was wrong — reopen
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

        if status == "awaiting_employee_confirmation":
            handle_awaiting_confirmation(
                session_doc_id, session, slack_token, channel_id, thread_ts, user_message
            )
            return

        if status == "awaiting_dennis_approval":
            # Log the message but don't re-investigate — waiting for Dennis
            append_to_session_conversation(session_doc_id, "employee", user_message)
            log({"SESSION_DENNIS_PENDING_MESSAGE_LOGGED": {"session_doc_id": session_doc_id}})
            return

        if status == "resolved":
            log({"SESSION_ALREADY_RESOLVED": {"session_doc_id": session_doc_id}})
            return

        # status in ("investigating", "unclear") — re-classify with conversation context
        conversation = session.get("conversation", [])
        classification = classify_message(user_message, conversation)
        signal_type = classification.get("classification", "unclear")

        append_to_session_conversation(session_doc_id, "employee", user_message)

        if signal_type == "operational":
            update_session(session_doc_id, {"status": "resolved"})
            log({"SESSION_OPERATIONAL_UPDATE": {"session_doc_id": session_doc_id}})
            return

        if signal_type == "unclear":
            update_session(session_doc_id, {"status": "unclear"})
            slack_post(
                token=slack_token, channel=channel_id,
                text="Bedoel je dat de taak al gedaan is maar nog openstaat?",
                thread_ts=thread_ts
            )
            log({"TO_SLACK_CLARIFICATION": {"thread_ts": thread_ts}})
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

        create_session(session_doc_id, {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "task_doc_id": task_doc_id,
            "status": "investigating" if signal_type == "discrepancy" else signal_type,
            "signal": user_message,
            "signal_type": signal_type,
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
            slack_post(
                token=slack_token, channel=channel_id,
                text="Bedoel je dat de taak al gedaan is maar nog openstaat?",
                thread_ts=thread_ts
            )
            log({"TO_SLACK_CLARIFICATION": {"thread_ts": thread_ts}})
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

    if hasattr(created_at, "isoformat"):
        created_at_dt = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
    else:
        created_at_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    investigation_steps = ["firestore"]
    events = get_events_for_task(tenant_id, str(customer_id) if customer_id else None, email, created_at_dt)
    investigation_steps.append("bigquery")

    diagnosis = investigate_discrepancy(task_data, events, user_message)

    root_cause = diagnosis.get("root_cause", "")
    root_cause_category = diagnosis.get("root_cause_category", "unknown")
    resolution_possible = diagnosis.get("resolution_possible", False)
    resolution_method = diagnosis.get("resolution_method", "escalate")
    needs_dennis = diagnosis.get("needs_dennis_approval", True)
    employee_message_text = diagnosis.get("employee_message", "Er is iets misgegaan in de verwerking.")

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

    if resolution_possible and not needs_dennis and resolution_method == "pipeline_event":
        publish_correction_event(tenant_id, task_data, task_doc_id)
        resolution = "Correctie-event gepubliceerd via pipeline"
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

    else:
        escalation_summary = diagnosis.get("escalation_summary", root_cause)
        escalation_message = (
            f"⚠️ *Escalatie foutonderzoek*\n"
            f"Taak: `{task_doc_id}`\n"
            f"Signaal: {user_message}\n"
            f"Onderzocht: {', '.join(investigation_steps)}\n"
            f"Gevonden: {root_cause}\n"
            f"Voorstel: {escalation_summary}"
        )
        slack_dm_dennis(token=slack_token, dennis_user_id=dennis_user_id, text=escalation_message)
        resolution = f"Geëscaleerd naar Dennis: {escalation_summary}"
        approved_by = "dennis"
        log({"TO_SLACK_ESCALATION": {"dennis_user_id": dennis_user_id}})

        update_session(session_doc_id, {
            "status": "awaiting_dennis_approval",
            "error_log_doc_id": error_log_doc_id
        })

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
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": datetime.now(timezone.utc).isoformat() if approved_by == "agent" else None
    })


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

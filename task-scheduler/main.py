import base64
import hashlib
import json
import os
import requests
import functions_framework
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google.cloud import pubsub_v1, firestore


PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
ACUITY_APPOINTMENT_TYPE_ID = 3261919
AVAILABILITY_MIN_MINUTES = 10
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

firestore_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()
events_topic = publisher.topic_path(PROJECT_ID, "events")


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def hash_email(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


# ── Acuity ────────────────────────────────────────────────────────────────────

def get_acuity_credentials(tenant_id):
    tenant_doc = firestore_client.collection("tenants").document(tenant_id).get()
    if not tenant_doc.exists:
        raise ValueError(f"Tenant {tenant_id} not found")
    acuity_config = tenant_doc.to_dict().get("acuityConfig", {})
    user_id = acuity_config.get("userId")
    api_key = acuity_config.get("apiKey")
    if not user_id or not api_key:
        raise ValueError(f"Missing Acuity credentials for tenant {tenant_id}")
    return user_id, api_key


def get_acuity_calendars(user_id, api_key):
    response = requests.get(
        "https://acuityscheduling.com/api/v1/calendars",
        auth=(user_id, api_key),
        timeout=10
    )
    response.raise_for_status()
    calendars = response.json()
    log_json("ENRICHMENT_ACUITY_CALENDARS", {
        "count": len(calendars),
        "calendars": [{"id": c["id"], "name": c["name"]} for c in calendars]
    })
    return calendars


def get_acuity_availability(user_id, api_key, calendar_id, date_str):
    response = requests.get(
        "https://acuityscheduling.com/api/v1/availability/times",
        auth=(user_id, api_key),
        params={
            "appointmentTypeID": ACUITY_APPOINTMENT_TYPE_ID,
            "calendarID": calendar_id,
            "date": date_str
        },
        timeout=10
    )
    response.raise_for_status()
    return response.json()


def has_sufficient_availability(slots, expires_at_dt):
    """Return True if any slot starts at least AVAILABILITY_MIN_MINUTES before expiry"""
    for slot in slots:
        slot_time_str = slot.get("time")
        if not slot_time_str:
            continue
        try:
            slot_dt = datetime.fromisoformat(slot_time_str)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=timezone.utc)
            if slot_dt + timedelta(minutes=AVAILABILITY_MIN_MINUTES) <= expires_at_dt:
                return True
        except Exception as e:
            print(f"⚠️ Could not parse slot time '{slot_time_str}': {e}")
    return False


def check_availability(user_id, api_key, expires_at_dt):
    """
    Check all calendars for a slot of at least AVAILABILITY_MIN_MINUTES before expires_at.
    Returns (is_available: bool, calendar_id: int|None)
    """
    calendars = get_acuity_calendars(user_id, api_key)
    now_amsterdam = datetime.now(AMSTERDAM_TZ)
    date_str = now_amsterdam.strftime("%Y-%m-%d")

    for calendar in calendars:
        calendar_id = calendar["id"]
        try:
            slots = get_acuity_availability(user_id, api_key, calendar_id, date_str)
            log_json("ENRICHMENT_ACUITY_AVAILABILITY", {
                "calendar_id": calendar_id,
                "calendar_name": calendar.get("name"),
                "date": date_str,
                "slot_count": len(slots)
            })
            if has_sufficient_availability(slots, expires_at_dt):
                return True, calendar_id
        except Exception as e:
            print(f"⚠️ Error checking availability for calendar {calendar_id}: {e}")

    return False, None


# ── Timing ────────────────────────────────────────────────────────────────────

def calculate_expires_at(received_at_str, valid_minutes):
    try:
        if received_at_str:
            if received_at_str.endswith("Z"):
                received_at_str = received_at_str.replace("Z", "+00:00")
            received_at = datetime.fromisoformat(received_at_str)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        else:
            received_at = datetime.now(timezone.utc)
        return received_at + timedelta(minutes=int(valid_minutes))
    except Exception as e:
        print(f"⚠️ Error calculating expires_at: {e}")
        return datetime.now(timezone.utc)


# ── Firestore ─────────────────────────────────────────────────────────────────

def save_task_to_firestore(envelope, availability_status, expires_at_dt):
    tenant_id = envelope.get("tenant_id", "unknown")
    task_type = envelope.get("task_type", "")
    customer_id = envelope.get("customer_id")
    email = envelope.get("email")
    event_id = envelope.get("event_id")

    now = datetime.now(AMSTERDAM_TZ)
    task_date = now.strftime("%Y-%m-%d")

    if task_type == "fitcheck" and customer_id:
        doc_id = f"{tenant_id}_tasks_{customer_id}_{task_date}"
    elif email:
        doc_id = f"{tenant_id}_tasks_{task_type}_{hash_email(email)}_{task_date}"
    else:
        doc_id = f"{tenant_id}_tasks_{task_type}_{event_id or int(now.timestamp())}_{task_date}"

    doc_data = {
        "tenant_id": tenant_id,
        "task_type": task_type,
        "task_date": task_date,
        "availability_status": availability_status,
        "created_at": firestore.SERVER_TIMESTAMP,
        "expires_at": expires_at_dt,
        "completed": False,
        "expired": False,
        "message_type": "crm_task",
        "received_at": envelope.get("received_at"),
        "event_id": event_id,
        "payload": envelope.get("payload", {})
    }
    if customer_id:
        doc_data["customer_id"] = customer_id
    if email:
        doc_data["email"] = email

    firestore_client.collection("slack_messages").document(doc_id).set(doc_data)
    return doc_id


# ── Output ────────────────────────────────────────────────────────────────────

def build_output_envelope(envelope, availability_status):
    output = dict(envelope)
    if availability_status == "unavailable":
        existing_details = output.get("event_details", "")
        output["event_details"] = f"⚠️ {existing_details}" if existing_details else "⚠️ Geen beschikbare medewerker"
    output["availability_status"] = availability_status
    return output


def publish_to_events(envelope):
    message_data = json.dumps(envelope).encode("utf-8")
    publisher.publish(events_topic, message_data).result()


# ── Entry point ───────────────────────────────────────────────────────────────

@functions_framework.cloud_event
def task_scheduler(cloud_event):
    """Gen 2 Pub/Sub function — availability gate for CRM tasks"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        event_type = envelope.get("event_type", "")
        tenant_id = envelope.get("tenant_id", "")

        if event_type != "crm_task":
            print(f"Skipping non-crm_task event: {event_type}")
            return "OK"

        if not tenant_id:
            print("❌ Missing tenant_id")
            return "OK"

        # Calculate expiry
        valid_minutes = envelope.get("task_valid_minutes", 30)
        received_at = envelope.get("received_at")
        expires_at_dt = calculate_expires_at(received_at, valid_minutes)

        log_json("TASK_TIMING", {
            "received_at": received_at,
            "valid_minutes": valid_minutes,
            "expires_at": expires_at_dt.isoformat()
        })

        # Check Acuity availability
        try:
            user_id, api_key = get_acuity_credentials(tenant_id)
            is_available, calendar_id = check_availability(user_id, api_key, expires_at_dt)
        except Exception as e:
            print(f"❌ Acuity check failed: {e}")
            is_available = False
            calendar_id = None

        availability_status = "pending" if is_available else "unavailable"

        log_json("AVAILABILITY_RESULT", {
            "is_available": is_available,
            "calendar_id": calendar_id,
            "availability_status": availability_status,
            "expires_at": expires_at_dt.isoformat()
        })

        # Save to Firestore
        try:
            doc_id = save_task_to_firestore(envelope, availability_status, expires_at_dt)
            log_json("FIRESTORE_TASK_CREATED", {
                "doc_id": doc_id,
                "availability_status": availability_status
            })
        except Exception as e:
            print(f"⚠️ Firestore save failed (continuing): {e}")

        # Publish to events
        output_envelope = build_output_envelope(envelope, availability_status)
        publish_to_events(output_envelope)

        log_json("OUTPUT", output_envelope)

        return "OK"

    except Exception as e:
        print(f"❌ task-scheduler error: {e}")
        import traceback
        print(f"🐛 Traceback: {traceback.format_exc()}")

        try:
            error_event = {
                "webhook_source": "task-scheduler",
                "tenant_id": "system",
                "event_type": "service_error",
                "timestamp": datetime.utcnow().isoformat(),
                "email": "dennis@habits.fit",
                "payload": {
                    "service": "task-scheduler",
                    "error": f"**{str(e)}**"
                }
            }
            publisher.publish(events_topic, json.dumps(error_event).encode("utf-8")).result()
        except Exception as publish_err:
            print(f"❌ Failed to publish error event: {publish_err}")

        raise

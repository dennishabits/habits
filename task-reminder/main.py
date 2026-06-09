import base64
import json
import os
import hashlib
import functions_framework
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.cloud import firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
TAKEN_CHANNEL_ID = "C0ATAT7UTE0"
DENNIS_SLACK_ID = "D1KPR28A0"
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

EXCLUDED_TASK_TYPES = ["fitcheck"]

firestore_client = firestore.Client()
slack_clients = {}


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def get_slack_client(tenant_id):
    global slack_clients
    if tenant_id in slack_clients:
        return slack_clients[tenant_id]
    try:
        tenant_doc = firestore_client.collection("tenants").document(tenant_id).get()
        if not tenant_doc.exists:
            return None
        bot_token = tenant_doc.to_dict().get("slack_bot_token")
        if not bot_token or not bot_token.startswith("xoxb-"):
            return None
        client = WebClient(token=bot_token)
        slack_clients[tenant_id] = client
        return client
    except Exception as e:
        print(f"❌ Error creating Slack client for {tenant_id}: {e}")
        return None


def format_datetime(dt):
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_amsterdam = dt.astimezone(AMSTERDAM_TZ)
        dutch_months = {
            1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
            7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec"
        }
        month = dutch_months.get(dt_amsterdam.month, str(dt_amsterdam.month))
        return f"{dt_amsterdam.day} {month}, {dt_amsterdam.strftime('%H:%M')}"
    except Exception as e:
        print(f"❌ Error formatting datetime: {e}")
        return "Onbekende tijd"


def hash_email(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def build_reminder_message(task_data, reminder_count):
    visitor_name = task_data.get("visitor_name", "Onbekend")
    task_type = task_data.get("task_type", "")
    situation = task_data.get("task_situation", "")
    action = task_data.get("task_action", "")
    original_created_at = task_data.get("created_at")
    reminder_nr = reminder_count + 1

    if isinstance(original_created_at, datetime):
        original_time = format_datetime(original_created_at)
    else:
        original_time = "Onbekende tijd"

    now_str = format_datetime(datetime.now(AMSTERDAM_TZ))

    if task_type == "lead_call":
        header = f"*Lead: {visitor_name}*"
        task_line = f"📞 Taak: Bel terug • {now_str}"
    elif task_type == "order_processing":
        header = f"*Order: {visitor_name}*"
        task_line = f"📋 Taak: Verwerk in ledenadministratie • {now_str}"
    elif task_type == "subscription_change":
        header = f"*Lidmaatschap wijziging: {visitor_name}*"
        task_line = f"📋 Taak: Verwerk wijziging • {now_str}"
    else:
        header = f"*{visitor_name}*"
        task_line = f"📋 Taak: {action} • {now_str}" if action else f"📋 Taak • {now_str}"

    message = header
    if situation:
        message += f"\nSituatie: {situation}"
    message += f"\n{task_line}"
    message += f"\n⏰ Herinnering {reminder_nr}/2 • Originele taak: {original_time}"

    return message


def process_expired_tasks():
    now = datetime.now(AMSTERDAM_TZ)
    results = {"processed": 0, "reminded": 0, "skipped_max_reminders": 0, "errors": 0}

    log_json("QUERY_START", {"now": now.isoformat(), "excluded_task_types": EXCLUDED_TASK_TYPES})

    try:
        expired_tasks = (
            firestore_client.collection("slack_messages")
            .where("message_type", "==", "crm_task")
            .where("completed", "==", False)
            .where("expired", "==", False)
            .where("expires_at", "<", now)
            .stream()
        )
    except Exception as e:
        log_json("QUERY_ERROR", {"error": str(e)})
        raise

    tasks_by_tenant = {}
    for doc in expired_tasks:
        data = doc.to_dict()
        task_type = data.get("task_type", "")

        if task_type in EXCLUDED_TASK_TYPES:
            continue

        # Skip reminder documents themselves
        if data.get("is_reminder", False):
            continue

        tenant_id = data.get("tenant_id", "")
        if tenant_id not in tasks_by_tenant:
            tasks_by_tenant[tenant_id] = []
        tasks_by_tenant[tenant_id].append((doc.id, data))

    log_json("QUERY_RESULT", {
        "tenants_found": list(tasks_by_tenant.keys()),
        "total_tasks": sum(len(v) for v in tasks_by_tenant.values())
    })

    for tenant_id, tasks in tasks_by_tenant.items():
        client = get_slack_client(tenant_id)
        if not client:
            log_json("SLACK_CLIENT_ERROR", {"tenant_id": tenant_id})
            results["errors"] += len(tasks)
            continue

        for doc_id, task_data in tasks:
            results["processed"] += 1
            try:
                reminder_count = task_data.get("reminder_count", 0)

                if reminder_count >= 2:
                    log_json("TASK_MAX_REMINDERS_REACHED", {
                        "doc_id": doc_id,
                        "task_type": task_data.get("task_type"),
                        "visitor_name": task_data.get("visitor_name"),
                        "reminder_count": reminder_count
                    })
                    results["skipped_max_reminders"] += 1
                    continue

                # Post new reminder message to Slack
                reminder_message = build_reminder_message(task_data, reminder_count)

                try:
                    response = client.chat_postMessage(
                        channel=TAKEN_CHANNEL_ID,
                        text=reminder_message,
                        mrkdwn=True
                    )
                    reminder_ts = response["ts"]
                except SlackApiError as e:
                    if e.response["error"] == "not_in_channel":
                        client.conversations_join(channel=TAKEN_CHANNEL_ID)
                        response = client.chat_postMessage(
                            channel=TAKEN_CHANNEL_ID,
                            text=reminder_message,
                            mrkdwn=True
                        )
                        reminder_ts = response["ts"]
                    else:
                        raise

                # Store reminder as new Firestore document
                reminder_doc_id = f"reminder_{doc_id}_{reminder_count + 1}"
                reminder_expires_at = datetime.now(AMSTERDAM_TZ).replace(
                    hour=23, minute=59, second=59, microsecond=0
                )
                firestore_client.collection("slack_messages").document(reminder_doc_id).set({
                    "message_ts": reminder_ts,
                    "channel": TAKEN_CHANNEL_ID,
                    "tenant_id": tenant_id,
                    "task_type": task_data.get("task_type"),
                    "visitor_name": task_data.get("visitor_name"),
                    "parent_task_id": doc_id,
                    "reminder_number": reminder_count + 1,
                    "is_reminder": True,
                    "message_type": "crm_task",
                    "completed": False,
                    "expired": False,
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "expires_at": reminder_expires_at
                })

                # Update original document: mark as reminded + increment counter
                original_message_ts = task_data.get("message_ts")
                original_channel = task_data.get("channel", TAKEN_CHANNEL_ID)
                visitor_name = task_data.get("visitor_name", "Onbekend")
                reminded_time = format_datetime(now)

                if original_message_ts:
                    try:
                        client.chat_update(
                            channel=original_channel,
                            ts=original_message_ts,
                            text=f"⏰ ~{visitor_name}~ • Herinnerd • {reminded_time}",
                            blocks=[],
                            mrkdwn=True
                        )
                    except SlackApiError as e:
                        print(f"⚠️ Could not update original message {original_message_ts}: {e.response['error']}")

                firestore_client.collection("slack_messages").document(doc_id).update({
                    "reminder_count": reminder_count + 1,
                    "last_reminded_at": firestore.SERVER_TIMESTAMP
                })

                log_json("TASK_REMINDED", {
                    "doc_id": doc_id,
                    "reminder_doc_id": reminder_doc_id,
                    "task_type": task_data.get("task_type"),
                    "visitor_name": visitor_name,
                    "reminder_number": reminder_count + 1,
                    "reminder_ts": reminder_ts,
                    "tenant_id": tenant_id
                })
                results["reminded"] += 1

            except Exception as e:
                print(f"❌ Error processing task {doc_id}: {e}")
                results["errors"] += 1

    return results


@functions_framework.cloud_event
def task_reminder(cloud_event):
    """Gen 2 Pub/Sub function — sends reminders for expired, uncompleted CRM tasks"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data["message"]["data"]).decode("utf-8")
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        trigger_type = envelope.get("type", "")
        if trigger_type != "task_reminder":
            print(f"Skipping non-task_reminder schedule: {trigger_type}")
            return "OK"

        results = process_expired_tasks()

        log_json("OUTPUT", results)
        return "OK"

    except Exception as e:
        print(f"❌ task-reminder error: {e}")
        import traceback
        print(f"🐛 Traceback: {traceback.format_exc()}")

        try:
            from google.cloud import pubsub_v1
            publisher = pubsub_v1.PublisherClient()
            events_topic = publisher.topic_path(PROJECT_ID, "events")
            error_event = {
                "webhook_source": "task-reminder",
                "tenant_id": "system",
                "event_type": "service_error",
                "timestamp": datetime.now().isoformat(),
                "email": "dennis@habits.fit",
                "payload": {
                    "service": "task-reminder",
                    "error": f"**{str(e)}**"
                }
            }
            publisher.publish(events_topic, json.dumps(error_event).encode("utf-8")).result()
        except Exception as publish_err:
            print(f"❌ Failed to publish error event: {publish_err}")

        raise

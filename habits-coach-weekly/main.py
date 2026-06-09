import functions_framework
import base64
import json
import sys
from datetime import datetime, timezone
import uuid

from google.cloud import firestore, pubsub_v1

# Clients
fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()

PROJECT_ID = "solid-future-452906-a2"


def log(data: dict):
    sys.stdout.write(json.dumps(data) + "\n")
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


def get_active_tenants() -> list:
    tenants = []
    docs = fs_client.collection("tenants").where(
        filter=firestore.FieldFilter("active", "==", True)
    ).stream()
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        tenants.append(data)
    return tenants


def get_current_opportunity_nr(tenant_id: str, slack_channel: str) -> int:
    docs = fs_client.collection("coaching_sessions") \
        .where(filter=firestore.FieldFilter("tenant_id", "==", tenant_id)) \
        .where(filter=firestore.FieldFilter("slack_channel", "==", slack_channel)) \
        .order_by("created_at", direction=firestore.Query.DESCENDING) \
        .limit(1) \
        .stream()
    for doc in docs:
        return doc.to_dict().get("opportunity_nr", 0)
    return 0


def publish_start_trigger(tenant_id: str, slack_team_id: str, slack_channel: str, next_nr: int):
    """Publish a synthetic Slack message event to slack-translations topic."""
    topic_path = publisher.topic_path(PROJECT_ID, "slack-translations")

    event_message = {
        "event_type": "message",
        "tenant_id": tenant_id,
        "webhook_source": "slack",
        "payload": {
            "team_id": slack_team_id,
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "text": f"start:{next_nr}",
                "channel": slack_channel,
                "ts": str(datetime.now(timezone.utc).timestamp())
            }
        },
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "event_id": str(uuid.uuid4())
    }

    publisher.publish(
        topic_path,
        json.dumps(event_message).encode("utf-8"),
        event_type="message",
        webhook_source="slack",
        tenant_id=tenant_id
    )

    log({"TO_SLACK_TRANSLATIONS": {
        "tenant_id": tenant_id,
        "slack_team_id": slack_team_id,
        "channel": slack_channel,
        "message": f"start:{next_nr}"
    }})


@functions_framework.cloud_event
def habits_coach_weekly(cloud_event):
    """Pub/Sub triggered function. Publishes synthetic start trigger per active tenant."""

    log({"INPUT": {
        "trigger": "habits-coach-weekly",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }})

    try:
        tenants = get_active_tenants()
        log({"TENANTS": {"count": len(tenants)}})

        for tenant in tenants:
            tenant_id = tenant["id"]
            slack_team_id = tenant.get("slack_team_id")
            slack_channel = tenant.get("slack_coach_channel")

            if not slack_team_id or not slack_channel:
                error_msg = f"Tenant {tenant_id} missing slack_team_id or slack_coach_channel"
                log({"TENANT_CONFIG_MISSING": {"tenant_id": tenant_id}})
                publish_error_event("habits-coach-weekly", error_msg)
                continue

            try:
                current_nr = get_current_opportunity_nr(tenant_id, slack_channel)
                next_nr = current_nr + 1

                log({"TENANT_TRIGGER": {
                    "tenant_id": tenant_id,
                    "current_opportunity_nr": current_nr,
                    "next_opportunity_nr": next_nr
                }})

                publish_start_trigger(tenant_id, slack_team_id, slack_channel, next_nr)

            except Exception as e:
                error_msg = f"Error processing tenant {tenant_id}: {str(e)}"
                log({"TENANT_ERROR": {"tenant_id": tenant_id, "error": str(e)}})
                publish_error_event("habits-coach-weekly", error_msg)

    except Exception as e:
        error_msg = f"Fatal error in habits-coach-weekly: {str(e)}"
        log({"FATAL_ERROR": str(e)})
        publish_error_event("habits-coach-weekly", error_msg)
        raise

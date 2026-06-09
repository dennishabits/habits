import base64
import functions_framework
import json
import os
import sys
import requests
from datetime import datetime, timezone
from google.cloud import firestore, pubsub_v1

fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()

PROJECT_ID = "solid-future-452906-a2"
HABITS_COACH_REPLY_URL = os.environ.get("HABITS_COACH_REPLY_URL", "")


def log(data: dict):
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def publish_error_event(error_description: str):
    try:
        topic_path = publisher.topic_path(PROJECT_ID, "events")
        error_payload = {
            "envelope": {
                "webhook_source": "coaching-listener",
                "event_type": "service_error",
                "tenant_id": "system"
            },
            "payload": {
                "service": "coaching-listener",
                "error": f"**{error_description}**",
                "notification_email": "dennis@habits.fit",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        publisher.publish(topic_path, json.dumps(error_payload).encode("utf-8"))
    except Exception as e:
        log({"PUBLISH_ERROR_FAILED": str(e)})


def get_coaching_channel_for_team(slack_team_id: str) -> str | None:
    docs = fs_client.collection("tenants") \
        .where(filter=firestore.FieldFilter("slack_team_id", "==", slack_team_id)) \
        .limit(1) \
        .stream()
    for doc in docs:
        return doc.to_dict().get("slack_coach_channel")
    return None


@functions_framework.cloud_event
def coaching_listener(cloud_event):
    """Pub/Sub listener on slack-translations topic. Forwards coaching messages to habits-coach-reply."""
    try:
        message_data = cloud_event.data.get("message", {}).get("data", "")
        raw = base64.b64decode(message_data).decode("utf-8")
        envelope = json.loads(raw)

        log({"INPUT": {
            "event_type": envelope.get("event_type"),
            "webhook_source": envelope.get("webhook_source"),
            "tenant_id": envelope.get("tenant_id")
        }})

        if envelope.get("webhook_source") != "slack":
            log({"SKIPPED": {"reason": "not a slack event"}})
            return "OK"

        payload = envelope.get("payload", {})
        event = payload.get("event", {})

        if event.get("type") != "message":
            log({"SKIPPED": {"reason": "not a message event", "type": event.get("type")}})
            return "OK"

        # Filter message_changed and message_deleted — these are triggered by
        # the bot updating its own progress messages via chat.update
        if event.get("subtype") in ("message_deleted", "message_changed"):
            log({"SKIPPED": {"reason": "message_changed or deleted — bot update"}})
            return "OK"

        # Allow bot_message subtype only for weekly start trigger
        is_start_trigger = (
            event.get("subtype") == "bot_message" and
            event.get("text", "").startswith("start:")
        )

        if event.get("bot_id") and not is_start_trigger:
            log({"SKIPPED": {"reason": "bot message, not a start trigger"}})
            return "OK"

        if event.get("subtype") == "bot_message" and not is_start_trigger:
            log({"SKIPPED": {"reason": "bot_message subtype, not a start trigger"}})
            return "OK"

        slack_team_id = payload.get("team_id")
        if not slack_team_id:
            log({"SKIPPED": {"reason": "no team_id in payload"}})
            return "OK"

        coaching_channel = get_coaching_channel_for_team(slack_team_id)
        if not coaching_channel:
            log({"SKIPPED": {"reason": "no coaching channel configured", "team_id": slack_team_id}})
            return "OK"

        message_channel = event.get("channel")
        if message_channel != coaching_channel:
            log({"SKIPPED": {
                "reason": "not in coaching channel",
                "message_channel": message_channel,
                "coaching_channel": coaching_channel
            }})
            return "OK"

        log({"COACHING_MESSAGE_DETECTED": {
            "team_id": slack_team_id,
            "channel": message_channel,
            "is_start_trigger": is_start_trigger,
            "message_preview": event.get("text", "")[:100]
        }})

        if not HABITS_COACH_REPLY_URL:
            raise ValueError("HABITS_COACH_REPLY_URL environment variable not set")

        # Fire and forget — don't wait for reply to finish
        # Reply takes 20-30s; waiting causes Pub/Sub retries and duplicate processing
        try:
            requests.post(
                HABITS_COACH_REPLY_URL,
                json=payload,
                timeout=5
            )
            log({"TO_HABITS_COACH_REPLY": {
                "url": HABITS_COACH_REPLY_URL,
                "is_start_trigger": is_start_trigger,
                "status": "fired"
            }})
        except requests.exceptions.Timeout:
            log({"TO_HABITS_COACH_REPLY": {
                "url": HABITS_COACH_REPLY_URL,
                "is_start_trigger": is_start_trigger,
                "status": "timeout_expected"
            }})
        except Exception as e:
            log({"TO_HABITS_COACH_REPLY_ERROR": {"error": str(e)}})
            publish_error_event(f"Failed to call habits-coach-reply: {str(e)}")

        return "OK"

    except Exception as e:
        error_msg = f"Error in coaching-listener: {str(e)}"
        log({"ERROR": {"message": str(e)}})
        publish_error_event(error_msg)
        raise
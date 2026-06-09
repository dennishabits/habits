import base64
import json
import os
import functions_framework
from datetime import datetime
from google.cloud import pubsub_v1


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def convert_epoch_to_iso(timestamp):
    if not timestamp:
        return None
    try:
        if isinstance(timestamp, (int, float)):
            dt = datetime.utcfromtimestamp(timestamp)
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif isinstance(timestamp, str):
            return timestamp
        else:
            return None
    except Exception as e:
        print(f"❌ Error converting timestamp {timestamp}: {e}")
        return None


# Task types identified by email (no Sportivity customer id required)
EMAIL_BASED_TASK_TYPES = [
    'lead_call',            # legacy
    'order_processing',     # legacy
    'subscription_change',  # legacy
    'member_admin',         # new
    'prospect_call',        # new
]


class CrmTranslator:
    def __init__(self, project_id):
        self.project_id = project_id
        self.publisher = pubsub_v1.PublisherClient()
        self.events_topic = self.publisher.topic_path(project_id, "events")
        self.tasks_topic = self.publisher.topic_path(project_id, "tasks")

    def publish(self, topic, envelope):
        try:
            message_data = json.dumps(envelope).encode("utf-8")
            self.publisher.publish(topic, message_data).result()
        except Exception as e:
            print(f"❌ Error publishing to {topic}: {e}")

    def convert_payload_timestamps(self, payload):
        if isinstance(payload, dict):
            converted_payload = {}
            for key, value in payload.items():
                if key in ['created_at', 'updated_at', 'timestamp', 'receivedAt']:
                    converted_payload[key] = convert_epoch_to_iso(value) if value else value
                elif isinstance(value, dict):
                    converted_payload[key] = self.convert_payload_timestamps(value)
                elif isinstance(value, list):
                    converted_payload[key] = [self.convert_payload_timestamps(item) if isinstance(item, dict) else item for item in value]
                else:
                    converted_payload[key] = value
            return converted_payload
        else:
            return payload

    def translate_to_events(self, envelope):
        payload = envelope.get("payload", {})

        event_type = envelope.get("event_type")
        if event_type != "crm_task":
            return None

        task_type = payload.get('task_type', '')
        if not task_type:
            print("❌ No task_type found in payload")
            return None

        status = payload.get('status', 'active')

        # Resolve customer_id and email based on task_type
        if task_type in EMAIL_BASED_TASK_TYPES:
            customer_id = None
            email = payload.get('email')
            if not email:
                print(f"❌ No email found in {task_type} payload")
                return None
        else:
            # id-based task types (member_call, member_talk, fitcheck, reboot, etc.)
            sportivity_id = payload.get('id')
            if not sportivity_id:
                print(f"❌ No id found in {task_type} payload")
                return None
            customer_id = str(sportivity_id)
            email = None

        tenant_id = envelope.get("tenant_id", "unknown_tenant")
        received_at = envelope.get("receivedAt")

        if status == 'expired':
            # Expired tasks go directly to events — slack-listener handles expiry
            event_envelope = {
                "webhook_source": "customerio",
                "tenant_id": tenant_id,
                "event_type": "crm_task_expired",
                "received_at": received_at,
                "event_id": envelope.get("event_id"),
                "customer_id": customer_id,
                "email": email,
                "task_type": task_type,
                "payload": self.convert_payload_timestamps(payload)
            }
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            self.publish(self.events_topic, event_envelope)
            return event_envelope

        else:
            # Active tasks go to tasks topic → task-scheduler → events → slack-listener
            event_envelope = {
                "webhook_source": "customerio",
                "tenant_id": tenant_id,
                "event_type": "crm_task",
                "received_at": received_at,
                "event_id": envelope.get("event_id"),
                "customer_id": customer_id,
                "email": email,
                "task_type": task_type,
                "event_details": payload.get('context', ''),
                "event_secondary_details": payload.get('situation', ''),
                "event_action": payload.get('action', ''),
                "event_action_type": payload.get('action_type', ''),
                "task_valid_minutes": payload.get('valid_minutes', 0),
                "payload": self.convert_payload_timestamps(payload)
            }
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            self.publish(self.tasks_topic, event_envelope)
            return event_envelope


@functions_framework.cloud_event
def crm_translator(cloud_event):
    """Gen 2 Pub/Sub function with explicit CloudEvent decorator"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        webhook_source = envelope.get("webhook_source", "").lower()
        event_type = envelope.get("event_type", "").lower()

        if webhook_source != "customerio" or event_type != "crm_task":
            print(f"Skipping non-CRM event: {webhook_source}/{event_type}")
            return "OK"

        project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
        translator = CrmTranslator(project_id)

        translated_envelope = translator.translate_to_events(envelope)

        if translated_envelope:
            log_json("OUTPUT", translated_envelope)
        else:
            print("❌ No output generated")

        return "OK"

    except Exception as e:
        print(f"❌ Translation error: {e}")
        import traceback
        print(f"🐛 Traceback: {traceback.format_exc()}")

        try:
            project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
            publisher = pubsub_v1.PublisherClient()
            events_topic = publisher.topic_path(project_id, "events")

            error_event = {
                "webhook_source": "crm-translator",
                "tenant_id": "system",
                "event_type": "service_error",
                "timestamp": datetime.utcnow().isoformat(),
                "email": "dennis@habits.fit",
                "payload": {
                    "service": "crm-translator",
                    "error": f"**{str(e)}**"
                }
            }
            publisher.publish(events_topic, json.dumps(error_event).encode("utf-8")).result()

        except Exception as error_publish_exception:
            print(f"❌ Failed to publish error event: {error_publish_exception}")

        raise
import base64
import json
import os
import hashlib
from datetime import datetime
from google.cloud import firestore, pubsub_v1
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import functions_framework

# Configuration
PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
TAKEN_CHANNEL_ID = "C0ATAT7UTE0"

# Initialize services
firestore_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()
events_topic = publisher.topic_path(PROJECT_ID, "events")

slack_clients = {}


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def hash_email(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def get_slack_client(tenant_id):
    global slack_clients
    if tenant_id in slack_clients:
        return slack_clients[tenant_id]
    try:
        tenant_doc = firestore_client.collection("tenants").document(tenant_id).get()
        if not tenant_doc.exists:
            return None
        tenant_data = tenant_doc.to_dict()
        bot_token = tenant_data.get('slack_bot_token')
        if not bot_token or not bot_token.startswith('xoxb-'):
            return None
        client = WebClient(token=bot_token)
        slack_clients[tenant_id] = client
        return client
    except Exception as e:
        print(f"❌ Error creating Slack client: {e}")
        return None


def publish_event(event_type, tenant_id, email, outcome, user_id):
    """Publish lead call outcome event to events topic"""
    try:
        envelope = {
            "webhook_source": "slack",
            "tenant_id": tenant_id,
            "event_type": event_type,
            "email": email,
            "received_at": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            "payload": {
                "outcome": outcome,
                "email": email,
                "completed_by_slack_user": user_id,
                "completed_at": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            }
        }
        publisher.publish(events_topic, json.dumps(envelope).encode("utf-8")).result()
        log_json(f"TO_EVENTS", envelope)
        return True
    except Exception as e:
        print(f"❌ Error publishing event: {e}")
        return False


def format_datetime_now():
    """Format current Amsterdam time in Dutch format"""
    try:
        from zoneinfo import ZoneInfo
        amsterdam_tz = ZoneInfo("Europe/Amsterdam")
        dt = datetime.now(amsterdam_tz)
        dutch_months = {
            1: 'jan', 2: 'feb', 3: 'mrt', 4: 'apr', 5: 'mei', 6: 'jun',
            7: 'jul', 8: 'aug', 9: 'sep', 10: 'okt', 11: 'nov', 12: 'dec'
        }
        return f"{dt.day} {dutch_months[dt.month]}, {dt.strftime('%H:%M')}"
    except Exception:
        return datetime.now().strftime('%d %b, %H:%M')


@functions_framework.cloud_event
def slack_interactions(cloud_event):
    """
    Handles Slack button interactions for lead_call tasks.
    Triggered via Pub/Sub topic: slack-interactions
    """
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        tenant_id = envelope.get('tenant_id', '')
        if not tenant_id:
            print("❌ No tenant_id in envelope")
            return "OK"

        # The Slack interaction payload is in envelope.payload
        # webhook-dispatcher parsed the form-encoded 'payload' field into webhook_data
        slack_payload = envelope.get('payload', {})

        interaction_type = slack_payload.get('type')
        if interaction_type != 'block_actions':
            print(f"Skipping non-block_actions interaction: {interaction_type}")
            return "OK"

        actions = slack_payload.get('actions', [])
        if not actions:
            print("❌ No actions in payload")
            return "OK"

        action = actions[0]
        action_id = action.get('action_id')
        email = action.get('value', '')

        # Extract Slack context
        message = slack_payload.get('message', {})
        message_ts = message.get('ts')
        channel = slack_payload.get('channel', {}).get('id', TAKEN_CHANNEL_ID)
        user_id = slack_payload.get('user', {}).get('id', '')
        user_name = slack_payload.get('user', {}).get('name', '')

        if not email:
            print("❌ No email in action value")
            return "OK"

        if action_id not in ['lead_call_booked', 'lead_call_not_interested']:
            print(f"Skipping unknown action_id: {action_id}")
            return "OK"

        log_json("PROCESSING_INTERACTION", {
            "action_id": action_id,
            "email": email,
            "message_ts": message_ts,
            "user_id": user_id,
            "tenant_id": tenant_id
        })

        # Look up Firestore doc by email + date
        today_date = datetime.now().strftime('%Y-%m-%d')
        doc_id = f"{tenant_id}_{TAKEN_CHANNEL_ID}_lead_{hash_email(email)}_{today_date}"
        task_doc_ref = firestore_client.collection("slack_messages").document(doc_id)
        task_doc = task_doc_ref.get()

        if not task_doc.exists:
            print(f"❌ No task doc found: {doc_id}")
            return "OK"

        task_data = task_doc.to_dict()

        if task_data.get('completed', False):
            print(f"Task already completed: {doc_id}")
            return "OK"

        # Determine outcome
        if action_id == 'lead_call_booked':
            outcome = 'booked'
            event_type = 'lead_call_completed'
            completion_label = 'afspraak gemaakt'
            emoji = '✅'
        else:
            outcome = 'not_interested'
            event_type = 'lead_call_not_interested'
            completion_label = 'niet geïnteresseerd'
            emoji = '❌'

        completion_time = format_datetime_now()
        visitor_name = task_data.get('visitor_name', email)

        # Update Slack message — replace blocks with plain strikethrough text
        client = get_slack_client(tenant_id)
        if client and message_ts:
            try:
                completed_text = f"{emoji} ~{visitor_name} bellen~ • {completion_label} door <@{user_id}> • {completion_time}"
                client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=completed_text,
                    blocks=[],  # Remove action buttons
                    mrkdwn=True
                )
                log_json("SLACK_MESSAGE_UPDATED", {"message_ts": message_ts, "channel": channel, "completed_text": completed_text})
            except SlackApiError as e:
                print(f"❌ Slack update error: {e.response['error']}")

        # Update Firestore
        task_doc_ref.update({
            'completed': True,
            'completed_at': firestore.SERVER_TIMESTAMP,
            'outcome': outcome,
            'completed_by': user_id
        })
        log_json("FIRESTORE_UPDATED", {"doc_id": doc_id, "outcome": outcome, "completed_by": user_id})

        # Publish outcome event to events topic (customerio-listener will forward to CIO)
        publish_event(event_type, tenant_id, email, outcome, user_id)

        result = {
            "action": "lead_call_interaction_processed",
            "action_id": action_id,
            "outcome": outcome,
            "email": email,
            "message_ts": message_ts,
            "completed_by": user_id,
            "tenant_id": tenant_id
        }
        log_json("OUTPUT", result)
        return "OK"

    except Exception as e:
        print(f"❌ Error processing interaction: {e}")
        import traceback
        print(f"🐛 Full traceback: {traceback.format_exc()}")
        raise
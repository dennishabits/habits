import base64
import json
import os
import functions_framework
from google.cloud import firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

CIO_LOG_CHANNEL_ID = "C0B7T97GZ55"
SERVICE_NAME = "customerio-slack-logger"

firestore_client = firestore.Client()
slack_clients = {}

EVENT_EMOJI = {
    "email_sent": "✉️",
    "email_delivered": "✉️",
    "email_opened": "✉️",
    "email_clicked": "✉️",
    "email_bounced": "⚠️",
    "email_unsubscribed": "🚫",
    "email_complained": "🚫",
    "sms_sent": "💬",
    "sms_delivered": "💬",
    "sms_bounced": "⚠️",
    "push_sent": "🔔",
    "push_opened": "🔔",
    "push_bounced": "⚠️",
}


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
        print(f"Error creating Slack client: {e}")
        return None


def format_message(envelope: dict) -> str:
    event_type = envelope.get("event_type", "unknown")
    customer_id = envelope.get("customer_id", "")
    campaign_name = envelope.get("campaign_name", "")
    message_subject = envelope.get("message_subject", "")
    recipient = envelope.get("recipient", envelope.get("email", ""))
    failure_message = envelope.get("payload", {}).get("data", {}).get("failure_message", "")

    display_name = envelope.get("event_display_name", event_type.replace("_", " ").title())
    emoji = EVENT_EMOJI.get(event_type, "📨")

    campaign_part = f" • {campaign_name}" if campaign_name else ""
    line1 = f"{customer_id} - *{display_name}{campaign_part}*"

    if failure_message:
        detail = failure_message
    elif message_subject:
        detail = message_subject
    else:
        detail = recipient

    line2 = f"{emoji} {detail}"

    return f"{line1}\n{line2}"


def publish_error_event(error_description: str):
    try:
        from google.cloud import pubsub_v1
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path("solid-future-452906-a2", "events")
        error_envelope = {
            "event_type": "service_error",
            "service": SERVICE_NAME,
            "error": f"*{error_description}*",
            "email": "dennis@habits.fit"
        }
        publisher.publish(topic_path, json.dumps(error_envelope).encode("utf-8"))
    except Exception as e:
        print(f"Failed to publish error event: {e}")


@functions_framework.cloud_event
def customerio_slack_logger(cloud_event):
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data["message"]["data"]).decode("utf-8")
        envelope = json.loads(raw)

        print(json.dumps({"INPUT": {"envelope": envelope}}, default=str))

        if envelope.get("webhook_source") != "customerio":
            return

        tenant_id = envelope.get("tenant_id", "")
        if not tenant_id:
            return

        client = get_slack_client(tenant_id)
        if not client:
            print(json.dumps({"ERROR": {"reason": "no_slack_client", "tenant_id": tenant_id}}))
            return

        text = format_message(envelope)

        try:
            client.chat_postMessage(
                channel=CIO_LOG_CHANNEL_ID,
                text=text,
                mrkdwn=True,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            if e.response.get("error") == "not_in_channel":
                client.conversations_join(channel=CIO_LOG_CHANNEL_ID)
                client.chat_postMessage(
                    channel=CIO_LOG_CHANNEL_ID,
                    text=text,
                    mrkdwn=True,
                    unfurl_links=False,
                    unfurl_media=False
                )
            else:
                raise

        print(json.dumps({"OUTPUT": {"channel": CIO_LOG_CHANNEL_ID, "text": text}}))

    except Exception as e:
        error_msg = str(e)
        print(json.dumps({"ERROR": {"service": SERVICE_NAME, "error": error_msg}}))
        publish_error_event(error_msg)
        raise
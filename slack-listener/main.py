import base64
import json
import os
import hashlib
import re
from datetime import datetime, timedelta
from google.cloud import firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import functions_framework

# Configuration
PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
TAKEN_CHANNEL_ID = "C0ATAT7UTE0"
DENNIS_SLACK_ID = "D1KPR28A0"

firestore_client = firestore.Client()
slack_clients = {}


def log_json(label, envelope, payload=None):
    if payload is None:
        payload = envelope.get('payload', {})
    log_data = {"envelope": envelope, "payload": payload}
    print(f"{label}: {json.dumps(log_data, default=str)}")


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


def notify_dennis(client, service_name, error_description, context=None):
    try:
        context_text = f"\nContext: {json.dumps(context, default=str)}" if context else ""
        message = (
            f"⚠️ *{service_name}*\n"
            f"*<!here> Error:* *{error_description}*"
            f"{context_text}"
        )
        client.chat_postMessage(
            channel=DENNIS_SLACK_ID,
            text=message,
            mrkdwn=True,
            unfurl_links=False,
            unfurl_media=False
        )
        print(f"DM_DENNIS_SENT: {json.dumps({'service': service_name, 'error': error_description}, default=str)}")
    except Exception as e:
        print(f"❌ Failed to notify Dennis: {e}")


def try_join_and_retry(client, channel, text, service_name, context=None):
    try:
        client.conversations_join(channel=channel)
        print(f"AUTO_JOIN_SUCCESS: {json.dumps({'channel': channel, 'service': service_name}, default=str)}")
    except SlackApiError as join_err:
        error_msg = f"Auto-join failed for channel {channel}: {join_err.response['error']}"
        print(f"❌ {error_msg}")
        notify_dennis(client, service_name, error_msg, context)
        raise
    return client.chat_postMessage(
        channel=channel,
        text=text,
        mrkdwn=True,
        unfurl_links=False,
        unfurl_media=False
    )


def hash_email(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def calculate_age(birth_date_str):
    if not birth_date_str:
        return None
    try:
        if len(birth_date_str) == 10:
            parts = birth_date_str.split('-')
            if len(parts[0]) == 4:
                birth_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                birth_date = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
        else:
            return None
        today = datetime.now()
        age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
        return age
    except Exception as e:
        print(f"❌ Error calculating age from '{birth_date_str}': {e}")
        return None


def format_datetime(timestamp):
    if not timestamp:
        return "Onbekende tijd"
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        amsterdam_tz = ZoneInfo("Europe/Amsterdam")
        if isinstance(timestamp, str):
            if 'UTC' in timestamp:
                dt = datetime.fromisoformat(timestamp.replace(' UTC', '+00:00'))
            elif timestamp.endswith('Z'):
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            elif '+' in timestamp or '-' in timestamp[-6:]:
                dt = datetime.fromisoformat(timestamp)
            else:
                dt = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        elif isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            dt = timestamp
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        dt_amsterdam = dt.astimezone(amsterdam_tz)
        dutch_months = {
            1: 'jan', 2: 'feb', 3: 'mrt', 4: 'apr', 5: 'mei', 6: 'jun',
            7: 'jul', 8: 'aug', 9: 'sep', 10: 'okt', 11: 'nov', 12: 'dec'
        }
        month_name = dutch_months.get(dt_amsterdam.month, str(dt_amsterdam.month))
        return f"{dt_amsterdam.day} {month_name}, {dt_amsterdam.strftime('%H:%M')}"
    except Exception as e:
        print(f"❌ Error formatting datetime '{timestamp}': {e}")
        return "Onbekende tijd"


def get_date_from_timestamp(timestamp):
    if not timestamp:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).strftime('%Y-%m-%d')
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        amsterdam_tz = ZoneInfo("Europe/Amsterdam")
        if isinstance(timestamp, str):
            if 'UTC' in timestamp:
                dt = datetime.fromisoformat(timestamp.replace(' UTC', '+00:00'))
            elif timestamp.endswith('Z'):
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            elif '+' in timestamp or '-' in timestamp[-6:]:
                dt = datetime.fromisoformat(timestamp)
            else:
                dt = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        elif isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            dt = timestamp
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(amsterdam_tz).strftime('%Y-%m-%d')
    except Exception as e:
        print(f"❌ Error extracting date from timestamp '{timestamp}': {e}")
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).strftime('%Y-%m-%d')


def parse_activity_timestamp(timestamp_str):
    try:
        dutch_months_reverse = {
            'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12
        }
        parts = timestamp_str.split(', ')
        day_month = parts[0].split(' ')
        day = int(day_month[0])
        month = dutch_months_reverse.get(day_month[1], 1)
        time_parts = parts[1].split(':')
        return datetime(datetime.now().year, month, day, int(time_parts[0]), int(time_parts[1]))
    except:
        return None


def extract_activity_history(message_text):
    try:
        for pattern in [r"Recente acties:\n(.*?)$", r"Recent Activity:\n(.*?)$"]:
            match = re.search(pattern, message_text, re.DOTALL | re.MULTILINE)
            if match:
                activity_section = match.group(1).strip()
                if activity_section:
                    return [normalize_emoji_in_activity(a) for a in activity_section.split('\n') if a.strip()]
        return []
    except Exception as e:
        print(f"❌ Error extracting activity history: {e}")
        return []


def normalize_emoji_in_activity(activity_text):
    emoji_mappings = {
        ':arrows_counterclockwise:': '🔄', ':e-mail:': '📧', ':email:': '📧',
        ':calendar:': '📅', ':x:': '❌', ':running:': '🏃‍♀️', ':phone:': '📞',
        ':dart:': '🎯', ':sparkles:': '✨', ':no_entry_sign:': '🚫',
        ':outbox_tray:': '📤', ':clipboard:': '📋', ':credit_card:': '💳'
    }
    for slack_code, unicode_emoji in emoji_mappings.items():
        activity_text = activity_text.replace(slack_code, unicode_emoji)
    return activity_text


def sort_activities_by_timestamp(activities):
    activities_with_timestamps = []
    activities_without_timestamps = []
    fallback_time = datetime.now()
    for i, activity in enumerate(activities):
        timestamp_match = re.search(r'(\d{1,2} \w{3}, \d{2}:\d{2})', activity)
        if timestamp_match:
            parsed_dt = parse_activity_timestamp(timestamp_match.group(1))
            if parsed_dt:
                activities_with_timestamps.append((parsed_dt, activity))
            else:
                activities_with_timestamps.append((fallback_time - timedelta(seconds=len(activities) - i), activity))
        else:
            activities_without_timestamps.append(activity)
    activities_with_timestamps.sort(key=lambda x: x[0])
    return [a[1] for a in activities_with_timestamps] + activities_without_timestamps


def get_date_range_48h():
    """Return list of date strings covering the last 48 hours."""
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return [today.strftime('%Y-%m-%d'), yesterday.strftime('%Y-%m-%d')]


def render_crm_task_message(payload):
    """Generic renderer — no task_type logic, purely renders the schema."""
    subject = payload.get('subject', '')
    task_title = payload.get('task_title', '')
    details = payload.get('details', [])
    task_icon = payload.get('task_icon', '📋')
    task_label = payload.get('task_label', '')
    task_link = payload.get('task_link', '')
    note = payload.get('note', '')

    if subject and task_title:
        message_text = f"{subject} - *{task_title}*"
    elif subject:
        message_text = f"*{subject}*"
    else:
        message_text = f"*{task_title}*"

    if task_label:
        if task_link:
            message_text += f"\n{task_icon} Taak: <{task_link}|{task_label}>"
        else:
            message_text += f"\n{task_icon} Taak: {task_label}"

    if note and note.strip():
        message_text += f"\n_{note}_"

    visible_details = [d for d in details if d.get('value')]
    if visible_details:
        if len(visible_details) <= 2:
            parts = [f"{d['label']}: {d['value']}" for d in visible_details]
            message_text += '\n' + ' • '.join(parts)
        else:
            for d in visible_details:
                if d.get('bold'):
                    message_text += f"\n*{d['label']}: {d['value']}*"
                else:
                    message_text += f"\n{d['label']}: {d['value']}"

    return message_text


def build_crm_task_from_lead(envelope):
    payload = envelope.get('payload', {})
    firstname = payload.get('firstname', '')
    lastname = payload.get('lastname', '')
    full_name = f"{firstname} {lastname}".strip()
    phone_number = payload.get('phone_number', '')
    email = envelope.get('email', payload.get('email', ''))
    product_interest = envelope.get('product_interest', payload.get('product', ''))
    traffic_source = envelope.get('traffic_source', payload.get('source', ''))
    campaign_source = envelope.get('campaign_source', payload.get('sub_source', ''))
    note = payload.get('message', '')

    details = []
    if phone_number:
        details.append({"label": "Telefoon", "value": phone_number})
    if email:
        details.append({"label": "Email", "value": email})
    source_parts = list(filter(None, [traffic_source, campaign_source]))
    if product_interest:
        details.append({"label": "Product", "value": product_interest})
    if source_parts:
        details.append({"label": "Bron", "value": ', '.join(source_parts)})

    return {
        **envelope,
        'event_type': 'crm_task',
        'payload': {
            'task_type': 'prospect_call',
            'subject': full_name,
            'task_title': 'Lead',
            'details': details,
            'task_icon': '📞',
            'task_label': 'Bel binnen 24 uur',
            'task_link': '',
            'note': note,
            'visible': True
        }
    }


def send_crm_task_message(tenant_id, task_data):
    try:
        payload = task_data.get('payload', {})

        if 'payload' in payload:
            render_payload = payload.get('payload', {})
        else:
            render_payload = payload

        task_type = render_payload.get('task_type', payload.get('task_type', task_data.get('task_type', '')))
        subject = render_payload.get('subject', '')

        log_json("PROCESSING_CRM_TASK", task_data, payload)

        client = get_slack_client(tenant_id)
        if not client:
            return None

        action_type = task_data.get('event_action_type', payload.get('action_type', render_payload.get('action_type', '')))
        crm_task_id = task_data.get('crm_task_id')
        customer_id = task_data.get('customer_id')
        email = task_data.get('email', payload.get('email', render_payload.get('email', '')))
        email_lower = email.lower() if email else email
        availability_status = task_data.get('availability_status', 'pending')
        visible = render_payload.get('visible', payload.get('visible', True))

        if task_type in ['prospect_call', 'member_admin', 'lead_call', 'order_processing', 'subscription_change']:
            if not email:
                log_json("PROCESSING_CRM_TASK_NO_EMAIL", task_data, payload)
                return None
            task_date = get_date_from_timestamp(task_data.get('received_at'))
            doc_id = f"{tenant_id}_{TAKEN_CHANNEL_ID}_{task_type}_{hash_email(email)}_{task_date}"
        else:
            if not customer_id:
                log_json("PROCESSING_CRM_TASK_NO_CUSTOMER_ID", task_data, payload)
                return None
            task_date = get_date_from_timestamp(task_data.get('received_at'))
            doc_id = f"{tenant_id}_{TAKEN_CHANNEL_ID}_{customer_id}_{task_date}"

        existing_task_doc = firestore_client.collection("slack_messages").document(doc_id).get()

        if existing_task_doc.exists:
            existing_task = existing_task_doc.to_dict()
            if task_type in ['prospect_call', 'member_admin', 'lead_call', 'order_processing', 'subscription_change'] and not existing_task.get('completed', False):
                old_message_ts = existing_task.get('message_ts')
                old_channel = existing_task.get('channel')
                if old_message_ts and old_channel:
                    try:
                        client.chat_delete(channel=old_channel, ts=old_message_ts)
                        log_json("TASK_REPLACED", task_data, {"deleted_message_ts": old_message_ts, "task_type": task_type})
                    except Exception as e:
                        print(f"❌ Error deleting old task: {e}")
            elif existing_task.get('completed', False) or existing_task.get('expired', False):
                log_json("CRM_TASK_ALREADY_COMPLETED_OR_EXPIRED", task_data, {"doc_id": doc_id})
                return None
            else:
                log_json("CRM_TASK_ALREADY_EXISTS", task_data, {"doc_id": doc_id})
                return None

        message_text = render_crm_task_message(render_payload)

        now = datetime.now()
        expires_at = datetime.now() + timedelta(days=7) if task_type in ['member_admin', 'order_processing'] else datetime(now.year, now.month, now.day, 23, 59, 59)

        if not visible:
            firestore_client.collection("slack_messages").document(doc_id).set({
                'message_ts': None, 'channel': None,
                'crm_task_id': crm_task_id, 'customer_id': customer_id,
                'email': email_lower, 'task_type': task_type,
                'task_date': task_date, 'tenant_id': tenant_id,
                'action_type': action_type,
                'created_at': firestore.SERVER_TIMESTAMP,
                'expires_at': expires_at,
                'message_type': 'crm_task', 'completed': False, 'expired': False,
                'visitor_name': subject, 'task_action': render_payload.get('task_label', ''),
                'availability_status': availability_status,
                'visible': False
            })
            result = {
                "action": "store_invisible_task", "task_type": task_type,
                "crm_task_id": crm_task_id, "customer_id": customer_id,
                "email": email_lower, "task_date": task_date
            }
            log_json("CRM_TASK_STORED_INVISIBLE", task_data, result)
            return result

        log_json("SENDING_CRM_TASK_TO_SLACK", task_data, {
            "channel": TAKEN_CHANNEL_ID, "task_type": task_type,
            "email": email_lower, "availability_status": availability_status
        })

        try:
            response = client.chat_postMessage(
                channel=TAKEN_CHANNEL_ID,
                text=message_text,
                mrkdwn=True,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            if e.response['error'] == 'not_in_channel':
                response = try_join_and_retry(client, TAKEN_CHANNEL_ID, message_text, service_name="slack-listener / send_crm_task_message", context={"tenant_id": tenant_id})
            else:
                notify_dennis(client, "slack-listener / send_crm_task_message", f"SlackApiError: {e.response['error']}", context={"tenant_id": tenant_id})
                raise

        message_ts = response['ts']

        firestore_client.collection("slack_messages").document(doc_id).set({
            'message_ts': message_ts, 'channel': TAKEN_CHANNEL_ID,
            'crm_task_id': crm_task_id, 'customer_id': customer_id,
            'email': email_lower, 'task_type': task_type,
            'task_date': task_date, 'tenant_id': tenant_id,
            'action_type': action_type,
            'created_at': firestore.SERVER_TIMESTAMP,
            'expires_at': expires_at,
            'message_type': 'crm_task', 'completed': False, 'expired': False,
            'visitor_name': subject, 'task_action': render_payload.get('task_label', ''),
            'availability_status': availability_status,
            'visible': True
        })

        result = {
            "action": "send_crm_task", "message_ts": message_ts,
            "channel": TAKEN_CHANNEL_ID, "task_type": task_type,
            "crm_task_id": crm_task_id, "customer_id": customer_id,
            "email": email_lower, "task_date": task_date,
            "availability_status": availability_status
        }
        log_json("CRM_TASK_MESSAGE_SENT", task_data, result)
        return result

    except Exception as e:
        log_json("ERROR_SEND_CRM_TASK", task_data, {"error": str(e)})
        client = get_slack_client(tenant_id)
        if client:
            notify_dennis(client, "slack-listener / send_crm_task_message", str(e), context={"tenant_id": tenant_id})
        return None


def expire_crm_task(tenant_id, task_type, customer_id, email, envelope):
    try:
        client = get_slack_client(tenant_id)
        if not client:
            return None

        today_date = datetime.now().strftime('%Y-%m-%d')
        results = []

        if task_type in ['prospect_call', 'member_admin', 'lead_call', 'order_processing', 'subscription_change']:
            if not email:
                log_json("EXPIRE_CRM_TASK_NO_EMAIL", envelope, {"task_type": task_type})
                return None
            doc_ids = [(f"{tenant_id}_{TAKEN_CHANNEL_ID}_{task_type}_{hash_email(email)}_{today_date}", TAKEN_CHANNEL_ID)]
        else:
            doc_ids = [(f"{tenant_id}_{TAKEN_CHANNEL_ID}_{customer_id}_{today_date}", TAKEN_CHANNEL_ID)]

        for doc_id, channel_id in doc_ids:
            task_doc_ref = firestore_client.collection("slack_messages").document(doc_id)
            task_doc = task_doc_ref.get()

            if not task_doc.exists:
                log_json("EXPIRE_CRM_TASK_NOT_FOUND", envelope, {"doc_id": doc_id})
                continue

            task_data = task_doc.to_dict()

            if task_data.get('completed', False) or task_data.get('expired', False):
                log_json("EXPIRE_CRM_TASK_ALREADY_DONE", envelope, {"doc_id": doc_id})
                continue

            if not task_data.get('visible', True):
                task_doc_ref.update({'expired': True, 'expired_at': firestore.SERVER_TIMESTAMP})
                log_json("CRM_TASK_EXPIRED_INVISIBLE", envelope, {"doc_id": doc_id})
                continue

            message_ts = task_data.get('message_ts')
            channel = task_data.get('channel', channel_id)
            visitor_name = task_data.get('visitor_name', '')

            if not message_ts:
                continue

            task_action = task_data.get('task_action', '')
            if task_action:
                expired_text = f"⏰ ~{visitor_name}~ • Verlopen zonder actie • _{task_action}_"
            else:
                expired_text = f"⏰ ~{visitor_name}~ • Verlopen zonder actie"

            try:
                client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=expired_text,
                    blocks=[],
                    mrkdwn=True
                )
                task_doc_ref.update({'expired': True, 'expired_at': firestore.SERVER_TIMESTAMP})
                result = {
                    "action": "expire_crm_task", "doc_id": doc_id,
                    "message_ts": message_ts, "channel": channel,
                    "task_type": task_type, "expired_text": expired_text
                }
                log_json("CRM_TASK_EXPIRED", envelope, result)
                results.append(result)
            except SlackApiError as e:
                print(f"❌ Slack update error expiring task {doc_id}: {e.response['error']}")
            except Exception as e:
                print(f"❌ Error expiring task {doc_id}: {e}")

        return results[0] if results else None

    except Exception as e:
        log_json("ERROR_EXPIRE_CRM_TASK", envelope, {"error": str(e)})
        return None


def complete_crm_task_for_appointment(tenant_id, customer_id, appointment_data):
    """Complete a CRM task when an appointment is made.
    Searches within a 48-hour window and completes even if already expired.
    """
    try:
        if not customer_id:
            return None

        client = get_slack_client(tenant_id)
        if not client:
            return None

        payload = appointment_data.get('payload', {})
        appointment_type = payload.get('type', payload.get('appointment_type', ''))
        if appointment_type:
            completion_text_action = f"{appointment_type} ingepland"
        else:
            completion_text_action = "Afspraak ingepland"

        # Search within 48-hour window
        for date_str in get_date_range_48h():
            doc_id = f"{tenant_id}_{TAKEN_CHANNEL_ID}_{customer_id}_{date_str}"
            task_doc_ref = firestore_client.collection("slack_messages").document(doc_id)
            task_doc = task_doc_ref.get()

            if not task_doc.exists:
                log_json("NO_CRM_TASK_FOUND_FOR_APPOINTMENT", appointment_data, {"customer_id": customer_id, "doc_id": doc_id})
                continue

            task_data = task_doc.to_dict()

            # Skip if already completed
            if task_data.get('completed', False):
                continue

            if task_data.get('action_type') not in ('appointment', 'contact'):
                continue

            if not task_data.get('visible', True):
                task_doc_ref.update({'completed': True, 'expired': False, 'completed_at': firestore.SERVER_TIMESTAMP})
                result = {"action": "complete_crm_task_invisible", "customer_id": customer_id, "doc_id": doc_id}
                log_json("CRM_TASK_COMPLETED_INVISIBLE", appointment_data, result)
                return result

            message_ts = task_data.get('message_ts')
            channel = task_data.get('channel')
            visitor_name = task_data.get('visitor_name', 'Bezoeker')

            if not message_ts or not channel:
                continue

            was_expired = task_data.get('expired', False)

            try:
                client.chat_update(
                    channel=channel, ts=message_ts,
                    text=f"✅ ~{visitor_name}~ • {completion_text_action}",
                    mrkdwn=True
                )
                task_doc_ref.update({
                    'completed': True,
                    'expired': False,
                    'completed_at': firestore.SERVER_TIMESTAMP
                })
                result = {
                    "action": "complete_crm_task",
                    "message_ts": message_ts,
                    "channel": channel,
                    "customer_id": customer_id,
                    "visitor_name": visitor_name,
                    "completion_text": completion_text_action,
                    "was_expired": was_expired
                }
                log_json("CRM_TASK_COMPLETED", appointment_data, result)
                return result
            except SlackApiError as e:
                print(f"❌ Slack update error: {e.response['error']}")
            except Exception as e:
                print(f"❌ Error completing CRM task: {e}")

        return None

    except Exception as e:
        log_json("ERROR_COMPLETE_CRM_TASK", appointment_data, {"error": str(e)})
        return None


def complete_lead_task_for_appointment(tenant_id, email, appointment_data):
    """Complete a lead task when an appointment is made.
    Searches within a 48-hour window and completes even if already expired.
    """
    try:
        if not email:
            return None

        client = get_slack_client(tenant_id)
        if not client:
            return None

        payload = appointment_data.get('payload', {})
        appointment_type = payload.get('type', payload.get('appointment_type', ''))
        if appointment_type:
            completion_text_action = f"{appointment_type} ingepland"
        else:
            completion_text_action = "Afspraak ingepland"

        # Search within 48-hour window
        for date_str in get_date_range_48h():
            doc_id_taken = f"{tenant_id}_{TAKEN_CHANNEL_ID}_prospect_call_{hash_email(email)}_{date_str}"
            taken_doc = firestore_client.collection("slack_messages").document(doc_id_taken).get()

            if not taken_doc.exists:
                continue

            taken_data = taken_doc.to_dict()

            # Skip if already completed
            if taken_data.get('completed', False):
                continue

            message_ts = taken_data.get('message_ts')
            channel = taken_data.get('channel')
            visitor_name = taken_data.get('visitor_name', 'Lead')

            if not message_ts or not channel:
                continue

            was_expired = taken_data.get('expired', False)

            try:
                client.chat_update(
                    channel=channel, ts=message_ts,
                    text=f"✅ ~{visitor_name}~ • {completion_text_action}",
                    blocks=[], mrkdwn=True
                )
                firestore_client.collection("slack_messages").document(doc_id_taken).update({
                    'completed': True,
                    'expired': False,
                    'completed_at': firestore.SERVER_TIMESTAMP
                })
                result = {
                    "action": "complete_lead_task_taken",
                    "channel": channel,
                    "email": email,
                    "was_expired": was_expired
                }
                log_json("LEAD_TASK_COMPLETED_TAKEN", appointment_data, result)
                return result
            except Exception as e:
                print(f"❌ Error completing lead task in taken: {e}")

        return None

    except Exception as e:
        print(f"❌ Error completing lead task: {e}")
        return None


def is_new_membership(event_type, payload):
    if event_type in ['membership_new', 'subscription_new']:
        return True
    return event_type == 'subscription_update' and payload.get('status', '').lower() == 'new'


def complete_order_task_for_membership(tenant_id, customer_id, membership_data):
    try:
        email = membership_data.get('email') or membership_data.get('payload', {}).get('email')
        if not email:
            return None

        email = email.lower()
        client = get_slack_client(tenant_id)
        if not client:
            return None

        payload = membership_data.get('payload', {})
        subscription_name = payload.get('subscription_name', '')
        if subscription_name:
            completion_text_action = f"Lidmaatschap verwerkt • {subscription_name}"
        else:
            completion_text_action = "Lidmaatschap verwerkt"

        results = []
        cutoff = datetime.now() - timedelta(days=7)
        taken_docs = (
            firestore_client.collection("slack_messages")
            .where("tenant_id", "==", tenant_id)
            .where("task_type", "==", "member_admin")
            .where("email", "==", email)
            .where("completed", "==", False)
            .where("expired", "==", False)
            .where("created_at", ">=", cutoff)
            .stream()
        )

        for taken_doc in taken_docs:
            taken_data = taken_doc.to_dict()
            message_ts = taken_data.get('message_ts')
            channel = taken_data.get('channel')
            visitor_name = taken_data.get('visitor_name', 'Order')

            if not taken_data.get('visible', True):
                firestore_client.collection("slack_messages").document(taken_doc.id).update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                results.append({"action": "complete_invisible_order_task", "email": email, "doc_id": taken_doc.id})
                log_json("ORDER_TASK_COMPLETED_INVISIBLE", membership_data, results[-1])
                continue

            if not message_ts or not channel:
                continue

            try:
                client.chat_update(
                    channel=channel, ts=message_ts,
                    text=f"✅ ~{visitor_name}~ • {completion_text_action}",
                    mrkdwn=True
                )
                firestore_client.collection("slack_messages").document(taken_doc.id).update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                results.append({"action": "complete_order_task_taken", "channel": channel, "customer_id": customer_id, "doc_id": taken_doc.id})
                log_json("ORDER_TASK_COMPLETED_TAKEN", membership_data, results[-1])
            except Exception as e:
                print(f"❌ Error completing order in taken: {e}")

        return results[0] if results else None

    except Exception as e:
        log_json("ERROR_COMPLETE_ORDER_TASK", membership_data, {"error": str(e)})
        return None


def complete_lead_task_for_membership(tenant_id, email, membership_data):
    try:
        if not email:
            return None

        client = get_slack_client(tenant_id)
        if not client:
            return None

        payload = membership_data.get('payload', {})
        subscription_name = payload.get('subscription_name', '')
        if subscription_name:
            completion_text_action = f"Lidmaatschap gestart • {subscription_name}"
        else:
            completion_text_action = "Lidmaatschap gestart"

        today_date = datetime.now().strftime('%Y-%m-%d')

        doc_id_taken = f"{tenant_id}_{TAKEN_CHANNEL_ID}_prospect_call_{hash_email(email)}_{today_date}"
        taken_doc = firestore_client.collection("slack_messages").document(doc_id_taken).get()
        if taken_doc.exists:
            taken_data = taken_doc.to_dict()
            if not taken_data.get('completed', False) and not taken_data.get('expired', False):
                message_ts = taken_data['message_ts']
                channel = taken_data['channel']
                visitor_name = taken_data.get('visitor_name', 'Lead')
                try:
                    client.chat_update(
                        channel=channel, ts=message_ts,
                        text=f"✅ ~{visitor_name}~ • {completion_text_action}",
                        blocks=[], mrkdwn=True
                    )
                    firestore_client.collection("slack_messages").document(doc_id_taken).update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                    result = {"action": "complete_lead_task_taken", "channel": channel, "email": email}
                    log_json("LEAD_TASK_COMPLETED_FOR_MEMBERSHIP_TAKEN", membership_data, result)
                    return result
                except Exception as e:
                    print(f"❌ Error completing lead task in taken: {e}")

        return None

    except Exception as e:
        print(f"❌ Error completing lead task for membership: {e}")
        return None


def complete_member_admin_task(tenant_id, email, membership_data):
    try:
        if not email:
            return None

        today_date = datetime.now().strftime('%Y-%m-%d')
        client = get_slack_client(tenant_id)
        if not client:
            return None

        payload = membership_data.get('payload', {})
        event_type = membership_data.get('event_type', '')
        subscription_name = payload.get('subscription_name', '')
        status = payload.get('status', '').lower()

        if status == 'cancel' or 'cancel' in event_type.lower():
            completion_text_action = "Opzegging verwerkt"
        elif subscription_name:
            completion_text_action = f"Verwerkt • {subscription_name}"
        else:
            completion_text_action = "Verwerkt"

        results = []

        for task_type in ['member_admin', 'subscription_change']:
            doc_id = f"{tenant_id}_{TAKEN_CHANNEL_ID}_{task_type}_{hash_email(email)}_{today_date}"
            task_doc_ref = firestore_client.collection("slack_messages").document(doc_id)
            task_doc = task_doc_ref.get()

            if not task_doc.exists:
                continue

            task_data = task_doc.to_dict()

            if task_data.get('completed', False) or task_data.get('expired', False):
                continue

            if not task_data.get('visible', True):
                task_doc_ref.update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                result = {"action": "complete_member_admin_task_invisible", "email": email, "task_type": task_type}
                log_json("MEMBER_ADMIN_TASK_COMPLETED_INVISIBLE", membership_data, result)
                results.append(result)
                continue

            message_ts = task_data.get('message_ts')
            channel = task_data.get('channel')
            visitor_name = task_data.get('visitor_name', '')

            if not message_ts or not channel:
                continue

            client.chat_update(
                channel=channel,
                ts=message_ts,
                text=f"✅ ~{visitor_name}~ • {completion_text_action}",
                mrkdwn=True
            )
            task_doc_ref.update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})

            result = {
                "action": "complete_member_admin_task",
                "message_ts": message_ts,
                "channel": channel,
                "email": email,
                "task_type": task_type
            }
            log_json("MEMBER_ADMIN_TASK_COMPLETED", membership_data, result)
            results.append(result)

        return results[0] if results else None

    except Exception as e:
        print(f"❌ Error completing member admin task: {e}")
        return None


def should_process_event_for_update(event_type):
    filtered_events = ['customer_updated', 'customerupdate', 'membership_updated', 'membershipupdate', 'visit']
    return event_type.lower() not in [e.lower() for e in filtered_events]


@functions_framework.cloud_event
def slack_crm_pipeline(cloud_event):
    """Main Cloud Function entry point - Gen 2 CloudEvent format"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        event_type = envelope.get('event_type', '')
        webhook_source = envelope.get('webhook_source', '')
        tenant_id = envelope.get('tenant_id', '')

        if not tenant_id:
            return "OK"

        if event_type == 'lead_submitted':
            log_json("INPUT", envelope)
            crm_task_envelope = build_crm_task_from_lead(envelope)
            task_result = send_crm_task_message(tenant_id, crm_task_envelope)
            if task_result:
                log_json("OUTPUT", envelope, task_result)

        elif event_type == 'crm_task':
            log_json("INPUT", envelope)
            result = send_crm_task_message(tenant_id, envelope)
            if result:
                log_json("OUTPUT", envelope, result)

        elif event_type == 'crm_task_expired':
            log_json("INPUT", envelope)
            task_type = envelope.get('task_type', '')
            customer_id = envelope.get('customer_id')
            email = envelope.get('email')
            result = expire_crm_task(tenant_id, task_type, customer_id, email, envelope)
            if result:
                log_json("OUTPUT", envelope, result)

        elif webhook_source in ['acuity', 'customerio', 'sportivity']:
            if not should_process_event_for_update(event_type):
                return "OK"

            email = envelope.get('email') or envelope.get('payload', {}).get('email')
            if not email:
                return "OK"

            log_json("INPUT", envelope)

            if webhook_source == 'acuity' and event_type in ['booking', 'appointment']:
                payload = envelope.get('payload', {})
                appointment_type = payload.get('type', payload.get('appointment_type', ''))
                customer_id = envelope.get('customer_id')
                if 'fitcheck' in appointment_type.lower() or 'circuit' in appointment_type.lower():
                    if customer_id:
                        completion_result = complete_crm_task_for_appointment(tenant_id, customer_id, envelope)
                        if completion_result:
                            log_json("OUTPUT_CRM_TASK_COMPLETION", envelope, completion_result)
                lead_completion_result = complete_lead_task_for_appointment(tenant_id, email, envelope)
                if lead_completion_result:
                    log_json("OUTPUT_LEAD_TASK_COMPLETION", envelope, lead_completion_result)

            if webhook_source == 'sportivity':
                payload = envelope.get('payload', {})
                if is_new_membership(event_type, payload):
                    customer_id = envelope.get('customer_id')
                    completion_result = complete_order_task_for_membership(tenant_id, customer_id, envelope)
                    if completion_result:
                        log_json("OUTPUT_ORDER_TASK_COMPLETION", envelope, completion_result)
                    if email:
                        lead_completion_result = complete_lead_task_for_membership(tenant_id, email, envelope)
                        if lead_completion_result:
                            log_json("OUTPUT_LEAD_TASK_COMPLETION_MEMBERSHIP", envelope, lead_completion_result)

                if event_type == 'subscription_update':
                    if email:
                        admin_result = complete_member_admin_task(tenant_id, email, envelope)
                        if admin_result:
                            log_json("OUTPUT_MEMBER_ADMIN_COMPLETION", envelope, admin_result)

        return "OK"

    except Exception as e:
        print(f"❌ Error processing event: {e}")
        import traceback
        print(f"🐛 Full traceback: {traceback.format_exc()}")
        raise
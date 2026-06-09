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
SLACK_CHANNEL_ID = "C654VMGG7"
LEDENADMINISTRATIE_CHANNEL_ID = "C010PNUAZP1"
CRM_TASKS_CHANNEL_ID = "C09CGLHBG6N"
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
        return datetime.now().strftime('%Y-%m-%d')
    try:
        from datetime import timezone
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
        return dt.strftime('%Y-%m-%d')
    except Exception as e:
        print(f"❌ Error extracting date from timestamp '{timestamp}': {e}")
        return datetime.now().strftime('%Y-%m-%d')


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


def check_existing_lead(tenant_id, email):
    try:
        doc_id = f"{tenant_id}_{SLACK_CHANNEL_ID}_{hash_email(email)}"
        message_doc = firestore_client.collection("slack_messages").document(doc_id).get()
        if not message_doc.exists:
            return None
        message_data = message_doc.to_dict()
        created_at = message_data.get('created_at')
        if not created_at:
            return None
        if created_at.replace(tzinfo=None) > datetime.now() - timedelta(hours=48):
            return message_data
        return None
    except Exception as e:
        print(f"❌ Error checking existing lead: {e}")
        return None


def check_existing_order(tenant_id, email):
    try:
        doc_id = f"{tenant_id}_{LEDENADMINISTRATIE_CHANNEL_ID}_{hash_email(email)}"
        message_doc = firestore_client.collection("slack_messages").document(doc_id).get()
        if not message_doc.exists:
            return None
        message_data = message_doc.to_dict()
        created_at = message_data.get('created_at')
        if not created_at:
            return None
        if created_at.replace(tzinfo=None) > datetime.now() - timedelta(days=7):
            return message_data
        return None
    except Exception as e:
        print(f"❌ Error checking existing order: {e}")
        return None


def compare_order_fields(old_payload, new_payload):
    comparison_fields = [
        'firstname', 'lastname', 'birth_date', 'dob', 'gender',
        'street', 'house_number', 'zip', 'zipcode', 'postal_code', 'city',
        'phone_number', 'phone', 'email', 'brand', 'iban', 'ccname',
        'account_holder_name', 'subscription_duration', 'subscription_price',
        'contract', 'promotion', 'message'
    ]
    changes_detected = False
    field_changes = []
    for field in comparison_fields:
        old_val = str(old_payload.get(field, '') or '').strip()
        new_val = str(new_payload.get(field, '') or '').strip()
        if old_val != new_val:
            changes_detected = True
            field_changes.append({'field': field, 'old_value': old_val, 'new_value': new_val})
    return changes_detected, field_changes


def render_crm_task_message(payload):
    """Generic renderer — no task_type logic, purely renders the schema.
    Format: subject - task_title / task line / note / details
    """
    subject = payload.get('subject', '')
    task_title = payload.get('task_title', '')
    details = payload.get('details', [])
    task_icon = payload.get('task_icon', '📋')
    task_label = payload.get('task_label', '')
    task_link = payload.get('task_link', '')
    note = payload.get('note', '')

    # Title line
    if subject and task_title:
        message_text = f"{subject} - *{task_title}*"
    elif subject:
        message_text = f"*{subject}*"
    else:
        message_text = f"*{task_title}*"

    # Task line — with or without link
    if task_label:
        if task_link:
            message_text += f"\n{task_icon} Taak: <{task_link}|{task_label}>"
        else:
            message_text += f"\n{task_icon} Taak: {task_label}"

    # Note — before details
    if note and note.strip():
        message_text += f"\n_{note}_"

    # Details — inline (•) if ≤2, block if >2
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

        # Support new schema: render fields may be nested one level deeper
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

        action_type = task_data.get('event_action_type', payload.get('action_type', ''))
        crm_task_id = task_data.get('crm_task_id')
        customer_id = task_data.get('customer_id')
        email = task_data.get('email', payload.get('email', render_payload.get('email', '')))
        email_lower = email.lower() if email else email
        availability_status = task_data.get('availability_status', 'pending')
        visible = render_payload.get('visible', payload.get('visible', True))

        # Determine deduplication key
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
        expiry_time = format_datetime(envelope.get('received_at', datetime.now()))
        results = []

        if task_type in ['prospect_call', 'member_admin', 'lead_call', 'order_processing', 'subscription_change']:
            if not email:
                log_json("EXPIRE_CRM_TASK_NO_EMAIL", envelope, {"task_type": task_type})
                return None
            doc_ids = [(f"{tenant_id}_{TAKEN_CHANNEL_ID}_{task_type}_{hash_email(email)}_{today_date}", TAKEN_CHANNEL_ID)]
        else:
            doc_ids = [
                (f"{tenant_id}_{TAKEN_CHANNEL_ID}_{customer_id}_{today_date}", TAKEN_CHANNEL_ID),
                (f"{tenant_id}_{CRM_TASKS_CHANNEL_ID}_{customer_id}_{today_date}", CRM_TASKS_CHANNEL_ID)
            ]

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

            expired_text = f"⏰ ~{visitor_name}~ • Verlopen • {expiry_time}"

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


def send_order_message(tenant_id, order_data):
    try:
        payload = order_data.get('payload', {})
        email = order_data.get('email') or payload.get('email', '')
        if not email:
            log_json("PROCESSING_ORDER_MESSAGE_NO_EMAIL", order_data, payload)
            return None

        existing_order = check_existing_order(tenant_id, email)

        if existing_order:
            old_payload = existing_order.get('last_payload', {})
            changes_detected, field_changes = compare_order_fields(old_payload, payload)
            if not changes_detected:
                log_json("ORDER_NO_CHANGES_DETECTED", order_data, {"existing_order_ts": existing_order.get('message_ts'), "email": email})
                return None
            log_json("ORDER_CHANGES_DETECTED", order_data, {"field_changes": field_changes})
            client = get_slack_client(tenant_id)
            if not client:
                return None
            existing_activities = []
            try:
                old_message_ts = existing_order['message_ts']
                old_channel = existing_order['channel']
                response = client.conversations_history(channel=old_channel, latest=old_message_ts, limit=1, inclusive=True)
                if response['messages']:
                    existing_activities = extract_activity_history(response['messages'][0]['text'])
                client.chat_delete(channel=old_channel, ts=old_message_ts)
                log_json("OLD_ORDER_MESSAGE_DELETED", order_data, {"deleted_message_ts": old_message_ts})
            except Exception as e:
                print(f"❌ Error deleting old order message: {e}")
        else:
            existing_activities = []

        log_json("PROCESSING_ORDER_MESSAGE", order_data, payload)
        client = get_slack_client(tenant_id)
        if not client:
            return None

        firstname = payload.get('firstname', '')
        lastname = payload.get('lastname', '')
        birth_date = payload.get('birth_date', payload.get('dob', ''))
        gender = payload.get('gender', '')
        street = payload.get('street', '')
        house_number = payload.get('house_number', '')
        zip_code = payload.get('zip', payload.get('zipcode', payload.get('postal_code', '')))
        city = payload.get('city', '')
        phone_number = payload.get('phone_number', payload.get('phone', ''))
        brand = payload.get('brand', '')
        iban = payload.get('iban', '')
        ccname = payload.get('ccname', payload.get('account_holder_name', ''))
        customer_id = order_data.get('customer_id', payload.get('customer_id', ''))
        subscription_duration = payload.get('subscription_duration', '')
        subscription_price = payload.get('subscription_price', '')
        contract = payload.get('contract', '')
        traffic_source = order_data.get('traffic_source', '')
        pagename = order_data.get('pagename', '')
        promotion = order_data.get('promotion', payload.get('promotion', ''))
        message = payload.get('message', '')

        subscription_type = {'young': 'Young', 'regular': 'Regular', 'smart': 'Smart (daluren)'}.get(subscription_duration, subscription_duration)
        contract_text = {'flex': 'flex', '1y': '1 jaar'}.get(contract, contract)
        gender_text = {'m': 'Man', 'v': 'Vrouw', 'f': 'Vrouw'}.get(gender, gender)

        birth_date_formatted = birth_date
        if birth_date and len(birth_date) == 10:
            try:
                parts = birth_date.split('-')
                birth_date_formatted = f"{parts[2]}-{parts[1]}-{parts[0]}"
            except:
                pass

        full_name = f"{firstname} {lastname}".strip()
        message_text = f"*Order: {full_name}*"

        personal_info = list(filter(None, [
            f"Geboortedatum: {birth_date_formatted}" if birth_date_formatted else None,
            f"Geslacht: {gender_text}" if gender_text else None
        ]))
        if personal_info:
            message_text += f"\n{' • '.join(personal_info)}"

        address_line = ' '.join(filter(None, [street, house_number]))
        location_line = ' '.join(filter(None, [zip_code, city]))
        if address_line and location_line:
            message_text += f"\nAdres: {address_line}, {location_line}"
        elif address_line or location_line:
            message_text += f"\nAdres: {address_line or location_line}"

        if phone_number:
            message_text += f"\nTelefoon: {phone_number}"
        if email:
            message_text += f"\nEmail: {email}"
        if brand:
            message_text += f"\nSportschool: {brand}"

        if iban or ccname:
            message_text += f"\nRekeningnummer: {' '.join(filter(None, [iban, f'tnv {ccname}' if ccname else None]))}"

        if subscription_type or subscription_price or contract_text:
            sub_parts = list(filter(None, [
                subscription_type,
                f"€{subscription_price} per 4 weken" if subscription_price else None,
                f"({contract_text})" if contract_text else None
            ]))
            message_text += f"\n*Lidmaatschap: {' - '.join(sub_parts)}*"

        context_parts = list(filter(None, [
            f"Actie: {promotion}" if promotion else None,
            f"Bron: {', '.join(filter(None, [traffic_source, pagename]))}" if (traffic_source or pagename) else None
        ]))
        if context_parts:
            message_text += f"\n{' • '.join(context_parts)}"

        if message:
            message_text += f"\nBericht: {message}"

        task_time = format_datetime(order_data.get('received_at', datetime.now()))
        message_text += f"\n📋 Taak: Verwerk in ledenadministratie • {task_time}"

        activity_lines = existing_activities.copy()
        current_time = format_datetime(order_data.get('received_at', datetime.now()))
        activity_lines.append(f"🔄 Order bijgewerkt • {current_time}" if existing_order else f"💳 Order ontvangen • {current_time}")

        if activity_lines:
            message_text += f"\n\nRecente acties:\n" + "\n".join(sort_activities_by_timestamp(activity_lines))

        log_json("SENDING_ORDER_TO_SLACK", order_data, {"channel": LEDENADMINISTRATIE_CHANNEL_ID, "email": email})

        try:
            response = client.chat_postMessage(
                channel=LEDENADMINISTRATIE_CHANNEL_ID,
                text=message_text,
                mrkdwn=True,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            if e.response['error'] == 'not_in_channel':
                response = try_join_and_retry(client, LEDENADMINISTRATIE_CHANNEL_ID, message_text, service_name="slack-listener / send_order_message", context={"tenant_id": tenant_id, "email": email})
            else:
                notify_dennis(client, "slack-listener / send_order_message", f"SlackApiError: {e.response['error']}", context={"tenant_id": tenant_id, "email": email})
                raise

        message_ts = response['ts']
        doc_id = f"{tenant_id}_{LEDENADMINISTRATIE_CHANNEL_ID}_{hash_email(email)}"
        firestore_client.collection("slack_messages").document(doc_id).set({
            'message_ts': message_ts, 'channel': LEDENADMINISTRATIE_CHANNEL_ID,
            'email': email.lower(), 'customer_id': customer_id, 'tenant_id': tenant_id,
            'created_at': firestore.SERVER_TIMESTAMP,
            'expires_at': datetime.now() + timedelta(days=7),
            'message_type': 'order', 'last_payload': payload,
            'task_completed': False, 'lead_name': full_name
        })

        result = {"action": "send_updated_order" if existing_order else "send_order_message", "message_ts": message_ts, "channel": LEDENADMINISTRATIE_CHANNEL_ID, "email": email, "customer_id": customer_id}
        log_json("ORDER_MESSAGE_SENT", order_data, result)
        return result

    except Exception as e:
        log_json("ERROR_SEND_ORDER", order_data, {"error": str(e)})
        client = get_slack_client(tenant_id)
        if client:
            notify_dennis(client, "slack-listener / send_order_message", str(e), context={"tenant_id": tenant_id})
        return None


def complete_lead_task_for_appointment(tenant_id, email, appointment_data):
    try:
        if not email:
            return None

        client = get_slack_client(tenant_id)
        if not client:
            return None

        completion_time = format_datetime(appointment_data.get('received_at', datetime.now()))
        today_date = datetime.now().strftime('%Y-%m-%d')
        results = []

        doc_id_leads = f"{tenant_id}_{SLACK_CHANNEL_ID}_{hash_email(email)}"
        lead_doc = firestore_client.collection("slack_messages").document(doc_id_leads).get()
        if lead_doc.exists:
            lead_data = lead_doc.to_dict()
            if not lead_data.get('task_completed', False):
                message_ts = lead_data['message_ts']
                channel = lead_data['channel']
                try:
                    response = client.conversations_history(channel=channel, latest=message_ts, limit=1, inclusive=True)
                    if response['messages']:
                        name_match = re.search(r'\*(?:Lead|Bezoeker): (.+?)\*', response['messages'][0]['text'])
                        name = name_match.group(1) if name_match else lead_data.get('lead_name', 'Lead')
                        client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{name} bellen~ • {completion_time}", mrkdwn=True)
                        firestore_client.collection("slack_messages").document(doc_id_leads).update({'task_completed': True, 'task_completed_at': firestore.SERVER_TIMESTAMP})
                        results.append({"action": "complete_lead_task_leads", "channel": channel, "email": email, "name": name})
                        log_json("LEAD_TASK_COMPLETED_LEADS", appointment_data, results[-1])
                except Exception as e:
                    print(f"❌ Error completing lead task in leads: {e}")

        doc_id_taken = f"{tenant_id}_{TAKEN_CHANNEL_ID}_prospect_call_{hash_email(email)}_{today_date}"
        taken_doc = firestore_client.collection("slack_messages").document(doc_id_taken).get()
        if taken_doc.exists:
            taken_data = taken_doc.to_dict()
            if not taken_data.get('completed', False) and not taken_data.get('expired', False):
                message_ts = taken_data['message_ts']
                channel = taken_data['channel']
                visitor_name = taken_data.get('visitor_name', 'Lead')
                try:
                    client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{visitor_name} bellen~ • {completion_time}", blocks=[], mrkdwn=True)
                    firestore_client.collection("slack_messages").document(doc_id_taken).update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                    results.append({"action": "complete_lead_task_taken", "channel": channel, "email": email})
                    log_json("LEAD_TASK_COMPLETED_TAKEN", appointment_data, results[-1])
                except Exception as e:
                    print(f"❌ Error completing lead task in taken: {e}")

        return results[0] if results else None

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

        completion_time = format_datetime(membership_data.get('received_at', datetime.now()))
        results = []

        if customer_id:
            doc_id_admin = f"{tenant_id}_{LEDENADMINISTRATIE_CHANNEL_ID}_{hash_email(email)}"
            order_doc = firestore_client.collection("slack_messages").document(doc_id_admin).get()
            if order_doc.exists:
                order_data = order_doc.to_dict()
                if not order_data.get('task_completed', False):
                    message_ts = order_data['message_ts']
                    channel = order_data['channel']
                    try:
                        response = client.conversations_history(channel=channel, latest=message_ts, limit=1, inclusive=True)
                        if response['messages']:
                            name_match = re.search(r'\*Order: (.+?)\*', response['messages'][0]['text'])
                            name = name_match.group(1) if name_match else order_data.get('lead_name', 'Order')
                            client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{name} lidmaatschap~ • {completion_time}", mrkdwn=True)
                            firestore_client.collection("slack_messages").document(doc_id_admin).update({'task_completed': True, 'task_completed_at': firestore.SERVER_TIMESTAMP})
                            results.append({"action": "complete_order_task", "channel": channel, "email": email})
                            log_json("ORDER_TASK_COMPLETED_LEDENADMINISTRATIE", membership_data, results[-1])
                    except Exception as e:
                        print(f"❌ Error completing order in ledenadministratie: {e}")

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
                client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{visitor_name} lidmaatschap~ • {completion_time}", mrkdwn=True)
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

        completion_time = format_datetime(membership_data.get('received_at', datetime.now()))
        today_date = datetime.now().strftime('%Y-%m-%d')
        results = []

        doc_id_leads = f"{tenant_id}_{SLACK_CHANNEL_ID}_{hash_email(email)}"
        lead_doc = firestore_client.collection("slack_messages").document(doc_id_leads).get()
        if lead_doc.exists:
            lead_data = lead_doc.to_dict()
            if not lead_data.get('task_completed', False):
                message_ts = lead_data['message_ts']
                channel = lead_data['channel']
                try:
                    response = client.conversations_history(channel=channel, latest=message_ts, limit=1, inclusive=True)
                    if response['messages']:
                        name_match = re.search(r'\*(?:Lead|Bezoeker): (.+?)\*', response['messages'][0]['text'])
                        name = name_match.group(1) if name_match else lead_data.get('lead_name', 'Lead')
                        client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{name} bellen~ • {completion_time}", mrkdwn=True)
                        firestore_client.collection("slack_messages").document(doc_id_leads).update({'task_completed': True, 'task_completed_at': firestore.SERVER_TIMESTAMP})
                        results.append({"action": "complete_lead_task_leads", "channel": channel, "email": email})
                        log_json("LEAD_TASK_COMPLETED_FOR_MEMBERSHIP_LEADS", membership_data, results[-1])
                except Exception as e:
                    print(f"❌ Error completing lead task in leads: {e}")

        doc_id_taken = f"{tenant_id}_{TAKEN_CHANNEL_ID}_prospect_call_{hash_email(email)}_{today_date}"
        taken_doc = firestore_client.collection("slack_messages").document(doc_id_taken).get()
        if taken_doc.exists:
            taken_data = taken_doc.to_dict()
            if not taken_data.get('completed', False) and not taken_data.get('expired', False):
                message_ts = taken_data['message_ts']
                channel = taken_data['channel']
                visitor_name = taken_data.get('visitor_name', 'Lead')
                try:
                    client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{visitor_name} bellen~ • {completion_time}", blocks=[], mrkdwn=True)
                    firestore_client.collection("slack_messages").document(doc_id_taken).update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                    results.append({"action": "complete_lead_task_taken", "channel": channel, "email": email})
                    log_json("LEAD_TASK_COMPLETED_FOR_MEMBERSHIP_TAKEN", membership_data, results[-1])
                except Exception as e:
                    print(f"❌ Error completing lead task in taken: {e}")

        return results[0] if results else None

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

        completion_time = format_datetime(membership_data.get('received_at', datetime.now()))
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
                text=f"✅ ~{visitor_name}~ • {completion_time}",
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


def complete_crm_task_for_appointment(tenant_id, customer_id, appointment_data):
    try:
        if not customer_id:
            return None

        today_date = datetime.now().strftime('%Y-%m-%d')
        client = get_slack_client(tenant_id)
        if not client:
            return None

        completion_time = format_datetime(appointment_data.get('received_at', datetime.now()))
        results = []

        for channel_id in [TAKEN_CHANNEL_ID, CRM_TASKS_CHANNEL_ID]:
            doc_id = f"{tenant_id}_{channel_id}_{customer_id}_{today_date}"
            task_doc_ref = firestore_client.collection("slack_messages").document(doc_id)
            task_doc = task_doc_ref.get()

            if not task_doc.exists:
                log_json("NO_CRM_TASK_FOUND_FOR_APPOINTMENT", appointment_data, {"customer_id": customer_id, "doc_id": doc_id})
                continue

            task_data = task_doc.to_dict()

            if task_data.get('completed', False) or task_data.get('expired', False):
                continue

            if task_data.get('action_type') not in ('appointment', 'contact'):
                continue

            if not task_data.get('visible', True):
                task_doc_ref.update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                result = {"action": "complete_crm_task_invisible", "customer_id": customer_id, "doc_id": doc_id}
                log_json("CRM_TASK_COMPLETED_INVISIBLE", appointment_data, result)
                results.append(result)
                continue

            message_ts = task_data['message_ts']
            channel = task_data['channel']
            visitor_name = task_data.get('visitor_name', 'Bezoeker')

            try:
                client.chat_update(channel=channel, ts=message_ts, text=f"✅ ~{visitor_name} afspraak~ • {completion_time}", mrkdwn=True)
                task_doc_ref.update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
                result = {"action": "complete_crm_task_minimal", "message_ts": message_ts, "channel": channel, "customer_id": customer_id, "visitor_name": visitor_name}
                log_json("CRM_TASK_COMPLETED_MINIMAL", appointment_data, result)
                results.append(result)
            except SlackApiError as e:
                print(f"❌ Slack update error for channel {channel_id}: {e.response['error']}")
            except Exception as e:
                print(f"❌ Error completing CRM task for channel {channel_id}: {e}")

        return results[0] if results else None

    except Exception as e:
        log_json("ERROR_COMPLETE_CRM_TASK", appointment_data, {"error": str(e)})
        return None


def send_initial_lead_message(tenant_id, lead_data):
    try:
        payload = lead_data.get('payload', {})
        firstname = payload.get('firstname', '')
        lastname = payload.get('lastname', '')
        name = f"{firstname} {lastname}".strip() or 'Onbekend'
        email = lead_data.get('email') or payload.get('email', 'Geen email')

        existing_lead = None
        if email and email != 'Geen email':
            existing_lead = check_existing_lead(tenant_id, email)

        if existing_lead:
            log_json("LEAD_RESUBMISSION_SKIPPED", lead_data, {"email": email, "name": name})
            return {"action": "skip_resubmission", "skipped": True}

        client = get_slack_client(tenant_id)
        if not client:
            return None

        phone = payload.get('phone_number', 'Geen telefoon')
        product_interest = lead_data.get('product_interest', 'Niet gespecificeerd')
        traffic_source = lead_data.get('traffic_source', 'Niet gespecificeerd')
        campaign_source = lead_data.get('campaign_source', 'Niet gespecificeerd')
        pagename = lead_data.get('pagename', 'Niet gespecificeerd')
        campaign_display = campaign_source if campaign_source != 'Niet gespecificeerd' else pagename

        message_text = f"*Lead: {name}*"
        message_text += f"\nTelefoon: {phone}"
        message_text += f"\nEmail: {email}"
        message_text += f"\nProduct: {product_interest} • Bron: {traffic_source}, {campaign_display}"

        customer_message = payload.get('message', '')
        if customer_message and customer_message.strip():
            message_text += f"\nBericht: {customer_message}"

        task_time = format_datetime(lead_data.get('received_at', datetime.now()))
        message_text += f"\n📞 Taak: Bel binnen 24 uur • {task_time}"

        try:
            response = client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                text=message_text,
                mrkdwn=True,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            if e.response['error'] == 'not_in_channel':
                response = try_join_and_retry(client, SLACK_CHANNEL_ID, message_text, service_name="slack-listener / send_initial_lead_message", context={"tenant_id": tenant_id, "email": email})
            else:
                notify_dennis(client, "slack-listener / send_initial_lead_message", f"SlackApiError: {e.response['error']}", context={"tenant_id": tenant_id})
                raise

        message_ts = response['ts']
        if email and email != 'Geen email':
            doc_id = f"{tenant_id}_{SLACK_CHANNEL_ID}_{hash_email(email)}"
            firestore_client.collection("slack_messages").document(doc_id).set({
                'message_ts': message_ts, 'channel': SLACK_CHANNEL_ID,
                'email': email.lower(), 'tenant_id': tenant_id,
                'created_at': firestore.SERVER_TIMESTAMP,
                'expires_at': datetime.now() + timedelta(days=30),
                'message_type': 'lead', 'task_completed': False, 'lead_name': name
            })

        result = {"action": "send_initial_lead", "message_ts": message_ts, "channel": SLACK_CHANNEL_ID, "email": email, "name": name}
        log_json("LEAD_MESSAGE_SENT", lead_data, result)
        return result

    except Exception as e:
        print(f"❌ Send error: {e}")
        client = get_slack_client(tenant_id)
        if client:
            notify_dennis(client, "slack-listener / send_initial_lead_message", str(e), context={"tenant_id": tenant_id})
        return None


def should_process_event_for_update(event_type):
    filtered_events = ['customer_updated', 'customerupdate', 'membership_updated', 'membershipupdate', 'visit']
    return event_type.lower() not in [e.lower() for e in filtered_events]


def update_message_with_event(tenant_id, email, event_data):
    try:
        payload = event_data.get('payload', {})
        doc_id_lead = f"{tenant_id}_{SLACK_CHANNEL_ID}_{hash_email(email)}"
        doc_id_order = f"{tenant_id}_{LEDENADMINISTRATIE_CHANNEL_ID}_{hash_email(email)}"
        messages_to_update = []

        for doc_id, msg_type in [(doc_id_lead, "lead"), (doc_id_order, "order")]:
            doc = firestore_client.collection("slack_messages").document(doc_id).get()
            if doc.exists and not doc.to_dict().get('task_completed', False):
                messages_to_update.append((doc, doc_id, msg_type))

        if not messages_to_update:
            return None

        event_type = event_data.get('event_type', 'unknown')
        timestamp = event_data.get('received_at') or event_data.get('timestamp') or event_data.get('created_at')
        if event_type.lower() in ['membership_new', 'membership_created', 'subscription_update'] and payload.get('start_date'):
            timestamp = payload.get('start_date')
        formatted_time = format_datetime(timestamp)

        event_display_name = event_data.get('event_display_name')
        event_details = event_data.get('event_details')

        if event_display_name and event_details:
            dutch_event_names = {
                'Email Bounced': 'Email geweigerd', 'Email Opened': 'Email geopend',
                'Email Sent': 'Email verstuurd', 'Booking': 'Afspraak',
                'Booking Cancelled': 'Afspraak geannuleerd', 'Visit': 'Bezoek',
                'Call Completed': 'Gesprek voltooid', 'Membership New': 'Nieuw lidmaatschap',
                'Trial Started': 'Proefperiode gestart', 'Appointment Booked': 'Afspraak',
                'Appointment Cancelled': 'Afspraak geannuleerd', 'Sms Bounced': 'SMS geweigerd'
            }
            icon_mapping = {
                'Email Bounced': '🚫', 'Email Opened': '📧', 'Email Sent': '📤',
                'Booking': '📅', 'Booking Cancelled': '❌', 'Visit': '🏃‍♀️',
                'Call Completed': '📞', 'Membership New': '🎯', 'Trial Started': '✨',
                'Appointment Booked': '📅', 'Appointment Cancelled': '❌', 'Sms Bounced': '🚫'
            }
            event_line = f"{icon_mapping.get(event_display_name, '📋')} {dutch_event_names.get(event_display_name, event_display_name)}: {event_details} • {formatted_time}"
        else:
            if event_type in ['booking', 'appointment_booked']:
                event_line = f"📅 Afspraak: {payload.get('type', 'Afspraak')} • {formatted_time}"
            elif event_type in ['booking_cancelled', 'appointment_cancelled']:
                event_line = f"❌ Afspraak geannuleerd: {payload.get('type', 'Afspraak')} • {formatted_time}"
            elif event_type == 'email_opened':
                event_line = f"📧 Email geopend: {payload.get('subject', 'Email')} • {formatted_time}"
            elif event_type == 'email_sent':
                event_line = f"📤 Email verstuurd: {payload.get('subject', 'Email')} • {formatted_time}"
            elif event_type in ['membership_new', 'membership_created']:
                event_line = f"🎯 Nieuw lidmaatschap: {payload.get('subscription_name', 'Lidmaatschap')} • {formatted_time}"
            elif event_type == 'subscription_update' and payload.get('status', '').lower() == 'new':
                event_line = f"🎯 Nieuw lidmaatschap: {payload.get('subscription_name', 'Lidmaatschap')} • {formatted_time}"
            elif event_type == 'visit':
                event_line = f"🏃‍♀️ Bezoek • {formatted_time}"
            elif event_type == 'trial_started':
                event_line = f"✨ Proefperiode gestart: {payload.get('subscription_name', 'Proefperiode')} • {formatted_time}"
            elif event_type == 'call_completed':
                event_line = f"📞 Gesprek voltooid • {formatted_time}"
            else:
                event_line = f"📋 {event_type.replace('_', ' ').title()} • {formatted_time}"

        client = get_slack_client(tenant_id)
        if not client:
            return None

        results = []
        for message_doc, doc_id, message_type in messages_to_update:
            try:
                message_data = message_doc.to_dict()
                message_ts = message_data['message_ts']
                channel = message_data['channel']
                response = client.conversations_history(channel=channel, latest=message_ts, limit=1, inclusive=True)
                if not response['messages']:
                    continue
                original_text = response['messages'][0]['text']
                existing_activities = extract_activity_history(original_text)
                sorted_activities = sort_activities_by_timestamp(existing_activities + [event_line])
                base_message_match = re.search(r'^(.*?)(?:\n\nRecente acties:|\n\nRecent Activity:|$)', original_text, re.DOTALL)
                base_message = base_message_match.group(1).strip() if base_message_match else original_text
                client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=base_message + "\n\nRecente acties:\n" + "\n".join(sorted_activities),
                    unfurl_links=False,
                    unfurl_media=False
                )
                results.append({"action": "update_message", "message_ts": message_ts, "channel": channel, "email": email, "event_type": event_type, "message_type": message_type})
            except Exception as e:
                print(f"❌ Update error for {message_type} message: {e}")
                continue

        return results if results else None

    except Exception as e:
        print(f"❌ Update error: {e}")
        return None


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
            lead_result = send_initial_lead_message(tenant_id, envelope)
            if lead_result:
                log_json("OUTPUT_LEADS", envelope, lead_result)
            if not (lead_result and lead_result.get('skipped')):
                crm_task_envelope = build_crm_task_from_lead(envelope)
                task_result = send_crm_task_message(tenant_id, crm_task_envelope)
                if task_result:
                    log_json("OUTPUT_TAKEN", envelope, task_result)

        elif event_type == 'order':
            log_json("INPUT", envelope)
            result = send_order_message(tenant_id, envelope)
            if result:
                log_json("OUTPUT", envelope, result)

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

            result = update_message_with_event(tenant_id, email, event_data=envelope)
            if result:
                log_json("OUTPUT", envelope, result)

        return "OK"

    except Exception as e:
        print(f"❌ Error processing event: {e}")
        import traceback
        print(f"🐛 Full traceback: {traceback.format_exc()}")
        raise
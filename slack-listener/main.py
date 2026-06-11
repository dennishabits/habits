import base64
import json
import os
import hashlib
from datetime import datetime, timedelta
from google.cloud import firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Configuration
PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
SLACK_CHANNEL_ID = "C654VMGG7"

# Initialize services
firestore_client = firestore.Client()
slack_clients = {}


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def get_slack_client(tenant_id):
    """Get Slack client for tenant"""
    global slack_clients
    
    if tenant_id in slack_clients:
        return slack_clients[tenant_id]
    
    try:
        # Get tenant from Firestore
        tenant_doc = firestore_client.collection("tenants").document(tenant_id).get()
        if not tenant_doc.exists:
            return None
        
        tenant_data = tenant_doc.to_dict()
        bot_token = tenant_data.get('slack_bot_token')
        
        if not bot_token:
            return None
        
        if not bot_token.startswith('xoxb-'):
            return None
        
        # Create Slack client
        client = WebClient(token=bot_token)
        slack_clients[tenant_id] = client
        
        return client
        
    except Exception as e:
        print(f"❌ Error creating Slack client: {e}")
        return None


def hash_email(email):
    """Create hash of email for document ID"""
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def format_datetime(timestamp):
    """Format timestamp to readable datetime in Dutch format"""
    if not timestamp:
        return "Onbekende tijd"
    
    try:
        if isinstance(timestamp, str):
            # Parse ISO format or other common formats
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif isinstance(timestamp, (int, float)):
            # Handle epoch timestamps
            dt = datetime.fromtimestamp(timestamp)
        else:
            dt = timestamp
        
        # Dutch month abbreviations
        dutch_months = {
            1: 'jan', 2: 'feb', 3: 'mrt', 4: 'apr', 5: 'mei', 6: 'jun',
            7: 'jul', 8: 'aug', 9: 'sep', 10: 'okt', 11: 'nov', 12: 'dec'
        }
        
        month_name = dutch_months.get(dt.month, str(dt.month))
        return f"{dt.day} {month_name}, {dt.strftime('%H:%M')}"
    except Exception:
        return str(timestamp)


def send_initial_lead_message(tenant_id, lead_data):
    """Send initial clean lead message to Slack"""
    try:
        client = get_slack_client(tenant_id)
        if not client:
            return None
        
        # Get lead info from standardized fields
        payload = lead_data.get('payload', {})
        firstname = payload.get('firstname', '')
        lastname = payload.get('lastname', '')
        name = f"{firstname} {lastname}".strip() or 'Onbekend'
        email = payload.get('email', 'Geen email')
        phone = payload.get('phone_number', 'Geen telefoon')
        product_interest = lead_data.get('product_interest', 'Niet gespecificeerd')
        traffic_source = lead_data.get('traffic_source', 'Niet gespecificeerd')
        campaign_source = lead_data.get('campaign_source', 'Niet gespecificeerd')
        
        # Check if known customer
        is_known_customer = payload.get('is_known_customer', False)
        status_text = "Oud lid" if is_known_customer else "Nieuwe Lead"
        
        # Handle campaign vs pagename - look for pagename field
        pagename = lead_data.get('pagename', 'Niet gespecificeerd')
        if campaign_source == 'Niet gespecificeerd':
            campaign_display = pagename
        else:
            campaign_display = campaign_source
        
        # Create Dutch formatted message
        message_text = f"*{status_text}*\n"
        message_text += f"Naam: {name}\n"
        message_text += f"Telefoon: {phone}\n"
        message_text += f"Email: {email}\n"
        message_text += f"Product: {product_interest}\n"
        message_text += f"Bron: {traffic_source}, {campaign_display}"
        
        # Add customer message if available
        message = payload.get('message', '')
        if message and message.strip():
            message_text += f"\nBericht: {message}"
        
        # Send message
        response = client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=message_text,
            mrkdwn=True
        )
        
        message_ts = response['ts']
        
        # Store message metadata in Firestore
        doc_id = f"{tenant_id}_{hash_email(email)}"
        expires_at = datetime.now() + timedelta(days=30)
        
        slack_message_doc = {
            'message_ts': message_ts,
            'channel': SLACK_CHANNEL_ID,
            'email': email.lower(),
            'tenant_id': tenant_id,
            'created_at': firestore.SERVER_TIMESTAMP,
            'expires_at': expires_at
        }
        
        firestore_client.collection("slack_messages").document(doc_id).set(slack_message_doc)
        
        return {
            "action": "send_initial_lead",
            "message_ts": message_ts,
            "channel": SLACK_CHANNEL_ID,
            "email": email,
            "name": name,
            "status": status_text
        }
        
    except SlackApiError as e:
        print(f"❌ Slack error: {e.response['error']}")
        return None
    except Exception as e:
        print(f"❌ Send error: {e}")
        return None


def update_message_with_event(tenant_id, email, event_data):
    """Update existing message with new event"""
    try:
        # Find existing message
        doc_id = f"{tenant_id}_{hash_email(email)}"
        message_doc = firestore_client.collection("slack_messages").document(doc_id).get()
        
        if not message_doc.exists:
            return None
        
        message_data = message_doc.to_dict()
        message_ts = message_data['message_ts']
        channel = message_data['channel']
        
        client = get_slack_client(tenant_id)
        if not client:
            return None
        
        # Get the original message
        response = client.conversations_history(
            channel=channel,
            latest=message_ts,
            limit=1,
            inclusive=True
        )
        
        if not response['messages']:
            return None
        
        original_text = response['messages'][0]['text']
        
        # Format new event
        event_type = event_data.get('event_type', 'unknown')
        
        # Filter out unwanted event types
        filtered_events = ['customer_new', 'customernew', 'membership_update', 'membership_updated']
        if event_type.lower() in [e.lower() for e in filtered_events]:
            return None
        
        timestamp = event_data.get('timestamp') or event_data.get('created_at')
        formatted_time = format_datetime(timestamp)
        
        # Use standardized display fields if available, otherwise fallback to old method
        event_display_name = event_data.get('event_display_name')
        event_details = event_data.get('event_details')
        
        if event_display_name and event_details:
            # Use new standardized format - translate to Dutch and add icons
            dutch_event_names = {
                'Email Bounced': 'Email geweigerd',
                'Email Opened': 'Email geopend', 
                'Email Sent': 'Email verstuurd',
                'Booking': 'Afspraak',
                'Booking Cancelled': 'Afspraak geannuleerd',
                'Visit': 'Bezoek',
                'Call Completed': 'Gesprek voltooid',
                'Membership New': 'Nieuw lidmaatschap',
                'Trial Started': 'Proefperiode gestart',
                'Appointment Booked': 'Afspraak',
                'Appointment Cancelled': 'Afspraak geannuleerd'
            }
            
            # Get emoji icon for the original event type or display name
            icon = get_event_icon(event_type) or get_event_icon(event_display_name.lower().replace(' ', '_'))
            
            dutch_display_name = dutch_event_names.get(event_display_name, event_display_name)
            event_line = f"{icon} {dutch_display_name}: {event_details} • {formatted_time}"
        else:
            # Fallback to old payload-based extraction - Dutch translations
            payload = event_data.get('payload', {})
            
            # Event type specific formatting in Dutch
            if event_type in ['booking', 'appointment_booked']:
                appointment_type = payload.get('appointment_type', 'Afspraak')
                event_line = f"Afspraak: {appointment_type} • {formatted_time}"
                
            elif event_type in ['booking_cancelled', 'appointment_cancelled']:
                appointment_type = payload.get('appointment_type', 'Afspraak')
                event_line = f"Afspraak geannuleerd: {appointment_type} • {formatted_time}"
                
            elif event_type == 'email_opened':
                subject = payload.get('subject', 'Email')
                event_line = f"Email geopend: {subject} • {formatted_time}"
                
            elif event_type == 'email_sent':
                subject = payload.get('subject', 'Email')
                event_line = f"Email verstuurd: {subject} • {formatted_time}"
                
            elif event_type in ['membership_new', 'membership_created']:
                subscription_name = payload.get('subscription_name', payload.get('membership_type', 'Lidmaatschap'))
                event_line = f"Nieuw lidmaatschap: {subscription_name} • {formatted_time}"
                
            elif event_type == 'visit':
                event_line = f"Bezoek • {formatted_time}"
                
            elif event_type == 'trial_started':
                subscription_name = payload.get('subscription_name', payload.get('trial_type', 'Proefperiode'))
                event_line = f"Proefperiode gestart: {subscription_name} • {formatted_time}"
                
            elif event_type == 'call_completed':
                event_line = f"Gesprek voltooid • {formatted_time}"
                
            else:
                # Default format for other events - translate common terms
                event_name = event_type.replace('_', ' ')
                # Basic Dutch translations for common terms
                dutch_translations = {
                    'customer new': 'nieuwe klant',
                    'membership update': 'lidmaatschap bijgewerkt',
                    'payment successful': 'betaling succesvol',
                    'payment failed': 'betaling mislukt',
                    'login': 'ingelogd',
                    'logout': 'uitgelogd'
                }
                event_name = dutch_translations.get(event_name.lower(), event_name.title())
                event_line = f"{event_name} • {formatted_time}"
        
        # Check if event history section exists (handle both old and new format)
        if "\nRecente acties:" not in original_text and "\nRecent Activity:" not in original_text:
            updated_text = original_text + f"\n\nRecente acties:\n{event_line}"
        else:
            updated_text = original_text + f"\n{event_line}"
        
        # Update the message with text (not blocks)
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=updated_text
        )
        
        return {
            "action": "update_message",
            "message_ts": message_ts,
            "channel": channel,
            "email": email,
            "event_type": event_type,
            "event_line": event_line
        }
        
    except SlackApiError as e:
        print(f"❌ Slack update error: {e.response['error']}")
        return None
    except Exception as e:
        print(f"❌ Update error: {e}")
        return None


def slack_crm_pipeline(cloud_event):
    """Main Cloud Function entry point for Slack CRM - Gen 2 Pub/Sub triggered"""
    try:
        # Decode Pub/Sub message from CloudEvent
        if not cloud_event.data:
            return
            
        # For Gen 2, the data is already base64 decoded
        if isinstance(cloud_event.data, dict):
            envelope = cloud_event.data
        else:
            raw = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
            envelope = json.loads(raw)
        
        log_json("INPUT", envelope)
        
        # Use standardized field names
        event_type = envelope.get('event_type', '')
        webhook_source = envelope.get('webhook_source', '')
        tenant_id = envelope.get('tenant_id', '')
        
        if not tenant_id:
            return
        
        # Handle lead_submitted events from any source
        if event_type == 'lead_submitted':
            result = send_initial_lead_message(tenant_id, envelope)
            
            if result:
                log_json("OUTPUT", result)
        
        # Handle other events - only from specific sources
        elif webhook_source in ['acuity', 'customerio', 'sportivity']:
            # Try to get email from standardized envelope field first, then from payload
            email = envelope.get('email')
            if not email:
                payload = envelope.get('payload', {})
                email = payload.get('email')
            
            if not email:
                return
            
            result = update_message_with_event(tenant_id, email, envelope)
            
            if result:
                log_json("OUTPUT", result)
        
    except Exception as e:
        print(f"❌ Error processing event: {e}")
        import traceback
        print(f"🐛 Full traceback: {traceback.format_exc()}")
        raise  # Re-raise to trigger retry if needed

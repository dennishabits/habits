import base64
import json
import os
from datetime import datetime
from google.cloud import pubsub_v1
import functions_framework


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def convert_epoch_to_iso(timestamp):
    """Convert epoch timestamp to ISO 8601 string"""
    if not timestamp:
        return None
    
    try:
        if isinstance(timestamp, (int, float)):
            # Convert epoch to datetime and format as ISO 8601
            dt = datetime.utcfromtimestamp(timestamp)
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif isinstance(timestamp, str):
            # Already a string, assume it's ISO format
            return timestamp
        else:
            return None
    except Exception as e:
        print(f"❌ Error converting timestamp {timestamp}: {e}")
        return None


class CustomerioTranslator:
    def __init__(self, project_id):
        self.project_id = project_id
        self.publisher = pubsub_v1.PublisherClient()
        self.events_topic = self.publisher.topic_path(project_id, "events")
    
    def publish_to_events(self, envelope):
        try:
            message_data = json.dumps(envelope).encode("utf-8")
            self.publisher.publish(self.events_topic, message_data).result()
        except Exception as e:
            print(f"❌ Error publishing event: {e}")

    def publish_error_event(self, error_message, original_envelope=None):
        """Publish error event to events topic with dennis@habits.fit email"""
        try:
            error_envelope = {
                "webhook_source": "customerio",
                "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
                "event_type": "translation_error",
                "received_at": datetime.utcnow().isoformat() + 'Z',
                "customer_id": None,  # No customer_id for error events
                "email": "dennis@habits.fit",
                "event_display_name": "Translation Error",
                "event_details": "Customer.io Translator",
                "event_secondary_details": "Processing Error",
                "payload": {
                    "error_message": error_message,
                    "service": "customerio-translator",
                    "original_event_type": original_envelope.get("event_type") if original_envelope else None
                }
            }
            
            # Remove None values
            error_envelope = {k: v for k, v in error_envelope.items() if v is not None}
            
            message_data = json.dumps(error_envelope).encode("utf-8")
            self.publisher.publish(self.events_topic, message_data).result()
            log_json("ERROR_EVENT_PUBLISHED", error_envelope)
            
        except Exception as publish_error:
            print(f"Failed to publish error event: {publish_error}")

    def extract_tenant_id_from_customer(self, customer_data):
        """
        Extract tenant_id from Customer.io customer data
        """
        # Try both formats for customer_id
        customer_id = customer_data.get('customer_id')
        if not customer_id:
            # Fallback to identifiers format
            identifiers = customer_data.get('identifiers', {})
            customer_id = identifiers.get('customer_id') or identifiers.get('id')
        
        # Try both formats for email
        email = customer_data.get('email_address') or customer_data.get('email')
        if not email:
            # Fallback to identifiers format
            identifiers = customer_data.get('identifiers', {})
            email = identifiers.get('email')
        
        # Placeholder logic - you can implement your tenant mapping logic here
        if customer_id:
            # Example: if customer_id has format "tenant_123_customer_456"
            if isinstance(customer_id, str) and '_' in customer_id:
                parts = customer_id.split('_')
                if len(parts) >= 2 and parts[0] == 'tenant':
                    return parts[1]
        
        if email:
            # Example: map email domains to tenants
            domain_tenant_map = {
                'fitnessrijen.nl': 'fitness_rijen',
                'sportcenteramsterdam.com': 'sport_amsterdam',
                # Add your domain mappings here
            }
            
            domain = email.split('@')[-1].lower()
            if domain in domain_tenant_map:
                return domain_tenant_map[domain]
        
        # Default fallback
        return 'unknown_tenant'

    def extract_campaign_info(self, message_data):
        """
        Extract campaign and marketing context from Customer.io message data
        """
        data_section = message_data.get('data', {})
        
        # Extract campaign information - handle both formats
        campaign_id = data_section.get('campaign_id')
        campaign_name = data_section.get('campaign_name')
        action_id = data_section.get('action_id')
        action_name = data_section.get('action_name')
        broadcast_id = data_section.get('broadcast_id')
        journey_id = data_section.get('journey_id')
        
        # Map to your campaign_source field
        campaign_source = None
        if campaign_name:
            # Convert campaign name to snake_case format
            campaign_source = campaign_name.lower().replace(' ', '_').replace('-', '_')
        elif action_name:
            # Use action name if campaign name not available
            campaign_source = action_name.lower().replace(' ', '_').replace('-', '_')
        elif campaign_id:
            campaign_source = f"campaign_{campaign_id}"
        elif action_id:
            campaign_source = f"action_{action_id}"
        elif broadcast_id:
            campaign_source = f"broadcast_{broadcast_id}"
        elif journey_id:
            campaign_source = f"journey_{journey_id}"
        
        return {
            'campaign_source': campaign_source,
            'campaign_id': campaign_id,
            'campaign_name': campaign_name,
            'action_id': action_id,
            'action_name': action_name,
            'broadcast_id': broadcast_id,
            'journey_id': journey_id
        }

    def extract_customer_identifiers(self, envelope, data_section):
        """
        Extract customer identifiers from envelope first, then Customer.io data section.
        Handles both current format (from variables.customer) and legacy format (direct fields).
        """
        customer_identifiers = {}
        
        # First check envelope for customer_id and email (from webhook-dispatcher)
        envelope_customer_id = envelope.get("customer_id") or envelope.get("customerId")
        envelope_email = envelope.get("email")
        
        if envelope_customer_id:
            customer_identifiers['customer_id'] = envelope_customer_id
        if envelope_email:
            customer_identifiers['email'] = envelope_email
        
        # If we have both from envelope, return early
        if customer_identifiers.get('customer_id') and customer_identifiers.get('email'):
            return customer_identifiers
        
        # Otherwise, extract from Customer.io data section
        # Primary format: customer info in variables.customer object
        variables = data_section.get('variables', {})
        customer_data = variables.get('customer', {})
        
        if customer_data:
            # Extract customer_id (could be in different fields)
            if not customer_identifiers.get('customer_id'):
                customer_id = (customer_data.get('customer_id') or 
                              customer_data.get('id') or 
                              customer_data.get('cio_id'))
                if customer_id:
                    customer_identifiers['customer_id'] = customer_id
            
            # Extract email
            if not customer_identifiers.get('email'):
                email = (customer_data.get('email') or 
                        customer_data.get('email_address'))
                if email:
                    customer_identifiers['email'] = email
        
        # Fallback format: customer info directly in data object
        if not customer_identifiers.get('customer_id'):
            customer_id = data_section.get('customer_id')
            if customer_id:
                customer_identifiers['customer_id'] = customer_id
        
        if not customer_identifiers.get('email'):
            email = data_section.get('email_address') or data_section.get('email')
            if email:
                customer_identifiers['email'] = email
        
        # Legacy format: customer info in identifiers object
        identifiers = data_section.get('identifiers', {})
        if identifiers and not customer_identifiers:
            customer_id = identifiers.get('customer_id') or identifiers.get('id') or identifiers.get('cio_id')
            if customer_id:
                customer_identifiers['customer_id'] = customer_id
            
            email = identifiers.get('email') or identifiers.get('email_address')
            if email:
                customer_identifiers['email'] = email
        
        return customer_identifiers

    def get_standardized_display_fields(self, event_type, data_section, campaign_info):
        """Get standardized display fields for Slack"""
        subject = data_section.get('subject', 'Email')
        campaign_name = campaign_info.get('campaign_name', '')
        action_name = campaign_info.get('action_name', '')
        
        # Choose the best secondary detail
        secondary_detail = campaign_name or action_name or 'Email Campaign'
        
        if event_type == 'email_opened':
            return {
                "event_display_name": "Email Opened",
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }
        elif event_type == 'email_sent':
            return {
                "event_display_name": "Email Sent", 
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }
        elif event_type == 'email_delivered':
            return {
                "event_display_name": "Email Delivered",
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }
        elif event_type == 'email_clicked':
            return {
                "event_display_name": "Email Clicked",
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }
        elif event_type == 'email_bounced':
            return {
                "event_display_name": "Email Bounced",
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }
        else:
            return {
                "event_display_name": event_type.replace('_', ' ').title(),
                "event_details": subject,
                "event_secondary_details": secondary_detail
            }

    def convert_payload_timestamps(self, payload):
        """Recursively convert epoch timestamps to ISO format in payload"""
        if isinstance(payload, dict):
            converted_payload = {}
            for key, value in payload.items():
                if key in ['timestamp', 'created_at', 'updated_at', 'sent_at', 'delivered_at', 'opened_at', 'clicked_at', 'bounced_at', 'event_timestamp', '_created_in_customerio_at', 'created_at']:
                    # Convert timestamp fields
                    converted_payload[key] = convert_epoch_to_iso(value) if value else value
                elif isinstance(value, dict):
                    # Recursively convert nested objects
                    converted_payload[key] = self.convert_payload_timestamps(value)
                elif isinstance(value, list):
                    # Handle lists
                    converted_payload[key] = [self.convert_payload_timestamps(item) if isinstance(item, dict) else item for item in value]
                else:
                    converted_payload[key] = value
            return converted_payload
        else:
            return payload

    def translate_to_events(self, envelope):
        """
        Translate Customer.io webhook events to standardized event format
        """
        try:
            payload = envelope.get("payload", {})
            
            # Extract basic event information from Customer.io webhook
            object_type = payload.get('object_type')  # 'email' or 'sms'
            metric = payload.get('metric')  # 'delivered', 'opened', 'bounced', etc.
            timestamp = payload.get('timestamp')
            event_id = payload.get('event_id')
            
            # Create generic event type using channel_action format
            if object_type and metric:
                generic_event_type = f"{object_type}_{metric}"
            else:
                self.publish_error_event("Missing object_type or metric in Customer.io event", envelope)
                return None
            
            # Extract customer data
            data_section = payload.get('data', {})
            
            # Extract customer identifiers using the improved method (envelope first)
            customer_identifiers = self.extract_customer_identifiers(envelope, data_section)
            
            if not customer_identifiers.get('customer_id') and not customer_identifiers.get('email'):
                self.publish_error_event("No customer identifier found in Customer.io event", envelope)
                return None
            
            # Extract tenant_id from customer data (or use from envelope)
            tenant_id = envelope.get("tenant_id") or self.extract_tenant_id_from_customer(data_section)
            
            # Extract campaign information
            campaign_info = self.extract_campaign_info(payload)
            
            # Get standardized display fields for Slack
            display_fields = self.get_standardized_display_fields(generic_event_type, data_section, campaign_info)
            
            # Extract message context
            message_id = data_section.get('email_id') or data_section.get('delivery_id')
            subject = data_section.get('subject')
            recipient = data_section.get('recipient') or customer_identifiers.get('email')
            
            # Convert timestamp to ISO format
            iso_timestamp = convert_epoch_to_iso(timestamp)
            
            # Convert all timestamps in the payload to ISO format
            converted_payload = self.convert_payload_timestamps(payload)
            
            # Create clean event envelope following your pattern
            event_envelope = {
                # Technical/Routing Fields (standardized)
                "webhook_source": "customerio",
                "tenant_id": tenant_id,
                "event_type": generic_event_type,
                
                # System metadata - convert receivedAt if it's epoch
                "received_at": envelope.get("receivedAt"),
                "event_id": event_id,
                "timestamp": iso_timestamp,  # Convert main timestamp to ISO
                
                # Customer identification - FIXED: use separate fields
                "customer_id": customer_identifiers.get('customer_id'),  # Actual customer_id (can be None)
                "email": customer_identifiers.get('email'),              # Actual email
                
                # Standardized display fields for Slack
                **display_fields,
                
                # Business Context Fields
                "campaign_source": campaign_info.get('campaign_source'),
                "traffic_source": envelope.get("traffic_source"),  # From webhook-dispatcher
                "page_source": None,     # Not available from Customer.io
                "product_interest": None, # Could be derived from campaign if needed
                "brand": None,           # Will be determined by tenant_id mapping
                
                # Message Context
                "message_id": message_id,
                "message_type": object_type,
                "message_subject": subject,
                "recipient": recipient,
                
                # Campaign Context
                "campaign_id": campaign_info.get('campaign_id'),
                "campaign_name": campaign_info.get('campaign_name'),
                "action_id": campaign_info.get('action_id'),
                "action_name": campaign_info.get('action_name'),
                "broadcast_id": campaign_info.get('broadcast_id'),
                "journey_id": campaign_info.get('journey_id'),
                
                # Complete original payload with converted timestamps
                "payload": converted_payload
            }
            
            # Remove None values from envelope
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            
            self.publish_to_events(event_envelope)
            
            return event_envelope
            
        except Exception as e:
            self.publish_error_event(f"Translation error: {e}", envelope)
            return None


@functions_framework.cloud_event
def customerio_pipeline(cloud_event):
    """Gen2 Cloud Function entry point for Pub/Sub trigger with proper CloudEvent signature"""
    try:
        # Extract Pub/Sub message from CloudEvent
        message_data = cloud_event.data.get('message', {}).get('data')
        
        if not message_data:
            print("No message data found")
            return "OK"
            
        raw = base64.b64decode(message_data).decode("utf-8")
        envelope = json.loads(raw)
        
        log_json("INPUT", envelope)
        
        # Use standardized field names
        webhook_source = envelope.get("webhook_source", "").lower()
        event_type = envelope.get("event_type", "").lower()
        
        # Check webhook_source for customerio
        if webhook_source != "customerio":
            return "OK"
        
        project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
        translator = CustomerioTranslator(project_id)
        
        translated_envelope = translator.translate_to_events(envelope)
        
        if translated_envelope:
            log_json("OUTPUT", translated_envelope)
        
        return "OK"
        
    except Exception as e:
        print(f"❌ Translation error: {e}")
        import traceback
        print(f"🐛 Traceback: {traceback.format_exc()}")
        return "ERROR"
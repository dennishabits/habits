import base64
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from google.cloud import pubsub_v1
import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat
import functions_framework


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def standardize_datetime_field(value):
    """Convert various datetime formats to ISO 8601 format with Europe/Amsterdam timezone"""
    if not value:
        return None
        
    try:
        amsterdam_tz = ZoneInfo("Europe/Amsterdam")
        
        # If it's already a Unix timestamp (integer or float)
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=amsterdam_tz)
            return dt.isoformat()
        
        # If it's a string, try to parse it
        if isinstance(value, str):
            # If it already has timezone info, return as-is
            if value.endswith('Z') or '+' in value[-6:] or value.endswith('+00:00'):
                return value
                
            # Try common formats
            formats_to_try = [
                '%Y-%m-%dT%H:%M:%S.%f',       # ISO with microseconds
                '%Y-%m-%dT%H:%M:%S',          # ISO without microseconds  
                '%Y-%m-%d %H:%M:%S',          # Standard datetime
                '%Y-%m-%d',                   # Date only
                '%d/%m/%Y %H:%M:%S',          # DD/MM/YYYY HH:MM:SS
                '%d/%m/%Y',                   # DD/MM/YYYY
                '%m/%d/%Y %H:%M:%S',          # MM/DD/YYYY HH:MM:SS
                '%m/%d/%Y',                   # MM/DD/YYYY
            ]
            
            for fmt in formats_to_try:
                try:
                    dt = datetime.strptime(value, fmt)
                    # Add timezone info
                    dt_with_tz = dt.replace(tzinfo=amsterdam_tz)
                    return dt_with_tz.isoformat()
                except ValueError:
                    continue
                    
            # If no format works, try parsing as timestamp string
            try:
                timestamp = float(value)
                dt = datetime.fromtimestamp(timestamp, tz=amsterdam_tz)
                return dt.isoformat()
            except ValueError:
                return value  # Return original if can't parse
        
        return value
        
    except Exception as e:
        print(f"Warning: Error standardizing datetime {value}: {e}")
        return value


def normalize_phone_number(phone_value):
    """
    Normalize phone number to E.164 format using phonenumbers library
    Assumes Dutch (+31) region if no country code is provided
    """
    if not phone_value:
        return None
    
    try:
        # Clean the input
        phone_str = str(phone_value).strip()
        if not phone_str:
            return None
        
        # Try to parse the phone number
        # First try with no region (for international numbers with + prefix)
        try:
            parsed_number = phonenumbers.parse(phone_str, None)
        except NumberParseException:
            # If that fails, assume Dutch region
            try:
                parsed_number = phonenumbers.parse(phone_str, "NL")
            except NumberParseException as e:
                print(f"Warning: Could not parse phone number '{phone_str}': {e}")
                return phone_str  # Return original if can't parse
        
        # Validate the number
        if not phonenumbers.is_valid_number(parsed_number):
            print(f"Warning: Invalid phone number '{phone_str}'")
            return phone_str  # Return original if invalid
        
        # Format to E.164
        e164_number = phonenumbers.format_number(parsed_number, PhoneNumberFormat.E164)
        return e164_number
        
    except Exception as e:
        print(f"Error normalizing phone number '{phone_value}': {e}")
        return phone_value  # Return original on any error


def convert_camelcase_to_snake_case(name):
    """
    Convert camelCase field names to snake_case
    Special cases: keep firstname and lastname without underscores
    """
    # Special cases for names
    if name in ['firstName', 'firstname']:
        return 'firstname'
    if name in ['lastName', 'lastname']:
        return 'lastname'
    
    # Handle all caps words (like ID) - convert to lowercase
    if name.isupper() and len(name) <= 4:
        return name.lower()
    
    # Insert underscore before uppercase letters (except the first one)
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    
    # Insert underscore before uppercase letters that follow lowercase letters
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    
    return s2.lower()


def combine_house_number_fields(house_number, house_number_addition):
    """Combine house_number and house_number_addition into house_number field"""
    if not house_number:
        return house_number
    
    if house_number_addition and str(house_number_addition).strip():
        return f"{str(house_number).strip()} {str(house_number_addition).strip()}"
    return str(house_number).strip()


def apply_field_transformations(data):
    """Apply business logic transformations to field values"""
    if not isinstance(data, dict):
        return data
    
    transformed_data = data.copy()
    
    # Combine house_number with house_number_addition
    if 'house_number' in transformed_data:
        house_number = transformed_data.get('house_number')
        house_number_addition = transformed_data.get('house_number_addition')
        
        transformed_data['house_number'] = combine_house_number_fields(house_number, house_number_addition)
        
        # Remove house_number_addition field
        transformed_data.pop('house_number_addition', None)
    
    # Field name standardization with value transformations
    field_renames = {
        'phone': 'phone_number',
        'address': 'street',
        'zipcode': 'zip',
        'dob': 'birth_date',
    }
    
    for old_name, new_name in field_renames.items():
        if old_name in transformed_data:
            value = transformed_data.pop(old_name)
            
            # Apply specific transformations based on field type
            if new_name == 'phone_number':
                # Normalize phone number to international format
                transformed_data[new_name] = normalize_phone_number(value)
            else:
                transformed_data[new_name] = value
    
    # Also normalize existing phone_number fields (in case they weren't renamed)
    if 'phone_number' in transformed_data:
        transformed_data['phone_number'] = normalize_phone_number(transformed_data['phone_number'])
    
    # Remove email from payload (should only be in envelope)
    transformed_data.pop('email', None)
    
    return transformed_data


def convert_dict_keys_to_snake_case(data):
    """Recursively convert all dictionary keys from camelCase to snake_case"""
    if isinstance(data, dict):
        converted_dict = {}
        for key, value in data.items():
            # Convert the key to snake_case
            snake_key = convert_camelcase_to_snake_case(key)
            
            # Recursively convert nested dictionaries and lists
            if isinstance(value, dict):
                converted_dict[snake_key] = convert_dict_keys_to_snake_case(value)
            elif isinstance(value, list):
                converted_dict[snake_key] = [
                    convert_dict_keys_to_snake_case(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                converted_dict[snake_key] = value
        return converted_dict
    
    elif isinstance(data, list):
        return [
            convert_dict_keys_to_snake_case(item) if isinstance(item, dict) else item
            for item in data
        ]
    
    else:
        return data


def standardize_dates_in_payload(payload):
    """Standardize all date fields in a payload to ISO 8601 format"""
    if not isinstance(payload, dict):
        return payload
    
    # Common date field names
    date_fields = {
        'datetime', 'created_at', 'updated_at', 'timestamp', 'received_at', 'submitted_at',
        'start_date', 'end_date', 'birth_date', 'subscription_future'
    }
    
    standardized_payload = {}
    
    for key, value in payload.items():
        if isinstance(value, dict):
            # Recursively standardize nested dictionaries
            standardized_payload[key] = standardize_dates_in_payload(value)
        elif isinstance(value, list):
            # Handle lists that might contain dictionaries with dates
            standardized_list = []
            for item in value:
                if isinstance(item, dict):
                    standardized_list.append(standardize_dates_in_payload(item))
                else:
                    standardized_list.append(item)
            standardized_payload[key] = standardized_list
        elif key in date_fields:
            # Standardize date field
            standardized_payload[key] = standardize_datetime_field(value)
        else:
            # Keep other fields as-is
            standardized_payload[key] = value
    
    return standardized_payload


def clean_and_transform_payload(payload):
    """
    Complete payload processing:
    1. Convert camelCase to snake_case
    2. Standardize date fields to ISO 8601
    3. Apply field transformations and removals
    """
    # First convert all keys to snake_case
    snake_case_payload = convert_dict_keys_to_snake_case(payload)
    
    # Then standardize all date fields
    standardized_payload = standardize_dates_in_payload(snake_case_payload)
    
    # Finally apply business transformations
    cleaned_payload = apply_field_transformations(standardized_payload)
    
    return cleaned_payload


class LeadformTranslator:
    def __init__(self, project_id):
        self.project_id = project_id
        self.publisher = pubsub_v1.PublisherClient()
        self.events_topic = self.publisher.topic_path(project_id, "events")
    
    def publish_to_events(self, envelope):
        try:
            message_data = json.dumps(envelope).encode("utf-8")
            self.publisher.publish(self.events_topic, message_data).result()
        except Exception as e:
            print(f"Error publishing event: {e}")

    def publish_error_event(self, error_message, original_envelope=None):
        """Publish error event to events topic with dennis@habits.fit email"""
        try:
            error_envelope = {
                "webhook_source": "leadform",
                "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
                "event_type": "translation_error",
                "received_at": datetime.utcnow().isoformat() + 'Z',
                "customer_id": None,
                "email": "dennis@habits.fit",
                "event_display_name": "Translation Error",
                "event_details": "Leadform Translator",
                "event_secondary_details": "Processing Error",
                "payload": {
                    "error_message": error_message,
                    "service": "leadform-translator",
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

    def extract_customer_identifiers(self, envelope, payload):
        """Extract customer_id and email separately from envelope and payload"""
        # Extract customer_id from envelope (if present)
        customer_id = envelope.get("customer_id") or envelope.get("customerId")
        
        # Extract email from envelope first, then payload
        email = envelope.get("email") or payload.get("email") or payload.get("Email")
        
        # If no email found, create a fallback identifier using phone
        if not email:
            phone = payload.get("phone_number") or payload.get("phone")
            if phone:
                # Normalize phone first, then use as identifier
                normalized_phone = normalize_phone_number(phone)
                if normalized_phone and normalized_phone.startswith('+'):
                    # Use normalized phone as identifier if no email
                    email = f"lead_phone_{normalized_phone.replace('+', '').replace(' ', '')}@temp.local"
            
            # Last resort: use timestamp-based identifier
            if not email:
                timestamp = int(datetime.utcnow().timestamp())
                email = f"lead_{timestamp}@temp.local"
        
        return customer_id, email

    def extract_clean_business_context(self, envelope):
        """Extract and clean business context fields - minimal mapping (dispatcher already did most)"""
        business_context = {}
        
        # These fields are already mapped by webhook-dispatcher, just pass through
        if envelope.get('traffic_source'):
            business_context['traffic_source'] = envelope['traffic_source']
        
        if envelope.get('page_source'):
            business_context['page_source'] = envelope['page_source']
        
        if envelope.get('product_interest'):
            business_context['product_interest'] = envelope['product_interest']
        
        # Map campaign → campaign_source (ONLY mapping done here)
        # Priority: utm_campaign > campaign (dispatcher already prioritized utm_campaign > campaign)
        if envelope.get('utm_campaign'):
            business_context['campaign_source'] = envelope['utm_campaign']
        elif envelope.get('campaign'):
            business_context['campaign_source'] = envelope['campaign']
        
        # Pass through brand
        if envelope.get('brand'):
            business_context['brand'] = envelope['brand']
        
        # Pass through remaining UTM parameters
        if envelope.get('utm_medium'):
            business_context['utm_medium'] = envelope['utm_medium']
        
        if envelope.get('utm_content'):
            business_context['utm_content'] = envelope['utm_content']
        
        if envelope.get('utm_term'):
            business_context['utm_term'] = envelope['utm_term']
        
        # Pass through click IDs
        if envelope.get('gclid'):
            business_context['gclid'] = envelope['gclid']
        
        if envelope.get('fbclid'):
            business_context['fbclid'] = envelope['fbclid']
        
        return business_context

    def get_standardized_display_fields(self, event_type, payload, business_context):
        """Get standardized display fields for Slack"""
        # Extract customer name
        firstname = payload.get("firstname") or payload.get("firstName", "")
        lastname = payload.get("lastname") or payload.get("lastName", "")
        customer_name = f"{firstname} {lastname}".strip() or "New Lead"
        
        # Extract product interest or page source
        product_interest = business_context.get("product_interest", "")
        page_source = business_context.get("page_source", "")
        
        # Choose best detail to show
        lead_detail = product_interest or page_source or "Lead Form"
        
        if event_type == 'lead_submitted':
            return {
                "event_display_name": "New Lead",
                "event_details": customer_name,
                "event_secondary_details": lead_detail
            }
        elif event_type == 'contactform_submitted':
            return {
                "event_display_name": "Contact Form",
                "event_details": customer_name,
                "event_secondary_details": lead_detail
            }
        elif event_type == 'newsletter_signup':
            return {
                "event_display_name": "Newsletter Signup",
                "event_details": customer_name,
                "event_secondary_details": "Newsletter"
            }
        else:
            return {
                "event_display_name": event_type.replace('_', ' ').title(),
                "event_details": customer_name,
                "event_secondary_details": lead_detail
            }

    def translate_to_events(self, envelope):
        """Translate leadform events to standardized event format"""
        try:
            payload = envelope.get("payload", {})
            event_type = envelope.get("event_type", "").lower()
            
            # Extract customer_id and email separately
            customer_id, email = self.extract_customer_identifiers(envelope, payload)
            
            # Extract clean business context from envelope
            business_context = self.extract_clean_business_context(envelope)
            
            # Get standardized display fields for Slack
            display_fields = self.get_standardized_display_fields(event_type, payload, business_context)
            
            # Create customer profile data for CustomerIO (standardized field names)
            customer_data = {
                "email": email,
                "firstname": payload.get("firstname") or payload.get("firstName"),
                "lastname": payload.get("lastname") or payload.get("lastName"),
                "phone_number": payload.get("phone_number") or payload.get("phone")
            }
            
            # Normalize phone number in customer data
            if customer_data.get("phone_number"):
                customer_data["phone_number"] = normalize_phone_number(customer_data["phone_number"])
            
            # Remove None values and clean for Customer.io
            customer_data = {k: v for k, v in customer_data.items() if v is not None}
            clean_customer_data = clean_and_transform_payload(customer_data)
            
            events_published = []
            
            # Only create customer update if we have more than just email
            if len(clean_customer_data) > 1:
                customer_envelope = {
                    # Technical/Routing Fields (standardized)
                    "webhook_source": envelope.get("webhook_source") or "leadform",
                    "tenant_id": envelope.get("tenant_id"),
                    "event_type": "customer_updated",
                    
                    # System metadata
                    "received_at": envelope.get("receivedAt"),
                    "customer_id": customer_id,
                    "email": email,
                    
                    # Standardized display fields for Slack (customer update events)
                    "event_display_name": "Customer Updated",
                    "event_details": display_fields.get("event_details", "Customer"),
                    "event_secondary_details": "Profile Updated",
                    
                    # Clean business context fields
                    **business_context,
                    
                    # Payload
                    "payload": {
                        "GetCustomersUpdate": [clean_customer_data]
                    }
                }
                
                # Remove None values from envelope
                customer_envelope = {k: v for k, v in customer_envelope.items() if v is not None}
                
                self.publish_to_events(customer_envelope)
                events_published.append("customer_updated")
            
            # Create lead behavioral event
            # Clean the original payload for the lead event
            clean_lead_payload = clean_and_transform_payload(payload)
            
            event_envelope = {
                # Technical/Routing Fields (standardized)
                "webhook_source": envelope.get("webhook_source") or "leadform",
                "tenant_id": envelope.get("tenant_id"),
                "event_type": event_type,
                
                # System metadata
                "received_at": envelope.get("receivedAt"),
                "customer_id": customer_id,
                "email": email,
                
                # Standardized display fields for Slack
                **display_fields,
                
                # Clean business context fields
                **business_context,
                
                # Payload - cleaned of technical/legacy fields
                "payload": clean_lead_payload
            }
            
            # Remove None values from envelope
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            
            self.publish_to_events(event_envelope)
            events_published.append(event_type)
            
            return {
                "customer_id": customer_id,
                "email": email,
                "events_published": events_published,
                "business_context": list(business_context.keys()) if business_context else [],
                "display_fields": display_fields,
                "final_event": event_envelope
            }
            
        except Exception as e:
            self.publish_error_event(f"Translation error: {e}", envelope)
            return None


@functions_framework.cloud_event
def leadform_translator(cloud_event):
    """Cloud Function entry point for Pub/Sub trigger"""
    try:
        # Extract Pub/Sub message data from CloudEvent
        message_data = cloud_event.data.get("message", {}).get("data")
        
        if not message_data:
            print("No message data found")
            return "OK"
            
        raw = base64.b64decode(message_data).decode("utf-8")
        envelope = json.loads(raw)
        
        log_json("INPUT", {
            "envelope": envelope,
            "payload": envelope.get("payload", {})
        })
        
        # Use standardized field names with backward compatibility
        webhook_source = (envelope.get("webhook_source") or envelope.get("source") or "").lower()
        event_type = (envelope.get("event_type") or envelope.get("eventType") or "").lower()
        
        if webhook_source != "leadform":
            return "OK"
        
        project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
        translator = LeadformTranslator(project_id)
        
        translation_result = translator.translate_to_events(envelope)
        
        if translation_result:
            log_json("OUTPUT", {
                "envelope": translation_result.get("final_event", {}),
                "payload": translation_result.get("final_event", {}).get("payload", {})
            })
        
        return "OK"
        
    except Exception as e:
        print(f"Translation error: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return "ERROR"
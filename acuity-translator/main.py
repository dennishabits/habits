import base64
import json
import os
import urllib.parse
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.cloud import pubsub_v1
import functions_framework


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def standardize_datetime_field(value):
    """Convert various datetime formats to ISO 8601 format in UTC"""
    if not value:
        return None
        
    try:
        # If it's already a Unix timestamp (integer or float)
        if isinstance(value, (int, float)):
            # Convert epoch timestamp directly to UTC ISO format
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
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
                    # Assume naive datetime is UTC and add timezone info
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except ValueError:
                    continue
                    
            # If no format works, try parsing as timestamp string
            try:
                timestamp = float(value)
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                return dt.isoformat()
            except ValueError:
                return value  # Return original if can't parse
        
        return value
        
    except Exception as e:
        print(f"Warning: Error standardizing datetime {value}: {e}")
        return value


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
    
    # Field name standardization
    field_renames = {
        'phone': 'phone_number',
        'address': 'street',
        'zipcode': 'zip',
    }
    
    for old_name, new_name in field_renames.items():
        if old_name in transformed_data:
            transformed_data[new_name] = transformed_data.pop(old_name)
    
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
        'datetime', 'created_at', 'updated_at', 'timestamp', 'received_at',
        'start_date', 'end_date', 'birth_date', 'booking_date', 'appointment_date',
        'datetime_created'
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


def filter_and_standardize_payload(payload):
    """
    Filter payload to only keep specific fields and standardize dates
    Converts camelCase to snake_case for output
    Maps Acuity fields to generic appointment format
    """
    # Define the allowed fields - include both camelCase (input) and snake_case (output)
    allowed_fields = {
        'calendar', 'datetime', 'confirmationPage', 'confirmation_page',
        'datetimeCreated', 'datetime_created', 'duration', 'type',
        'firstName', 'firstname', 'lastName', 'lastname'
    }
    
    # Filter to only allowed fields
    filtered_payload = {
        key: value for key, value in payload.items() 
        if key in allowed_fields
    }
    
    # Convert camelCase keys to snake_case
    snake_case_payload = {}
    for key, value in filtered_payload.items():
        snake_key = convert_camelcase_to_snake_case(key)
        snake_case_payload[snake_key] = value
    
    # Standardize date fields
    date_fields = {'datetime', 'datetime_created', 'start_at'}
    
    for key, value in snake_case_payload.items():
        if key in date_fields:
            snake_case_payload[key] = standardize_datetime_field(value)
    
    # MAP ACUITY FIELDS TO GENERIC APPOINTMENT FORMAT
    # Keep original Acuity fields for backward compatibility
    if 'datetime' in snake_case_payload:
        snake_case_payload['start_at'] = snake_case_payload['datetime']
    
    if 'type' in snake_case_payload:
        snake_case_payload['activity'] = snake_case_payload['type']
    
    if 'calendar' in snake_case_payload:
        snake_case_payload['employee'] = snake_case_payload['calendar']
    
    return snake_case_payload


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


class AcuityTranslator:
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
                "webhook_source": "acuity",
                "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
                "event_type": "translation_error",
                "received_at": datetime.utcnow().isoformat() + 'Z',
                "customer_id": None,  # No customer_id for error events
                "email": "dennis@habits.fit",
                "event_display_name": "Translation Error",
                "event_details": "Acuity Translator",
                "event_secondary_details": "Processing Error",
                "payload": {
                    "error_message": error_message,
                    "service": "acuity-translator",
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

    def extract_confirmation_page_id(self, confirmation_url):
        """Extract confirmation page ID from Acuity URL"""
        if not confirmation_url:
            return None
        
        try:
            # Parse the URL
            parsed_url = urllib.parse.urlparse(confirmation_url)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            
            # Extract the id parameter (it comes as a list)
            if 'id[]' in query_params and query_params['id[]']:
                return query_params['id[]'][0]
        except Exception as e:
            print(f"Warning: Could not extract confirmation page ID from URL: {confirmation_url}, error: {e}")
        
        return None

    def get_standardized_display_fields(self, status, payload):
        """Get standardized display fields for Slack based on appointment status"""
        appointment_type = payload.get('type', 'Appointment')
        trainer = payload.get('calendar', 'Staff')
        
        if status == 'new':
            return {
                "event_display_name": "Appointment",
                "event_details": appointment_type,
                "event_secondary_details": trainer
            }
        elif status == 'cancelled':
            return {
                "event_display_name": "Appointment Cancelled", 
                "event_details": appointment_type,
                "event_secondary_details": "Customer cancelled"
            }
        else:
            # Handle future statuses (completed, rescheduled, no-show, etc.)
            return {
                "event_display_name": f"Appointment {status.title()}",
                "event_details": appointment_type,
                "event_secondary_details": trainer
            }

    def translate_to_events(self, envelope):
        """
        Translate enriched Acuity events to standardized event format
        """
        try:
            payload = envelope.get("payload", {})
            event_type = envelope.get("event_type", "").lower()
            
            # Extract customer data from enriched envelope BEFORE transformations
            customer_id = envelope.get("customer_id")  # From BigQuery if found
            customer_email = envelope.get("email")     # From Acuity API
            customer_phone = envelope.get("phone")     # From Acuity API
            
            # Check if we have customer identification
            if not customer_email and not customer_id:
                self.publish_error_event("No customer identifier found in Acuity event", envelope)
                return None
            
            # Map Acuity event types to status values
            status_mapping = {
                'appointment': 'new',
                'appointment_cancelled': 'cancelled'
            }
            status = status_mapping.get(event_type, 'unknown')
            
            # Always use 'appointment' as the generic event type
            generic_event_type = 'appointment'
            
            # Filter and standardize the payload (now includes firstname/lastname and converts to snake_case)
            # Also maps to generic appointment format: datetime->start_at, type->activity, calendar->employee
            filtered_payload = filter_and_standardize_payload(payload)
            
            # Add status to payload
            filtered_payload['status'] = status
            
            # Extract confirmation page ID from original confirmationPage URL
            confirmation_url = payload.get('confirmationPage')
            confirmation_page_id = self.extract_confirmation_page_id(confirmation_url)
            if confirmation_page_id:
                filtered_payload['confirmation_page'] = confirmation_page_id
            
            # Add enrichment metadata to filtered payload
            filtered_payload['is_known_customer'] = payload.get('is_known_customer', False)
            filtered_payload['enriched'] = payload.get('enriched', False)
            
            # Get standardized display fields for Slack (pass status instead of event_type)
            display_fields = self.get_standardized_display_fields(status, payload)
            
            # Create clean event envelope with required format
            event_envelope = {
                # Technical/Routing Fields (standardized)
                "webhook_source": "acuity",
                "tenant_id": envelope.get("tenant_id"),
                "event_type": generic_event_type,
                
                # System metadata
                "event_id": envelope.get("event_id"),
                "received_at": envelope.get("receivedAt"),
                "timestamp": filtered_payload.get("start_at"),  # Use generic field name
                
                # Customer identification (standardized envelope fields)
                "customer_id": customer_id,  # Use actual customer_id (can be None)
                "email": customer_email,     # Use actual email
                
                # Standardized display fields for Slack
                **display_fields,
                
                # Business Context Fields
                "campaign_source": envelope.get("campaign_source"),
                "traffic_source": envelope.get("traffic_source"),
                "page_source": envelope.get("page_source"),
                "product_interest": envelope.get("product_interest"),
                "brand": envelope.get("brand"),
                
                # Event-specific payload (filtered Acuity data with standardized dates AND generic field names)
                "payload": filtered_payload
            }
            
            # Remove None values from envelope
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            
            # Remove None values from payload
            if event_envelope.get("payload"):
                event_envelope["payload"] = {k: v for k, v in event_envelope["payload"].items() if v is not None}
            
            self.publish_to_events(event_envelope)
            
            return event_envelope
            
        except Exception as e:
            self.publish_error_event(f"Translation error: {e}", envelope)
            return None


@functions_framework.cloud_event
def acuity_pipeline(cloud_event):
    """Gen2 Cloud Function entry point for Pub/Sub trigger with proper CloudEvent signature"""
    try:
        # Extract Pub/Sub message from CloudEvent
        message_data = cloud_event.data.get('message', {}).get('data')
            
        if not message_data:
            print("No message data found")
            return "OK"
            
        raw = base64.b64decode(message_data).decode("utf-8")
        envelope = json.loads(raw)
        
        log_json("INPUT", {
            "envelope": envelope,
            "payload": envelope.get("payload", {})
        })
        
        # Use standardized field names
        webhook_source = envelope.get("webhook_source", "").lower()
        event_type = envelope.get("event_type", "").lower()
        
        # Check webhook_source for acuity
        if webhook_source != "acuity":
            return "OK"
        
        project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
        translator = AcuityTranslator(project_id)
        
        translated_envelope = translator.translate_to_events(envelope)
        
        if translated_envelope:
            log_json("TO_EVENTS", {
                "envelope": translated_envelope,
                "payload": translated_envelope.get("payload", {})
            })
        
        return "OK"
        
    except Exception as e:
        print(f"Translation error: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return "ERROR"
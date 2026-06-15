import base64
import json
import os
import requests
import functions_framework
from google.cloud import secretmanager, pubsub_v1
from datetime import datetime
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT") or "solid-future-452906-a2"
CUSTOMERIO_API_BASE = "https://track.customer.io/api/v1"

secret_client = secretmanager.SecretManagerServiceClient()


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def get_customerio_credentials(tenant_id):
    """Get Customer.io site ID and API key from Secret Manager"""
    try:
        # Retrieve shared credentials from Secret Manager
        site_id_name = f"projects/{PROJECT_ID}/secrets/customerio-site-id/versions/latest"
        api_key_name = f"projects/{PROJECT_ID}/secrets/customerio-api-key/versions/latest"
        
        site_id_response = secret_client.access_secret_version(request={"name": site_id_name})
        site_id = site_id_response.payload.data.decode("UTF-8")
        
        api_key_response = secret_client.access_secret_version(request={"name": api_key_name})
        api_key = api_key_response.payload.data.decode("UTF-8")
        
        return site_id, api_key
        
    except Exception as e:
        print(f"Error fetching Customer.io credentials: {e}")
        return None, None


def publish_error_event(error_message, original_envelope=None):
    """Publish error event to events topic with dennis@habits.fit email"""
    try:
        publisher = pubsub_v1.PublisherClient()
        events_topic = publisher.topic_path(PROJECT_ID, "events")
        
        error_envelope = {
            "webhook_source": "customerio",
            "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
            "event_type": "processing_error",
            "received_at": datetime.utcnow().isoformat() + 'Z',
            "customer_id": "dennis@habits.fit",
            "email": "dennis@habits.fit",
            "event_display_name": "Processing Error",
            "event_details": "CustomerIO Listener",
            "event_secondary_details": "Payload Processing Error",
            "payload": {
                "error_message": f"**{error_message}**",
                "service": "customerio-listener",
                "original_event_type": original_envelope.get("event_type") if original_envelope else None
            }
        }
        
        # Remove None values
        error_envelope = {k: v for k, v in error_envelope.items() if v is not None}
        
        message_data = json.dumps(error_envelope).encode("utf-8")
        publisher.publish(events_topic, message_data).result()
        log_json("ERROR_EVENT_PUBLISHED", error_envelope)
        
    except Exception as publish_error:
        print(f"Failed to publish error event: {publish_error}")


def filter_payload_by_changed_fields(payload, changed_fields):
    """
    Filter payload to only include changed fields, including nested field filtering
    """
    if not changed_fields:
        return {}
    
    if not isinstance(payload, dict):
        return payload
    
    filtered_payload = {}
    
    for field_path in changed_fields:
        # Handle nested field paths (e.g., "customer_data.firstname")
        if '.' in field_path:
            field_parts = field_path.split('.')
            current_payload = payload
            current_filtered = filtered_payload
            
            # Navigate through nested structure
            for i, part in enumerate(field_parts):
                if i == len(field_parts) - 1:
                    # Last part - copy the actual value
                    if part in current_payload:
                        current_filtered[part] = current_payload[part]
                else:
                    # Intermediate part - ensure nested structure exists
                    if part in current_payload and isinstance(current_payload[part], dict):
                        if part not in current_filtered:
                            current_filtered[part] = {}
                        current_payload = current_payload[part]
                        current_filtered = current_filtered[part]
                    else:
                        # Path doesn't exist in payload, skip this field
                        break
        else:
            # Simple field path
            if field_path in payload:
                filtered_payload[field_path] = payload[field_path]
    
    return filtered_payload


def get_amsterdam_timezone_for_date(dt):
    """Get proper Amsterdam timezone for a specific date with correct DST handling"""
    from datetime import timezone, timedelta
    
    # Amsterdam DST rules: Last Sunday in March to last Sunday in October
    year = dt.year
    
    # Calculate DST start (last Sunday in March)
    march_31 = datetime(year, 3, 31)
    dst_start = march_31 - timedelta(days=march_31.weekday() + 1)  # Last Sunday in March
    
    # Calculate DST end (last Sunday in October) 
    october_31 = datetime(year, 10, 31)
    dst_end = october_31 - timedelta(days=october_31.weekday() + 1)  # Last Sunday in October
    
    # Check if date falls within DST period
    if dst_start <= dt.replace(tzinfo=None) < dst_end:
        return timezone(timedelta(hours=2))  # CEST (UTC+2)
    else:
        return timezone(timedelta(hours=1))  # CET (UTC+1)


def convert_datetime_to_epoch(value):
    """Convert datetime values to Unix epoch timestamps for Customer.io with proper Amsterdam timezone handling"""
    if not value:
        return None
        
    try:
        # If it's already a Unix timestamp (integer or float), return as is
        if isinstance(value, (int, float)):
            return int(value)
        
        # If it's a string, try to parse it
        if isinstance(value, str):
            from datetime import timezone, timedelta
            
            # Try common formats
            formats_to_try = [
                '%Y-%m-%dT%H:%M:%S+%f',    # ISO with timezone offset like +0200
                '%Y-%m-%dT%H:%M:%S%z',     # ISO with timezone
                '%Y-%m-%dT%H:%M:%S.%fZ',   # ISO with microseconds
                '%Y-%m-%dT%H:%M:%SZ',      # ISO without microseconds  
                '%Y-%m-%dT%H:%M:%S',       # ISO without Z
                '%Y-%m-%d %H:%M:%S UTC',   # BigQuery TIMESTAMP format
                '%Y-%m-%d %H:%M:%S',       # Standard datetime
                '%Y-%m-%d',                # Date only
                '%d/%m/%Y',                # DD/MM/YYYY
                '%m/%d/%Y',                # MM/DD/YYYY
                '%d-%m-%Y',                # DD-MM-YYYY (Sportivity format)
                '%d-%m-%Y %H:%M',          # DD-MM-YYYY HH:MM (Sportivity format)
            ]
            
            for fmt in formats_to_try:
                try:
                    dt = datetime.strptime(value, fmt)
                    
                    # For all date-only formats, use midnight Amsterdam time
                    if fmt in ['%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y']:
                        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        amsterdam_tz = get_amsterdam_timezone_for_date(dt)
                        dt = dt.replace(tzinfo=amsterdam_tz)
                        return int(dt.timestamp())
                        
                    elif fmt == '%d-%m-%Y %H:%M':
                        amsterdam_tz = get_amsterdam_timezone_for_date(dt)
                        dt = dt.replace(tzinfo=amsterdam_tz)
                        return int(dt.timestamp())

                    elif dt.tzinfo is None:
                        # For ISO datetime formats without timezone, assume UTC
                        dt = dt.replace(tzinfo=timezone.utc)
                    
                    return int(dt.timestamp())
                except ValueError:
                    continue
            
            # Try parsing timezone-aware datetime with fromisoformat
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return int(dt.timestamp())
            except ValueError:
                pass
                    
            # If no format works, try parsing as timestamp string
            try:
                return int(float(value))
            except ValueError:
                print(f"WARNING: Could not convert datetime '{value}' to epoch, returning original value")
                return value
        
        return value
        
    except Exception as e:
        print(f"Unexpected error converting datetime '{value}': {e}")
        return value


def add_customerio_legacy_compatibility(data):
    """Add legacy firstname/lastname fields for Customer.io compatibility"""
    if not isinstance(data, dict):
        return data
        
    # Create a copy to avoid modifying the original
    compatible_data = data.copy()
    
    # Add legacy name fields if standardized versions exist
    if 'first_name' in compatible_data and 'firstname' not in compatible_data:
        compatible_data['firstname'] = compatible_data['first_name']
    
    if 'last_name' in compatible_data and 'lastname' not in compatible_data:
        compatible_data['lastname'] = compatible_data['last_name']
    
    return compatible_data


def convert_payload_datetimes_to_epoch(data):
    """Recursively convert all datetime fields in payload to epoch timestamps"""
    if not isinstance(data, dict):
        return data
    
    # Fields that should be converted to epoch timestamps for Customer.io
    datetime_fields = {
        'datetime', 'created_at', 'updated_at', 'received_at', 'timestamp',
        'appointment_datetime', 'end_time', 'subscription_future', 'birth_date', 
        'start_date', 'end_date', 'submitted_at', 'dob', 'entry_date',
        'member_since', 'membership_start_date', 'membership_end_date',
        'subscription_start_date', 'subscription_end_date', 'joined_date',
        'registration_date', 'signup_date', 'enrollment_date',
        'access_end_date', 'payment_end_date', 'datetime_created', 'cancelled_per_date', 'contract_end_date',
        'acces_end_date',  # Typo version for backwards compatibility
        'last_visit',
        'next_appointment_at',
        'next_checkin_at',
        'followup_date',
        'last_visit_plus_14d',
        'last_visit_plus_21d',
        'last_visit_plus_28d',
        'last_visit_plus_42d',
        'last_visit_plus_56d',
        'last_visit_plus_70d',
    }
    
    converted_data = {}
    
    for key, value in data.items():
        if isinstance(value, dict):
            # Recursively convert nested dictionaries
            converted_data[key] = convert_payload_datetimes_to_epoch(value)
        elif isinstance(value, list):
            # Convert datetime fields in lists
            converted_data[key] = [
                convert_payload_datetimes_to_epoch(item) if isinstance(item, dict)
                else convert_datetime_to_epoch(item) if key in datetime_fields
                else item
                for item in value
            ]
        elif key in datetime_fields:
            # Convert datetime field to epoch
            converted_data[key] = convert_datetime_to_epoch(value)
        else:
            converted_data[key] = value
    
    return converted_data


def create_customer_data_for_unknown_customer(payload):
    """Create comprehensive customer data when is_known_customer = false"""
    customer_data = {}
    
    # Define field mappings from payload to Customer.io customer fields
    customer_field_mappings = {
        'email': 'email',
        'firstname': 'firstname',
        'first_name': 'firstname', 
        'lastname': 'lastname',
        'last_name': 'lastname',
        'brand': 'brand',
        'dob': 'dob',
        'birth_date': 'dob',
        'date_of_birth': 'dob',
        'gender': 'gender',
        'street': 'street',
        'zip': 'zip',
        'zipcode': 'zip',
        'postal_code': 'zip',
        'city': 'city',
        'ccname': 'ccname',
        'account_holder_name': 'ccname',
        'iban': 'iban',
        'phone_number': 'phone_number',
        'phone': 'phone_number',
        'pagename': 'pagename',
        'page_source': 'pagename'
    }
    
    # Extract customer data based on field mappings
    for payload_field, customer_field in customer_field_mappings.items():
        if payload_field in payload and payload[payload_field] is not None:
            value = payload[payload_field]
            # Skip empty strings
            if value != "":
                customer_data[customer_field] = value
    
    return customer_data


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(Exception)
)
def call_customerio_api(method, url, site_id, api_key, payload=None):
    """Make authenticated API call to Customer.io"""
    headers = {
        "Content-Type": "application/json"
    }
    
    # Customer.io Track API uses basic auth with site_id:api_key
    auth = (site_id, api_key)
    
    try:
        if method.upper() == "POST":
            resp = requests.post(url, headers=headers, auth=auth, json=payload)
        elif method.upper() == "PUT":
            resp = requests.put(url, headers=headers, auth=auth, json=payload)
        else:
            resp = requests.get(url, headers=headers, auth=auth)
        
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.exceptions.RequestException as e:
        print(f"Customer.io API error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.status_code} - {e.response.text}")
        raise


def extract_identifiers(envelope, payload):
    """Extract email and id identifiers from envelope and payload"""
    # Extract email from various sources
    email = (
        envelope.get("email") or
        payload.get("email") or 
        payload.get("Email") or
        payload.get("EMAIL")
    )
    
    # Extract id from various sources - but EXCLUDE email addresses
    id_value = (
        payload.get("id") or
        payload.get("Id") or 
        payload.get("ID") or
        payload.get("CustomerId") or
        payload.get("customer_id")
    )
    
    # If no ID found in payload, check envelope - but only if it's NOT an email
    if not id_value:
        envelope_customer_id = envelope.get("customer_id") or envelope.get("customerId")
        
        # Only use envelope customer_id if it's not an email address
        if envelope_customer_id and not is_email_address(envelope_customer_id):
            id_value = envelope_customer_id
    
    return email, str(id_value) if id_value else None


def is_email_address(value):
    """Check if a value looks like an email address"""
    if not value or not isinstance(value, str):
        return False
    
    # Simple email pattern check
    return "@" in value and "." in value.split("@")[-1]


def ensure_identifiers_in_payload(customer_data, email, id_value):
    """Ensure both email and id are present in customer data payload"""
    customer_data_with_identifiers = customer_data.copy()
    
    # Ensure email is in payload
    if email and 'email' not in customer_data_with_identifiers:
        customer_data_with_identifiers['email'] = email
    
    # Ensure id is in payload  
    if id_value and 'id' not in customer_data_with_identifiers:
        customer_data_with_identifiers['id'] = id_value
    
    return customer_data_with_identifiers


def create_or_update_customer(site_id, api_key, customer_data, identifier, status="update"):
    """Create or update a customer in Customer.io"""
    if not identifier:
        return False
    
    # Add legacy firstname/lastname compatibility for Customer.io
    customerio_compatible_data = add_customerio_legacy_compatibility(customer_data)
    
    # Convert datetime fields to epoch timestamps for Customer.io
    customerio_data = convert_payload_datetimes_to_epoch(customerio_compatible_data)
    
    # Remove any None values
    customerio_data = {k: v for k, v in customerio_data.items() if v is not None}
    
    url = f"{CUSTOMERIO_API_BASE}/customers/{identifier}"
    call_customerio_api("PUT", url, site_id, api_key, customerio_data)
    
    return {
        "action": f"customer_{status}",
        "identifier": identifier,
        "data": customerio_data
    }


def track_event(site_id, api_key, customer_id, event_name, event_data=None, timestamp=None):
    """Track an event for a customer in Customer.io"""
    if not customer_id:
        return False
    
    # Add legacy firstname/lastname compatibility for Customer.io
    compatible_event_data = add_customerio_legacy_compatibility(event_data or {})
    
    # Convert event data datetime fields to epoch
    converted_event_data = convert_payload_datetimes_to_epoch(compatible_event_data)
    
    payload = {
        "name": event_name,
        "data": converted_event_data
    }
    
    # Add timestamp if provided (convert to epoch if needed)
    if timestamp:
        epoch_timestamp = convert_datetime_to_epoch(timestamp)
        payload["timestamp"] = epoch_timestamp
    
    url = f"{CUSTOMERIO_API_BASE}/customers/{customer_id}/events"
    call_customerio_api("POST", url, site_id, api_key, payload)
    
    return {
        "action": "track_event",
        "customer_id": customer_id,
        "event_name": event_name,
        "event_data": converted_event_data,
        "timestamp": timestamp
    }


def extract_business_context(envelope):
    """Extract business context fields for Customer.io event data"""
    business_context = {}
    
    # Business context fields to include in Customer.io events
    business_fields = ['traffic_source', 'page_source', 'product_interest', 'campaign_source', 'brand']
    
    for field in business_fields:
        if field in envelope and envelope[field] is not None:
            business_context[field] = envelope[field]
    
    return business_context


def track_anonymous_event(site_id, api_key, email, event_name, event_data=None):
    """Track an event in Customer.io using email as identifier (for customers without a numeric ID)."""
    compatible = add_customerio_legacy_compatibility(event_data or {})
    converted = convert_payload_datetimes_to_epoch(compatible)
    payload = {"name": event_name, "email": email, "data": converted}
    url = f"{CUSTOMERIO_API_BASE}/events"
    call_customerio_api("POST", url, site_id, api_key, payload)
    return {"action": "track_anonymous_event", "email": email, "event_name": event_name, "event_data": converted}


def handle_task_followup_requested_event(envelope, site_id, api_key):
    """Forward task_followup_requested to Customer.io to trigger follow-up journeys."""
    payload = envelope.get("payload", {})
    customer_id = envelope.get("customer_id")
    email = envelope.get("email")

    event_data = {k: v for k, v in {
        "followup_date": payload.get("followup_date"),
        "task_type": payload.get("task_type"),
        "note": payload.get("note"),
        "original_task_doc_id": payload.get("original_task_doc_id"),
    }.items() if v is not None}

    log_json("TASK_FOLLOWUP_REQUESTED", {"customer_id": customer_id, "email": email, "event_data": event_data})

    if customer_id:
        return track_event(site_id, api_key, customer_id, "task_followup_requested", event_data)
    elif email:
        return track_anonymous_event(site_id, api_key, email, "task_followup_requested", event_data)
    else:
        print("task_followup_requested missing both customer_id and email")
        return None


def handle_customer_update_event(envelope, site_id, api_key):
    """Handle customer_update events - send ALL data to Customer.io as customer update"""
    payload = envelope.get("payload", {})
    status = payload.get("status", "update")
    
    # Extract identifiers
    email, id_value = extract_identifiers(envelope, payload)
    
    # Use id if available, otherwise email
    customer_identifier = id_value if id_value else email
    if not customer_identifier:
        print("Customer update missing both email and id identifiers")
        return None
    
    # Process payload - send ALL data without filtering
    customer_data_to_send = payload.copy()
    
    # Ensure both email and id are in the payload
    customer_data_with_identifiers = ensure_identifiers_in_payload(customer_data_to_send, email, id_value)
    
    log_json("CUSTOMER_UPDATE_WITH_STATUS", {
        "status": status,
        "customer_identifier": customer_identifier,
        "fields_count": len(customer_data_with_identifiers)
    })
    
    # Create/update customer - send ALL data
    return create_or_update_customer(
        site_id, api_key, customer_data_with_identifiers, 
        customer_identifier, status
    )


def handle_subscription_update_event(envelope, site_id, api_key):
    """Handle subscription_update events - create event only (no customer update)"""
    payload = envelope.get("payload", {})
    status = payload.get("status", "update")
    changed_fields = envelope.get("changed_fields", [])
    
    # Extract identifiers
    email, id_value = extract_identifiers(envelope, payload)
    
    # Use id if available, otherwise email
    customer_identifier = id_value if id_value else email
    if not customer_identifier:
        print("Subscription update missing both email and id identifiers")
        return None
    
    # Check if changed_fields is missing - this should trigger an error event
    if not isinstance(changed_fields, list):
        error_msg = f"Missing or invalid changed_fields in subscription_update event: {type(changed_fields)}"
        publish_error_event(error_msg, envelope)
        return None
    
    # Filter event payload to only changed fields
    filtered_event_payload = filter_payload_by_changed_fields(payload, changed_fields)
    
    log_json("SUBSCRIPTION_EVENT_PAYLOAD_FILTERED", {
        "status": status,
        "original_fields_count": len(payload),
        "changed_fields": changed_fields,
        "filtered_fields_count": len(filtered_event_payload),
        "filtered_fields": list(filtered_event_payload.keys())
    })
    
    if not filtered_event_payload:
        error_msg = f"Changed fields {changed_fields} not found in payload. Available fields: {list(payload.keys())}"
        publish_error_event(error_msg, envelope)
        # Continue with original payload for debugging
        filtered_event_payload = payload
    
    # Remove customer identifiers from event data
    event_data = {k: v for k, v in filtered_event_payload.items() if k not in ["CustomerId", "customer_id", "email", "customerId", "id", "Id", "ID"]}
    
    # Convert datetime fields to epoch
    event_data = convert_payload_datetimes_to_epoch(event_data)
    
    # Extract and add business context
    business_context = extract_business_context(envelope)
    
    # Add envelope metadata and business context
    event_data.update({
        "webhook_source": envelope.get("webhook_source"),
        "tenant_id": envelope.get("tenant_id"),
        "status": status,
        **business_context
    })
    
    # Remove None values
    event_data = {k: v for k, v in event_data.items() if v is not None}
    
    # Use timestamp from envelope if available
    timestamp = envelope.get("timestamp")
    
    # Event name remains subscription_update regardless of status
    event_name = "subscription_update"
    
    return track_event(site_id, api_key, customer_identifier, event_name, event_data, timestamp)


def handle_behavioral_event(envelope, site_id, api_key):
    """Handle behavioral events (visit, suspension, addon) with payload filtering for update events"""
    payload = envelope.get("payload", {})
    event_type = envelope.get("event_type", "unknown_event")
    status = payload.get("status", "update")
    changed_fields = envelope.get("changed_fields", [])
    
    # Extract identifiers
    email, id_value = extract_identifiers(envelope, payload)
    
    # For behavioral events, use email or id as customer identifier
    customer_id = email or id_value
    if not customer_id:
        print("Behavioral event missing identifiers")
        return None
    
    outputs = []
    
    # Check if this is an unknown customer (is_known_customer = false)
    is_known_customer = payload.get("is_known_customer", True)
    
    if not is_known_customer:
        # Create comprehensive customer data for unknown customer
        customer_data = create_customer_data_for_unknown_customer(payload)
        
        if customer_data:
            # Ensure both email and id are in the payload
            customer_data_with_identifiers = ensure_identifiers_in_payload(customer_data, email, id_value)
            
            # Create/update the customer with EMAIL as identifier and "new" status
            if email:
                customer_result = create_or_update_customer(site_id, api_key, customer_data_with_identifiers, email, "new")
                if customer_result:
                    outputs.append(customer_result)
    
    # Prepare event data for tracking
    event_data_payload = payload.copy()
    
    # For update events (determined by status), filter the event payload based on changed_fields
    is_update_event = status == "update"
    
    if is_update_event and changed_fields:
        # Check if changed_fields is missing for update events - this should trigger an error event
        if not isinstance(changed_fields, list):
            error_msg = f"Missing or invalid changed_fields in behavioral update event: {event_type}, type: {type(changed_fields)}"
            publish_error_event(error_msg, envelope)
            return None
        
        # SKIP PROCESSING if no fields changed for update events
        if len(changed_fields) == 0:
            log_json("BEHAVIORAL_UPDATE_SKIPPED", {
                "reason": "no_fields_changed",
                "changed_fields": changed_fields,
                "event_type": event_type,
                "status": status
            })
            return outputs if outputs else None
        
        # Filter event payload to only changed fields
        filtered_event_payload = filter_payload_by_changed_fields(event_data_payload, changed_fields)
        
        log_json("BEHAVIORAL_EVENT_PAYLOAD_FILTERED", {
            "event_type": event_type,
            "status": status,
            "original_fields_count": len(event_data_payload),
            "original_fields": list(event_data_payload.keys()),
            "changed_fields": changed_fields,
            "filtered_fields_count": len(filtered_event_payload),
            "filtered_fields": list(filtered_event_payload.keys())
        })
        
        if not filtered_event_payload:
            error_msg = f"Changed fields {changed_fields} not found in payload. Available fields: {list(event_data_payload.keys())}"
            publish_error_event(error_msg, envelope)
            return outputs if outputs else None
        else:
            event_data_payload = filtered_event_payload
    
    # Remove customer identifiers from event data
    event_data = {k: v for k, v in event_data_payload.items() if k not in ["CustomerId", "customer_id", "email", "customerId", "id", "Id", "ID"]}
    
    # Convert datetime fields to epoch
    event_data = convert_payload_datetimes_to_epoch(event_data)
    
    # Extract and add business context
    business_context = extract_business_context(envelope)
    
    # Add envelope metadata and business context
    event_data.update({
        "webhook_source": envelope.get("webhook_source"),
        "tenant_id": envelope.get("tenant_id"),
        "status": status,
        **business_context
    })
    
    # Remove None values
    event_data = {k: v for k, v in event_data.items() if v is not None}
    
    # Use timestamp from envelope if available
    timestamp = envelope.get("timestamp")
    
    # Generate event name based on event type and status  
    event_name = f"{event_type}_{status}" if status != "update" else event_type
    
    event_result = track_event(site_id, api_key, customer_id, event_name, event_data, timestamp)
    if event_result:
        outputs.append(event_result)
    
    return outputs


@functions_framework.cloud_event
def customerio_listener(cloud_event):
    """Main Cloud Function entry point"""
    try:
        # Extract Pub/Sub message from CloudEvent
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)
        
        log_json("INPUT", {"envelope": envelope, "payload": envelope.get("payload", {})})
        
        # Use standardized field names with proper fallbacks
        webhook_source = (envelope.get("webhook_source") or "").lower()
        event_type = (envelope.get("event_type") or "").lower()
        tenant_id = envelope.get("tenant_id")
        
        # Filter out events from Customer.io to avoid loops
        if webhook_source == "customerio":
            return
        
        if not tenant_id:
            return
        
        # Get Customer.io credentials using the tenant_id
        site_id, api_key = get_customerio_credentials(tenant_id)
        if not site_id or not api_key:
            return
        
        # Route to appropriate handler and collect outputs
        outputs = []
        
        # Handle customer_update events (with status: new/update)
        if event_type == "customer_update":
            result = handle_customer_update_event(envelope, site_id, api_key)
            if result:
                outputs.append(result)
        
        # Handle subscription_update events (with status: new/update)  
        elif event_type == "subscription_update":
            result = handle_subscription_update_event(envelope, site_id, api_key)
            if result:
                outputs.append(result)
        
        # Handle task follow-up events from slack-agent
        elif event_type == "task_followup_requested":
            result = handle_task_followup_requested_event(envelope, site_id, api_key)
            if result:
                outputs.append(result)

        # Handle behavioral events (visit, suspension, addon)
        elif event_type in ["visit", "suspension", "addon"]:
            result = handle_behavioral_event(envelope, site_id, api_key)
            if result:
                outputs.extend(result if isinstance(result, list) else [result])

        # Handle any other events as behavioral events
        else:
            result = handle_behavioral_event(envelope, site_id, api_key)
            if result:
                outputs.extend(result if isinstance(result, list) else [result])
        
        # Log all outputs
        for output in outputs:
            log_json("OUTPUT", output)
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        publish_error_event(str(e))
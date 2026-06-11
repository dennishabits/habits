import base64
import json
import os
import requests
from google.cloud import pubsub_v1, bigquery, firestore
from datetime import datetime

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT") or "solid-future-452906-a2"
TOPIC_NAME = "acuity-translations"

bigquery_client = bigquery.Client()
firestore_client = firestore.Client()


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def get_acuity_credentials(tenant_id):
    """Fetch Acuity API credentials from Firestore"""
    try:
        tenant_doc = firestore_client.collection('tenants').document(tenant_id).get()
        
        if not tenant_doc.exists:
            raise Exception(f"Tenant not found: {tenant_id}")
        
        tenant_data = tenant_doc.to_dict()
        acuity_config = tenant_data.get('acuityConfig', {})
        
        api_key = acuity_config.get('apiKey')
        user_id = acuity_config.get('userId')
        
        if not api_key or not user_id:
            raise Exception(f"Missing Acuity credentials for tenant {tenant_id}")
        
        return {'api_key': api_key, 'user_id': user_id}
        
    except Exception as e:
        print(f"❌ Credential error: {e}")
        raise


def fetch_acuity_data(appointment_id, credentials):
    """Fetch appointment data from Acuity API"""
    try:
        url = f"https://acuityscheduling.com/api/v1/appointments/{appointment_id}"
        auth = (credentials['user_id'], credentials['api_key'])
        headers = {'Accept': 'application/json'}
        
        response = requests.get(url, auth=auth, headers=headers, timeout=30)
        
        if response.status_code == 200:
            appointment_data = response.json()
            return appointment_data
            
        elif response.status_code == 404:
            raise Exception(f"Appointment {appointment_id} not found")
        elif response.status_code == 401:
            raise Exception("Acuity API authentication failed")
        elif response.status_code == 403:
            raise Exception(f"Acuity API plan limitation: {response.text}")
        else:
            raise Exception(f"Acuity API error: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        raise Exception(f"Acuity API request failed: {e}")
    except Exception as e:
        raise Exception(f"Acuity API error: {e}")


def normalize_phone(phone):
    """Remove non-digit characters from phone number"""
    if not phone:
        return None
    return ''.join(filter(str.isdigit, phone))


def lookup_customer_in_bigquery(email, phone, tenant_id):
    """Lookup customer in BigQuery by email/phone"""
    try:
        # Build query parameters
        query_params = [
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
        ]
        
        conditions = []
        
        # Add email condition
        if email and email.strip():
            conditions.append("email = @email")
            query_params.append(bigquery.ScalarQueryParameter("email", "STRING", email.strip().lower()))
        
        # Add phone condition
        clean_phone = normalize_phone(phone)
        if clean_phone:
            conditions.append("REGEXP_REPLACE(phone_number, r'[^0-9]', '') = @clean_phone")
            query_params.append(bigquery.ScalarQueryParameter("clean_phone", "STRING", clean_phone))
        
        if not conditions:
            return None
        
        # Build query
        where_clause = " OR ".join(conditions)
        query = f"""
        SELECT customer_id, email
        FROM `{PROJECT_ID}.gym_analytics.customers` 
        WHERE tenant_id = @tenant_id AND ({where_clause})
        LIMIT 1
        """
        
        # Execute query
        job_config = bigquery.QueryJobConfig(query_parameters=query_params)
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            return {
                "customer_id": row.customer_id,
                "email": row.email
            }
        
        return None
        
    except Exception as e:
        print(f"❌ BigQuery error: {e}")
        raise


def preserve_business_context(envelope):
    """Extract business context fields from original envelope"""
    business_fields = ['traffic_source', 'page_source', 'product_interest', 'campaign_source', 'brand']
    context = {}
    
    for field in business_fields:
        if field in envelope and envelope[field] is not None:
            context[field] = envelope[field]
    
    return context


def create_error_envelope(original_envelope, error_message):
    """Create error envelope that flows through normal translation pipeline"""
    tenant_id = original_envelope.get("tenant_id")
    timestamp = int(datetime.utcnow().timestamp())
    
    # Create error payload that acuity-translator can handle
    error_payload = {
        "id": f"error_{timestamp}",
        "type": "enrichment_error",
        "error_message": error_message,
        "service": "acuity-enricher",
        "original_appointment_id": original_envelope.get("payload", {}).get("id"),
        "enriched": False,
        "enrichedAt": timestamp
    }
    
    # Preserve business context and create envelope for acuity-translator
    business_context = preserve_business_context(original_envelope)
    
    error_envelope = {
        "webhook_source": "acuity",
        "tenant_id": tenant_id,
        "event_type": "error",
        "receivedAt": original_envelope.get("receivedAt"),
        "enrichedAt": timestamp,
        **business_context,
        "payload": error_payload
    }
    
    # Add email if available in original envelope
    if original_envelope.get("email"):
        error_envelope["email"] = original_envelope.get("email")
    
    return error_envelope


def acuity_enricher(event, context):
    """Main Cloud Function entry point"""
    try:
        # Step 1: Parse input
        raw = base64.b64decode(event["data"]).decode("utf-8")
        envelope = json.loads(raw)
        
        log_json("INPUT", envelope)
        
        # Basic validation
        webhook_source = envelope.get("webhook_source", "").lower()
        if webhook_source != "acuity":
            return
        
        # Extract key fields
        tenant_id = envelope.get("tenant_id")
        payload = envelope.get("payload", {})
        appointment_id = payload.get("id")
        
        if not tenant_id:
            raise Exception("Missing tenant_id")
        if not appointment_id:
            raise Exception("Missing appointment ID")
        
        # Initialize publisher
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(PROJECT_ID, TOPIC_NAME)
        
        try:
            # Step 2: Get Acuity credentials and fetch appointment data
            credentials = get_acuity_credentials(tenant_id)
            appointment_data = fetch_acuity_data(appointment_id, credentials)
            
            # Log Acuity API enrichment step
            email = appointment_data.get("email")
            phone = appointment_data.get("phone")
            log_json("ENRICHMENT_ACUITY_API", {
                "appointment_id": appointment_id,
                "success": True,
                "has_email": bool(email),
                "has_phone": bool(phone),
                "appointment_type": appointment_data.get("type"),
                "calendar": appointment_data.get("calendar")
            })
            
            # Step 3: Look up customer in BigQuery and determine what to send
            envelope_email = email  # Start with email from Acuity
            envelope_phone = phone  # Start with phone from Acuity
            envelope_customer_id = None
            lookup_method = None
            
            if email:
                # Email is present - lookup customer_id by email (and phone if available)
                customer_data = lookup_customer_in_bigquery(email, phone, tenant_id)
                lookup_method = "email" + ("+phone" if phone else "")
                if customer_data:
                    envelope_customer_id = customer_data.get("customer_id")
            elif phone:
                # No email but phone is present - lookup both customer_id and email by phone
                customer_data = lookup_customer_in_bigquery(None, phone, tenant_id)
                lookup_method = "phone_only"
                if customer_data:
                    envelope_customer_id = customer_data.get("customer_id")
                    envelope_email = customer_data.get("email")  # Get email from BigQuery
            else:
                customer_data = None
                lookup_method = "no_contact_info"
            
            # Log BigQuery lookup enrichment step
            log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
                "lookup_method": lookup_method,
                "customer_found": bool(customer_data),
                "customer_id": envelope_customer_id,
                "email_from_bigquery": envelope_email if customer_data and not email else None
            })
            
            # Step 4: Create enriched payload
            enriched_payload = appointment_data.copy()
            
            # Add customer information based on what we found
            if envelope_customer_id:
                enriched_payload.update({
                    "is_known_customer": True,
                    "gym_customer_id": envelope_customer_id,
                    "gym_customer_email": envelope_email  # This might be from Acuity or BigQuery
                })
            else:
                enriched_payload.update({
                    "is_known_customer": False,
                    "gym_customer_id": None,
                    "gym_customer_email": envelope_email  # From Acuity if available
                })
            
            # Add enrichment metadata
            enriched_payload.update({
                "enriched": True,
                "enrichedAt": int(datetime.utcnow().timestamp())
            })
            
            # Log final enrichment result
            enriched_fields = ["enriched", "enrichedAt"]
            if envelope_customer_id:
                enriched_fields.extend(["is_known_customer", "gym_customer_id", "gym_customer_email"])
            else:
                enriched_fields.extend(["is_known_customer"])
            
            log_json("ENRICHMENT_RESULT", {
                "is_known_customer": bool(envelope_customer_id),
                "enriched_fields": enriched_fields,
                "customer_id": envelope_customer_id,
                "final_email": envelope_email
            })
            
            # Step 5: Create enriched envelope
            business_context = preserve_business_context(envelope)
            
            enriched_envelope = {
                "webhook_source": envelope.get("webhook_source"),
                "tenant_id": tenant_id,
                "event_type": envelope.get("event_type"),
                "receivedAt": envelope.get("receivedAt"),
                "enrichedAt": int(datetime.utcnow().timestamp()),
                **business_context,
                "payload": enriched_payload
            }
            
            # Add fields based on what we found
            if envelope_customer_id:
                enriched_envelope["customer_id"] = envelope_customer_id
            
            if envelope_email:
                enriched_envelope["email"] = envelope_email
                
            if envelope_phone:
                enriched_envelope["phone"] = envelope_phone
            
            log_json("OUTPUT", enriched_envelope)
            
            # Step 6: Publish enriched event
            future = publisher.publish(topic_path, json.dumps(enriched_envelope).encode("utf-8"))
            message_id = future.result()
            
        except Exception as enrichment_error:
            # Log detailed error information
            print(f"❌ ENRICHMENT ERROR: {str(enrichment_error)}")
            print(f"❌ ERROR TYPE: {type(enrichment_error).__name__}")
            
            # Add stack trace for debugging
            import traceback
            print(f"❌ STACK TRACE: {traceback.format_exc()}")
            
            # Create error envelope and send through normal pipeline
            error_envelope = create_error_envelope(envelope, str(enrichment_error))
            log_json("ERROR_OUTPUT", error_envelope)
            
            # Publish to acuity-translations so acuity-translator can handle it
            future = publisher.publish(topic_path, json.dumps(error_envelope).encode("utf-8"))
            error_message_id = future.result()
            print(f"📤 Error envelope published to acuity-translations: {error_message_id}")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {str(e)}")
        print(f"❌ ERROR TYPE: {type(e).__name__}")
        import traceback
        print(f"❌ STACK TRACE: {traceback.format_exc()}")
        raise


if __name__ == "__main__":
    # Test with mock data
    test_event = {
        "data": base64.b64encode(json.dumps({
            "webhook_source": "acuity",
            "tenant_id": "test-tenant",
            "event_type": "appointment",
            "payload": {
                "id": 12345,
                "action": "scheduled"
            }
        }).encode()).decode()
    }
    
    acuity_enricher(test_event, None)

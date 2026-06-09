import base64
import json
import os
import functions_framework
from google.cloud import pubsub_v1, bigquery
from datetime import datetime, date
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT") or "solid-future-452906-a2"
FINAL_TOPIC_TRANSLATIONS = "leadform-translations"

bigquery_client = bigquery.Client()


def json_serializer(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def log_json(label, data):
    """Pretty print JSON data for logging with proper date handling"""
    print(f"{label}: {json.dumps(data, default=json_serializer)}")


def normalize_phone_number(phone_number):
    """Convert phone number to integer format by removing non-digits"""
    if not phone_number:
        return None
    
    # Remove all non-digit characters
    clean_phone = ''.join(filter(str.isdigit, phone_number))
    
    # Return as string to avoid leading zero issues
    return clean_phone if clean_phone else None


def lookup_customer_data(email, phone_number, tenant_id):
    """Lookup customer data in BigQuery by email or phone number"""
    try:
        # Normalize phone number for matching
        clean_phone = normalize_phone_number(phone_number)
        
        # Build query - first try email, then phone_number
        query_params = [
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
        ]
        
        conditions = []
        
        # Add email condition if available
        if email and email.strip():
            conditions.append("email = @email")
            query_params.append(bigquery.ScalarQueryParameter("email", "STRING", email.strip().lower()))
        
        # Add phone condition if available
        if clean_phone:
            conditions.append("REGEXP_REPLACE(phone_number, r'[^0-9]', '') = @clean_phone")
            query_params.append(bigquery.ScalarQueryParameter("clean_phone", "STRING", clean_phone))
        
        # If no email or phone, return no match
        if not conditions:
            return None
        
        # Build ORDER BY clause - only include conditions that exist
        order_conditions = []
        if email and email.strip():
            order_conditions.append("CASE WHEN email = @email THEN 1")
        if clean_phone:
            order_conditions.append("WHEN REGEXP_REPLACE(phone_number, r'[^0-9]', '') = @clean_phone THEN 2")
        
        order_by_clause = " ".join(order_conditions) + " ELSE 3 END"
        
        # Build final query - using correct column names from schema
        where_clause = " OR ".join(conditions)
        query = f"""
        SELECT 
            subscription_active,
            subscription_name,
            subscription_end_date,
            subscription_start_date,
            customer_id,
            tenant_id,
            email,
            phone_number,
            brand,
            member_since,
            subscription_cancelled,
            subscription_future,
            has_swimming
        FROM `{PROJECT_ID}.gym_analytics.customers` 
        WHERE tenant_id = @tenant_id
        AND ({where_clause})
        ORDER BY {order_by_clause}
        LIMIT 1
        """
        
        job_config = bigquery.QueryJobConfig(query_parameters=query_params)
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            customer_data = {
                "subscription_active": row.subscription_active,
                "subscription_name": row.subscription_name,
                "subscription_end_date": row.subscription_end_date.isoformat() if row.subscription_end_date else None,
                "subscription_start_date": row.subscription_start_date.isoformat() if row.subscription_start_date else None,
                "customer_id": row.customer_id,
                "tenant_id": row.tenant_id,
                "email": row.email,
                "phone_number": row.phone_number,
                "brand": row.brand,
                "member_since": row.member_since.isoformat() if row.member_since else None,
                "subscription_cancelled": row.subscription_cancelled,
                "subscription_future": row.subscription_future,
                "has_swimming": row.has_swimming
            }
            
            # Determine match method
            if email and email.strip().lower() == (row.email or "").lower():
                customer_data["match_method"] = "email"
            elif clean_phone and normalize_phone_number(row.phone_number) == clean_phone:
                customer_data["match_method"] = "phone_number"
            else:
                customer_data["match_method"] = "unknown"
            
            return customer_data
        
        return None
        
    except Exception as e:
        print(f"Error looking up customer data: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return None


def preserve_business_context(envelope):
    """Extract and preserve business context fields from original envelope"""
    business_context = {}
    
    # Business context fields to preserve (including pagename)
    business_fields = ['traffic_source', 'pagename', 'page_source', 'product_interest', 'campaign_source', 'brand']
    
    for field in business_fields:
        if field in envelope and envelope[field] is not None:
            business_context[field] = envelope[field]
    
    return business_context


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(Exception)
)
def publish_enriched_event(publisher, topic_path, message):
    """Publish enriched event to translations topic"""
    future = publisher.publish(topic_path, json.dumps(message, default=json_serializer).encode("utf-8"))
    return future.result()


def publish_error_event(publisher, project_id, error_message, original_envelope):
    """Publish error event to events topic"""
    try:
        events_topic_path = publisher.topic_path(project_id, "events")
        
        error_event = {
            "webhook_source": "leadform-enricher",
            "tenant_id": original_envelope.get("tenant_id"),
            "event_type": "error_occurred",
            "receivedAt": datetime.utcnow().isoformat(),
            "payload": {
                "event_type": "error_occurred",
                "service_name": "leadform-enricher",
                "error_description": f"**ERROR**: {error_message}",
                "email": "dennis@habits.fit",
                "original_event": original_envelope
            }
        }
        
        future = publisher.publish(events_topic_path, json.dumps(error_event, default=json_serializer).encode("utf-8"))
        future.result()
        print(f"Error event published successfully")
        
    except Exception as e:
        print(f"Failed to publish error event: {e}")


def enrich_leadform_event(envelope):
    """Enrich leadform event with customer data from BigQuery"""
    payload = envelope.get("payload", {})
    tenant_id = envelope.get("tenant_id")
    
    if not tenant_id:
        return None
    
    # Extract email and phone from payload
    email = payload.get("email")
    phone_number = payload.get("phone_number")
    
    # Lookup customer data
    customer_data = lookup_customer_data(email, phone_number, tenant_id)
    
    # Create enriched payload
    enriched_payload = payload.copy()
    
    if customer_data:
        # Customer found - add enrichment data
        enriched_payload.update({
            "is_known_customer": True,
            "subscription_active": customer_data.get("subscription_active"),
            "subscription_name": customer_data.get("subscription_name"),
            "subscription_end_date": customer_data.get("subscription_end_date"),
            "subscription_start_date": customer_data.get("subscription_start_date"),
            "customer_id": customer_data.get("customer_id"),
            "customer_match_method": customer_data.get("match_method"),
            "member_since": customer_data.get("member_since"),
            "subscription_cancelled": customer_data.get("subscription_cancelled"),
            "subscription_future": customer_data.get("subscription_future"),
            "has_swimming": customer_data.get("has_swimming"),
            "enriched": True,
            "enrichedAt": int(datetime.utcnow().timestamp())
        })
    else:
        # Customer not found - still mark as processed (this is normal, not an error)
        enriched_payload.update({
            "is_known_customer": False,
            "subscription_active": None,
            "subscription_name": None,
            "subscription_end_date": None,
            "subscription_start_date": None,
            "customer_id": None,
            "customer_match_method": None,
            "member_since": None,
            "subscription_cancelled": None,
            "subscription_future": None,
            "has_swimming": None,
            "enriched": True,
            "enrichedAt": int(datetime.utcnow().timestamp())
        })
    
    return enriched_payload


@functions_framework.cloud_event
def leadform_enricher(cloud_event):
    """Gen 2 Cloud Function entry point for Pub/Sub trigger"""
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
        
        # Use standardized field names with proper fallbacks
        webhook_source = (envelope.get("webhook_source") or "").lower()
        event_type = (envelope.get("event_type") or "").lower()
        tenant_id = envelope.get("tenant_id")
        
        # For backward compatibility, also check legacy fields
        if not webhook_source:
            webhook_source = (envelope.get("source") or "").lower()
        if not event_type:
            event_type = (envelope.get("eventType") or "").lower()
        if not tenant_id:
            tenant_id = envelope.get("tenantId")
        
        # Only process leadform events - skip all other sources
        if webhook_source != "leadform":
            return "OK"  # EXIT without publishing anything!
        
        # Extract contact info for enrichment
        payload = envelope.get("payload", {})
        email = payload.get("email")
        phone_number = payload.get("phone_number")
        
        # Log BigQuery lookup enrichment step
        customer_data = lookup_customer_data(email, phone_number, tenant_id)
        
        lookup_method = None
        if email and phone_number:
            lookup_method = "email+phone"
        elif email:
            lookup_method = "email_only"
        elif phone_number:
            lookup_method = "phone_only"
        else:
            lookup_method = "no_contact_info"
        
        log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
            "lookup_method": lookup_method,
            "customer_found": bool(customer_data),
            "customer_id": customer_data.get("customer_id") if customer_data else None,
            "match_method": customer_data.get("match_method") if customer_data else None,
            "subscription_active": customer_data.get("subscription_active") if customer_data else None
        })
        
        # Enrich with customer data from BigQuery
        enriched_payload = enrich_leadform_event(envelope)
        
        if enriched_payload is None:
            return "OK"
        
        # Log final enrichment result
        enriched_fields = ["enriched", "enrichedAt", "is_known_customer"]
        if customer_data:
            enriched_fields.extend(["customer_id", "customer_match_method", "subscription_active", "subscription_name"])
        
        log_json("ENRICHMENT_RESULT", {
            "is_known_customer": enriched_payload.get("is_known_customer"),
            "enriched_fields": enriched_fields,
            "customer_id": enriched_payload.get("customer_id"),
            "customer_match_method": enriched_payload.get("customer_match_method"),
            "subscription_active": enriched_payload.get("subscription_active")
        })
        
        # Preserve business context from original envelope
        business_context = preserve_business_context(envelope)
        
        # Create enriched envelope with standardized field names
        enriched_envelope = {
            # Technical/Routing Fields (standardized)
            "webhook_source": webhook_source,
            "tenant_id": tenant_id,
            "event_type": event_type,
            
            # System metadata
            "receivedAt": envelope.get("receivedAt"),
            "enrichedAt": int(datetime.utcnow().timestamp()),
            
            # Business context fields
            **business_context,
            
            # FIXED: Set customer_id from database (if found), email separately
            "customer_id": enriched_payload.get("customer_id"),  # Database customer_id or None
            "email": email,  # Always set email from payload
            
            # Payload with enriched data
            "payload": enriched_payload
        }
        
        # Convert envelope-level receivedAt to epoch if needed
        if "receivedAt" in enriched_envelope and enriched_envelope["receivedAt"]:
            if isinstance(enriched_envelope["receivedAt"], str):
                try:
                    dt = datetime.fromisoformat(enriched_envelope["receivedAt"].replace('Z', '+00:00'))
                    enriched_envelope["receivedAt"] = int(dt.timestamp())
                except:
                    pass  # Keep original value if conversion fails
        
        # Remove None values from envelope
        enriched_envelope = {k: v for k, v in enriched_envelope.items() if v is not None}
        
        log_json("TO_LEADFORM-TRANSLATIONS", {
            "envelope": enriched_envelope,
            "payload": enriched_envelope.get("payload", {})
        })
        
        # Publish to translations topic
        publisher = pubsub_v1.PublisherClient()
        translations_topic_path = publisher.topic_path(PROJECT_ID, FINAL_TOPIC_TRANSLATIONS)
        
        message_id = publish_enriched_event(publisher, translations_topic_path, enriched_envelope)
        
        return "OK"
        
    except Exception as e:
        print(f"Error processing enrichment: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Publish error event to events topic
        publisher = pubsub_v1.PublisherClient()
        publish_error_event(publisher, PROJECT_ID, f"Enrichment error: {e}", envelope)
        
        # In case of error, try to forward original event without enrichment
        try:
            translations_topic_path = publisher.topic_path(PROJECT_ID, FINAL_TOPIC_TRANSLATIONS)
            
            # Preserve business context for error case too
            business_context = preserve_business_context(envelope)
            
            # Add error information to original envelope
            error_payload = envelope.get("payload", {}).copy()
            error_payload.update({
                "is_known_customer": False,
                "subscription_active": None,
                "subscription_name": None,
                "subscription_end_date": None,
                "subscription_start_date": None,
                "customer_id": None,
                "customer_match_method": None,
                "member_since": None,
                "subscription_cancelled": None,
                "subscription_future": None,
                "has_swimming": None,
                "enriched": False,
                "enrichmentError": str(e),
                "enrichedAt": int(datetime.utcnow().timestamp())
            })
            
            error_envelope = {
                # Technical/Routing Fields (standardized)
                "webhook_source": (envelope.get("webhook_source") or envelope.get("source") or "leadform").lower(),
                "tenant_id": envelope.get("tenant_id") or envelope.get("tenantId"),
                "event_type": envelope.get("event_type") or envelope.get("eventType"),
                
                # System metadata
                "receivedAt": envelope.get("receivedAt"),
                "enrichedAt": int(datetime.utcnow().timestamp()),
                
                # Business context fields
                **business_context,
                
                # FIXED: Proper customer_id and email handling in error case
                "customer_id": None,  # No customer_id in error case
                "email": error_payload.get("email"),  # Set email properly
                
                # Payload with error info
                "payload": error_payload
            }
            
            # Remove None values from error envelope
            error_envelope = {k: v for k, v in error_envelope.items() if v is not None}
            
            log_json("ERROR_EVENT_OUTPUT", {
                "envelope": error_envelope,
                "payload": error_envelope.get("payload", {})
            })
            publish_enriched_event(publisher, translations_topic_path, error_envelope)
            
            return "OK"
            
        except Exception as forward_error:
            print(f"Failed to forward original event: {forward_error}")
            return "ERROR"
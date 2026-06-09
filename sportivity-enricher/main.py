import base64
import json
import os
import requests
import time
from datetime import datetime, timezone, timedelta
import functions_framework
from google.cloud import pubsub_v1, firestore, bigquery

PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
TOPIC_TRANSLATIONS = "sportivity-translations"
TOPIC_EVENTS = "events"

publisher = pubsub_v1.PublisherClient()
translations_path = publisher.topic_path(PROJECT_ID, TOPIC_TRANSLATIONS)
events_path = publisher.topic_path(PROJECT_ID, TOPIC_EVENTS)
firestore_client = firestore.Client()
bigquery_client = bigquery.Client()

def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")

def is_duplicate_event(event_type, entity_id, tenant_id, dedup_window_minutes=2):
    """Check if this event was already processed recently to prevent duplicate webhook processing
    
    Args:
        event_type: Type of event (subscription_new, customer_update, etc)
        entity_id: The business entity ID (MembershipID, CustomerId)
        tenant_id: The tenant ID
        dedup_window_minutes: How many minutes to check for duplicates (default 2)
    
    Returns:
        True if duplicate, False if not
    """
    try:
        # Create unique dedup key
        dedup_key = f"{tenant_id}_{event_type}_{entity_id}"
        doc_ref = firestore_client.collection("event_deduplication").document(dedup_key)
        doc = doc_ref.get()
        
        if doc.exists:
            doc_data = doc.to_dict()
            last_processed = doc_data.get("last_processed")
            
            if last_processed:
                # Check if within dedup window
                time_diff = datetime.now(timezone.utc) - last_processed
                if time_diff < timedelta(minutes=dedup_window_minutes):
                    print(f"DUPLICATE_DETECTED: {dedup_key} was processed {time_diff.total_seconds():.1f} seconds ago")
                    return True
        
        # Not a duplicate - record this processing
        doc_ref.set({
            "last_processed": datetime.now(timezone.utc),
            "event_type": event_type,
            "entity_id": str(entity_id),
            "tenant_id": tenant_id,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=dedup_window_minutes)
        })
        
        return False
        
    except Exception as e:
        print(f"Error checking duplicate (allowing processing to continue): {e}")
        # On error, allow processing to continue rather than block legitimate events
        return False

def publish_error_event(error_message, service_name="sportivity-enricher", original_envelope=None):
    """Publish error event to events topic with dennis@habits.fit email"""
    try:
        error_envelope = {
            "webhook_source": "sportivity",
            "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
            "event_type": "error",
            "received_at": datetime.utcnow().isoformat() + 'Z',
            "customer_id": "dennis@habits.fit",
            "email": "dennis@habits.fit",
            "event_display_name": "Service Error",
            "event_details": service_name,
            "event_secondary_details": "**ERROR**",
            "payload": {
                "error_message": f"**{error_message}**",
                "service": service_name,
                "original_event_type": original_envelope.get("event_type") if original_envelope else None
            }
        }
        
        error_envelope = {k: v for k, v in error_envelope.items() if v is not None}
        
        message_data = json.dumps(error_envelope).encode("utf-8")
        publisher.publish(events_path, message_data).result()
        print(f"ERROR_EVENT_PUBLISHED: {json.dumps(error_envelope, default=str)}")
        
    except Exception as publish_error:
        print(f"Failed to publish error event: {publish_error}")

def check_customer_exists_in_bq(customer_id, tenant_id):
    """Check if customer exists in BigQuery customers table"""
    try:
        if not customer_id or not tenant_id:
            print(f"DEBUG: Missing customer_id ({customer_id}) or tenant_id ({tenant_id}) for customer check")
            return False
        
        query = f"""
        SELECT COUNT(*) as count
        FROM `{PROJECT_ID}.gym_analytics.customers`
        WHERE customer_id = @customer_id 
        AND tenant_id = @tenant_id
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("customer_id", "STRING", str(customer_id)),
                bigquery.ScalarQueryParameter("tenant_id", "STRING", str(tenant_id))
            ]
        )
        
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            exists = row.count > 0
            print(f"DEBUG: BigQuery customer check - customer_id: {customer_id}, tenant_id: {tenant_id}, exists: {exists}")
            return exists
        
        return False
        
    except Exception as e:
        print(f"DEBUG: BigQuery customer check failed for customer_id {customer_id}, tenant_id {tenant_id}: {e}")
        # Default to new on error
        return False

def check_subscription_exists_in_bq(membership_id, tenant_id):
    """Check if subscription exists in BigQuery subscriptions table"""
    try:
        if not membership_id or not tenant_id:
            print(f"DEBUG: Missing membership_id ({membership_id}) or tenant_id ({tenant_id}) for subscription check")
            return False
        
        query = f"""
        SELECT COUNT(*) as count
        FROM `{PROJECT_ID}.gym_analytics.subscriptions`
        WHERE subscription_id = @subscription_id 
        AND tenant_id = @tenant_id
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("subscription_id", "STRING", str(membership_id)),
                bigquery.ScalarQueryParameter("tenant_id", "STRING", str(tenant_id))
            ]
        )
        
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            exists = row.count > 0
            print(f"DEBUG: BigQuery subscription check - subscription_id: {membership_id}, tenant_id: {tenant_id}, exists: {exists}")
            return exists
        
        return False
        
    except Exception as e:
        print(f"DEBUG: BigQuery subscription check failed for subscription_id {membership_id}, tenant_id {tenant_id}: {e}")
        # Default to new on error
        return False

def get_existing_subscription_cancelled_date(membership_id, tenant_id):
    """Get existing cancelled_per_date from BigQuery subscriptions table"""
    try:
        if not membership_id or not tenant_id:
            print(f"DEBUG: Missing membership_id ({membership_id}) or tenant_id ({tenant_id}) for cancelled date check")
            return None
        
        query = f"""
        SELECT cancelled_per_date
        FROM `{PROJECT_ID}.gym_analytics.subscriptions`
        WHERE subscription_id = @subscription_id 
        AND tenant_id = @tenant_id
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("subscription_id", "STRING", str(membership_id)),
                bigquery.ScalarQueryParameter("tenant_id", "STRING", str(tenant_id))
            ]
        )
        
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            existing_cancelled_per_date = row.cancelled_per_date
            print(f"DEBUG: Existing cancelled_per_date for subscription {membership_id}: {existing_cancelled_per_date}")
            return existing_cancelled_per_date
        
        # If no existing record found
        print(f"DEBUG: No existing subscription found for {membership_id}")
        return None
        
    except Exception as e:
        print(f"DEBUG: BigQuery cancelled date check failed for subscription_id {membership_id}, tenant_id {tenant_id}: {e}")
        return None

def convert_to_snake_case(data):
    """Convert dictionary keys from CamelCase to snake_case - REMOVED this function as enricher shouldn't translate"""
    # Enricher should NOT convert field names - that's the translator's job
    # Only ensure new fields we create are in snake_case
    return data

def determine_customer_status(customer_id, tenant_id, event_source):
    """Determine customer status based on BigQuery existence and event source"""
    if event_source == "refresh":
        return "refresh"
    elif event_source == "delete":  # Future functionality
        return "delete"
    else:
        exists = check_customer_exists_in_bq(customer_id, tenant_id)
        return "update" if exists else "new"

def determine_subscription_status(membership_id, tenant_id, event_source, api_data=None):
    """Determine subscription status based on BigQuery existence, event source, and cancellation check"""
    if event_source == "refresh":
        return "refresh"
    elif event_source == "delete":  # Future functionality
        return "delete"
    else:
        # Check if subscription exists
        exists = check_subscription_exists_in_bq(membership_id, tenant_id)
        
        if exists and api_data:
            # Extract cancelled_per_date from API data - check BOTH field name variants
            incoming_cancelled_per_date = (api_data.get("CancelledPerDate") or 
                                          api_data.get("CancelPerDate") or 
                                          api_data.get("cancelled_per_date"))
            
            print(f"DEBUG: Checking cancellation - membership_id: {membership_id}, incoming_cancelled_per_date: {incoming_cancelled_per_date}")
            
            # If incoming event has cancelled_per_date, check if it's a NEW cancellation
            if incoming_cancelled_per_date:
                existing_cancelled_per_date = get_existing_subscription_cancelled_date(membership_id, tenant_id)
                
                # Cancellation detected: subscription exists but has no cancelled_per_date yet
                if not existing_cancelled_per_date:
                    print(f"DEBUG: CANCELLATION DETECTED for subscription {membership_id} - existing: {existing_cancelled_per_date}, incoming: {incoming_cancelled_per_date}")
                    return "cancel"
                else:
                    print(f"DEBUG: Subscription {membership_id} already has cancellation date in BigQuery: {existing_cancelled_per_date}")
        
        return "update" if exists else "new"

def get_sportivity_token(webhook_token):
    try:
        if not webhook_token:
            return None
        doc_ref = firestore_client.collection("tenants").document(webhook_token)
        doc = doc_ref.get()
        if doc.exists:
            doc_data = doc.to_dict()
            return doc_data.get("sportivityToken")
    except Exception as e:
        print(f"Error fetching Sportivity token: {e}")
    return None

def extract_webhook_token(envelope):
    webhook_token = envelope.get("tenant_id")
    if webhook_token:
        return webhook_token
    payload = envelope.get("payload", {})
    return payload.get("token")

def call_sportivity_api_with_retry(url, token, max_retries=3):
    """Call Sportivity API with exponential backoff retry logic"""
    headers = {"accept": "application/json", "X-API-TOKEN": token, "Mem": "false"}
    
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404 and attempt < max_retries - 1:
                # Wait with exponential backoff: 1s, 2s, 4s
                wait_time = 2 ** attempt
                print(f"API call failed with 404, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print(f"Error calling Sportivity API (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return None
        except Exception as e:
            print(f"Error calling Sportivity API (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return None
    
    return None

def call_sportivity_membership_api_with_retry(url, token, max_retries=3):
    """Special function for individual membership API calls without Mem header, with retry logic"""
    headers = {"accept": "application/json", "X-API-TOKEN": token}
    
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404 and attempt < max_retries - 1:
                # Wait with exponential backoff: 1s, 2s, 4s
                wait_time = 2 ** attempt
                print(f"Membership API call failed with 404, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print(f"Error calling Sportivity Membership API (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return None
        except Exception as e:
            print(f"Error calling Sportivity Membership API (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return None
    
    return None

def parse_received_at_to_epoch(received_at):
    """Parse received_at timestamp from envelope to Unix epoch timestamp"""
    if not received_at:
        return int(datetime.now().timestamp())
    
    try:
        # If it's already a Unix timestamp
        if isinstance(received_at, (int, float)):
            return int(received_at)
        
        # If it's a string, try parsing
        if isinstance(received_at, str):
            # Try ISO format first
            try:
                dt = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                return int(dt.timestamp())
            except ValueError:
                pass
            
            # Try Unix timestamp as string
            try:
                return int(float(received_at))
            except ValueError:
                pass
    
    except Exception as e:
        print(f"Error parsing received_at '{received_at}': {e}")
    
    # Fallback to current time
    return int(datetime.now().timestamp())

def preserve_business_context(envelope):
    business_context = {}
    business_fields = ['traffic_source', 'page_source', 'product_interest', 'campaign_source', 'brand']
    for field in business_fields:
        if field in envelope and envelope[field] is not None:
            business_context[field] = envelope[field]
    return business_context

def publish_to_translations(payload):
    try:
        data = json.dumps(payload).encode("utf-8")
        publisher.publish(translations_path, data).result()
        log_json("TO_SPORTIVITY-TRANSLATIONS", {
            "envelope": payload,
            "payload": payload.get("payload", {})
        })
    except Exception as e:
        print(f"Publishing error: {e}")

def handle_visit_event(envelope, token):
    """Handle visit events by enriching with subscription information"""
    payload = envelope.get("payload", {})
    membership_id = payload.get("MembershipID")
    customer_id = payload.get("Customersid")
    
    if not token:
        publish_error_event("No Sportivity token found for visit event enrichment", "sportivity-enricher", envelope)
        return
        
    if not customer_id:
        publish_error_event("No Customersid found in visit event payload", "sportivity-enricher", envelope)
        return
    
    # Create enriched payload - start with original payload
    enriched_payload = payload.copy()
    subscription_name = "No Access"  # Default for visits without membership
    
    # Only try to enrich with subscription data if membership_id exists
    if membership_id:
        # Get membership/subscription details with retry
        url = f"https://www.sportivity.info/sportivity-api/Memberships/{membership_id}"
        api_data = call_sportivity_membership_api_with_retry(url, token)
        
        if api_data:
            log_json("ENRICHMENT_SPORTIVITY_MEMBERSHIP", api_data)
            
            # Extract subscription name from API data
            subscription_name = (api_data.get("Description") or 
                                api_data.get("MembershipDescription") or 
                                "Unknown Subscription")
        else:
            print(f"DEBUG: Failed to retrieve membership data for MembershipID: {membership_id}, using default subscription name")
            subscription_name = "Unknown Subscription"
    else:
        print(f"DEBUG: No MembershipID in visit event - this is a visit without membership access")
    
    # Add subscription_name to payload
    enriched_payload["subscription_name"] = subscription_name
    
    business_context = preserve_business_context(envelope)
    
    # Create enriched envelope for visit event
    enriched_envelope = {
        "webhook_source": envelope.get("webhook_source") or "sportivity",
        "tenant_id": envelope.get("tenant_id"),
        "event_type": "visit",
        "receivedAt": envelope.get("receivedAt"),
        "payload": enriched_payload,
        **business_context,
        "enrichedData": {
            "receivedAtEpoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
            "apiSource": "sportivity",
            "originalEventType": "visit",
            "membershipId": membership_id,
            "customerId": customer_id,
            "subscriptionName": subscription_name,
            "requiresChangeDetection": False,
            "hasAccess": bool(membership_id)
        }
    }
    
    publish_to_translations(enriched_envelope)

def handle_customer_event(envelope, token, event_source="webhook"):
    """Handle customer events - unified processing for all customer-related events"""
    payload = envelope.get("payload", {})
    customer_id = (payload.get("CustomerId") or payload.get("customerid") or 
                  payload.get("customer_id") or payload.get("Id") or payload.get("id"))
    tenant_id = envelope.get("tenant_id")
    
    # Check for duplicate customer event
    if customer_id and is_duplicate_event("customer_update", customer_id, tenant_id):
        print(f"SKIPPING DUPLICATE: customer_update event for customer {customer_id}")
        return
    
    if not token or not customer_id:
        publish_error_event(f"No Sportivity token or customer ID found for customer event", "sportivity-enricher", envelope)
        return
    
    # Get customer data from API with retry
    url = f"https://www.sportivity.info/sportivity-api/Customers/{customer_id}?Mem=true"
    api_data = call_sportivity_api_with_retry(url, token)
    
    if not api_data:
        publish_error_event(f"Failed to retrieve customer data for CustomerId: {customer_id} after retries", "sportivity-enricher", envelope)
        return
    
    log_json("ENRICHMENT_SPORTIVITY_CUSTOMER", api_data)
    
    # Keep original API field names, only add our new snake_case fields
    enriched_payload = api_data.copy()
    
    # Determine customer status
    status = determine_customer_status(customer_id, tenant_id, event_source)
    
    # Add status directly to payload (new field in snake_case)
    enriched_payload["status"] = status
    
    business_context = preserve_business_context(envelope)
    
    # Create consolidated customer_update event (envelope fields in snake_case)
    enriched_envelope = {
        "webhook_source": envelope.get("webhook_source") or "sportivity",
        "tenant_id": envelope.get("tenant_id"),
        "event_type": "customer_update",
        "received_at": envelope.get("receivedAt"),
        "payload": enriched_payload,
        **business_context,
        "enriched_data": {
            "received_at_epoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
            "api_source": "sportivity",
            "original_event_type": "customer_update",
            "customer_id": customer_id,
            "status": status,
            "event_source": event_source,
            "requires_change_detection": True
        }
    }
    
    publish_to_translations(enriched_envelope)
    
    # NEW: Process associated memberships as subscription events ONLY for customer_refresh
    if event_source == "refresh":
        print(f"DEBUG: Processing memberships for customer_refresh event for customer {customer_id}")
        
        # Extract membership IDs from customer data
        membership_ids = []
        if isinstance(api_data, dict):
            memberships_raw = (api_data.get("Memberships") or api_data.get("memberships") or 
                              api_data.get("CustomerMemberships") or api_data.get("customer_memberships") or [])
            
            if not memberships_raw and "Customer" in api_data:
                customer_info = api_data["Customer"]
                memberships_raw = (customer_info.get("Memberships") or customer_info.get("memberships") or [])
            
            # Extract membership IDs
            for membership in memberships_raw:
                membership_id = (membership.get('MembershipId') or membership.get('MembershipID') or 
                               membership.get('Id') or membership.get('id'))
                if membership_id:
                    membership_ids.append(membership_id)
        
        print(f"DEBUG: Found {len(membership_ids)} membership IDs for customer {customer_id}: {membership_ids}")
        
        # Process each membership as a subscription_update event
        for i, membership_id in enumerate(membership_ids):
            # Check for duplicate before processing
            if is_duplicate_event("subscription_update", membership_id, tenant_id):
                print(f"SKIPPING DUPLICATE: subscription_update event for membership {membership_id} (from customer_refresh)")
                continue
            
            membership_url = f"https://www.sportivity.info/sportivity-api/Memberships/{membership_id}"
            detailed_membership = call_sportivity_membership_api_with_retry(membership_url, token)
            
            if not detailed_membership:
                print(f"DEBUG: Failed to get detailed membership data for membership {membership_id} after retries")
                continue
                
            log_json("ENRICHMENT_SPORTIVITY_MEMBERSHIP", detailed_membership)
            
            # Create subscription data
            subscription_data = detailed_membership.copy()
            subscription_data["CustomerId"] = customer_id
            
            # Determine subscription status with cancellation detection
            subscription_status = determine_subscription_status(membership_id, tenant_id, event_source, detailed_membership)
            subscription_data["status"] = subscription_status
            
            # Create subscription_update event for this membership
            enriched_subscription_envelope = {
                "webhook_source": envelope.get("webhook_source") or "sportivity",
                "tenant_id": envelope.get("tenant_id"),
                "event_type": "subscription_update",
                "received_at": envelope.get("receivedAt"),
                "payload": subscription_data,
                **business_context,
                "enriched_data": {
                    "received_at_epoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
                    "api_source": "sportivity",
                    "original_event_type": "subscription_update",
                    "subscription_id": membership_id,
                    "customer_id": customer_id,
                    "status": subscription_status,
                    "event_source": event_source,
                    "triggered_by": "customer_refresh",
                    "membership_index": i,
                    "requires_change_detection": True
                }
            }
            
            publish_to_translations(enriched_subscription_envelope)
            print(f"DEBUG: Published subscription_update event for membership {membership_id}")

def handle_customer_bulk_event(envelope, token):
    """Handle customer_update_bulk events - fetch all updated customers"""
    if not token:
        publish_error_event("No Sportivity token found for customer_update_bulk event", "sportivity-enricher", envelope)
        return
    
    url = "https://www.sportivity.info/sportivity-api/CustomersUpdate"
    data = call_sportivity_api_with_retry(url, token)
    
    customers_data = []
    if data and isinstance(data, dict):
        raw_customers = data.get("GetCustomersUpdate", [])
        if isinstance(raw_customers, list):
            if len(raw_customers) == 1 and raw_customers[0] == "NoUpdates":
                customers_data = []
            else:
                customers_data = [item for item in raw_customers if isinstance(item, dict)]
    
    if not customers_data:
        print("No customer updates found from bulk API call")
        return
    
    log_json("ENRICHMENT_SPORTIVITY_CUSTOMERS", {"customers_update": customers_data})
    
    business_context = preserve_business_context(envelope)
    tenant_id = envelope.get("tenant_id")
    
    for i, customer_data in enumerate(customers_data):
        if not isinstance(customer_data, dict):
            continue
        
        bulk_customer_id = (customer_data.get("CustomerId") or 
                           customer_data.get("customerid") or 
                           customer_data.get("Id") or 
                           customer_data.get("id"))
        
        if not bulk_customer_id:
            continue
        
        # Check for duplicate before processing
        if is_duplicate_event("customer_update", bulk_customer_id, tenant_id):
            print(f"SKIPPING DUPLICATE: customer_update event for customer {bulk_customer_id} (from bulk)")
            continue
        
        # Get complete customer data from API with retry
        individual_url = f"https://www.sportivity.info/sportivity-api/Customers/{bulk_customer_id}?Mem=true"
        complete_customer_data = call_sportivity_api_with_retry(individual_url, token)
        
        if complete_customer_data:
            log_json("ENRICHMENT_SPORTIVITY_CUSTOMER", complete_customer_data)
            enriched_payload = complete_customer_data.copy()
        else:
            enriched_payload = customer_data.copy()
        
        # Determine status and add to payload (new field in snake_case)
        status = determine_customer_status(bulk_customer_id, tenant_id, "webhook")
        enriched_payload["status"] = status
        
        enriched_envelope = {
            "webhook_source": envelope.get("webhook_source") or "sportivity",
            "tenant_id": envelope.get("tenant_id"),
            "event_type": "customer_update",
            "received_at": envelope.get("receivedAt"),
            "payload": enriched_payload,
            **business_context,
            "enriched_data": {
                "received_at_epoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
                "api_source": "sportivity",
                "original_event_type": "customer_update",
                "customer_id": bulk_customer_id,
                "status": status,
                "event_source": "bulk_webhook",
                "bulk_update_index": i,
                "requires_change_detection": True
            }
        }
        
        publish_to_translations(enriched_envelope)

def handle_subscription_event(envelope, token, event_source="webhook"):
    """Handle subscription events - unified processing for all subscription-related events"""
    payload = envelope.get("payload", {})
    subscription_id = payload.get("MembershipID") or payload.get("MembershipId")
    customer_id = (payload.get("CustomerId") or payload.get("CustomerID") or payload.get("customer_id"))
    tenant_id = envelope.get("tenant_id")
    
    # Check for duplicate subscription event
    if subscription_id and is_duplicate_event("subscription_new", subscription_id, tenant_id):
        print(f"SKIPPING DUPLICATE: subscription_new event for membership {subscription_id}")
        return
    
    if not token:
        publish_error_event(f"No Sportivity token found for subscription event", "sportivity-enricher", envelope)
        return
    
    if not subscription_id:
        print(f"DEBUG: No MembershipID found in subscription event payload for customer {customer_id}, skipping")
        return
    
    # Get complete subscription data from API with retry
    url = f"https://www.sportivity.info/sportivity-api/Memberships/{subscription_id}"
    api_data = call_sportivity_membership_api_with_retry(url, token)
    
    if not api_data:
        publish_error_event(f"Failed to retrieve subscription data for MembershipID: {subscription_id} after retries", "sportivity-enricher", envelope)
        return
    
    log_json("ENRICHMENT_SPORTIVITY_SUBSCRIPTION", api_data)
    
    # Keep original API field names, only add our new snake_case fields
    enriched_payload = api_data.copy()
    
    # Determine subscription status with cancellation detection
    status = determine_subscription_status(subscription_id, tenant_id, event_source, api_data)
    
    # Add status directly to payload (new field in snake_case)
    enriched_payload["status"] = status
    
    # Ensure customer_id is preserved
    if customer_id:
        enriched_payload["CustomerId"] = customer_id
    elif api_data.get("CustomerId"):
        customer_id = api_data.get("CustomerId")
        enriched_payload["CustomerId"] = customer_id
    
    # NEW: Get customer email for subscription events (especially important for new subscriptions)
    customer_email = None
    if customer_id:
        customer_url = f"https://www.sportivity.info/sportivity-api/Customers/{customer_id}?Mem=false"
        customer_data = call_sportivity_api_with_retry(customer_url, token)
        
        if customer_data:
            customer_email = (customer_data.get("Email") or 
                            customer_data.get("email") or 
                            customer_data.get("EmailAddress"))
            
            if customer_email:
                # Add email to enriched payload
                enriched_payload["CustomerEmail"] = customer_email
                log_json("ENRICHMENT_CUSTOMER_EMAIL", {
                    "customer_id": customer_id,
                    "email": customer_email,
                    "subscription_id": subscription_id
                })
            else:
                print(f"WARNING: No email found in customer data for customer_id: {customer_id}")
        else:
            print(f"WARNING: Failed to retrieve customer data for customer_id: {customer_id}")
    else:
        print(f"WARNING: No customer_id available to fetch email for subscription {subscription_id}")
    
    business_context = preserve_business_context(envelope)
    
    # Create consolidated subscription_update event (envelope fields in snake_case)
    enriched_envelope = {
        "webhook_source": envelope.get("webhook_source") or "sportivity",
        "tenant_id": envelope.get("tenant_id"),
        "event_type": "subscription_update",
        "received_at": envelope.get("receivedAt"),
        "email": customer_email,  # Add email to envelope for slack-listener
        "payload": enriched_payload,
        **business_context,
        "enriched_data": {
            "received_at_epoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
            "api_source": "sportivity",
            "original_event_type": "subscription_update",
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "customer_email": customer_email,
            "status": status,
            "event_source": event_source,
            "requires_change_detection": True
        }
    }
    
    publish_to_translations(enriched_envelope)

def handle_subscription_bulk_event(envelope, token):
    """Handle subscription_update_bulk events - fetch all updated subscriptions"""
    if not token:
        publish_error_event("No Sportivity token found for subscription_update_bulk event", "sportivity-enricher", envelope)
        return
    
    url = "https://www.sportivity.info/sportivity-api/MembershipsUpdate"
    data = call_sportivity_api_with_retry(url, token)
    
    subscriptions_data = []
    if data and isinstance(data, dict):
        raw_subscriptions = data.get("GetMembershipsUpdate", [])
        if isinstance(raw_subscriptions, list):
            if len(raw_subscriptions) == 1 and raw_subscriptions[0] == "NoUpdates":
                subscriptions_data = []
            else:
                subscriptions_data = [item for item in raw_subscriptions if isinstance(item, dict)]
    
    if not subscriptions_data:
        print("No subscription updates found from bulk API call")
        return
    
    log_json("ENRICHMENT_SPORTIVITY_SUBSCRIPTIONS", {"subscriptions_update": subscriptions_data})
    
    business_context = preserve_business_context(envelope)
    tenant_id = envelope.get("tenant_id")
    
    for i, subscription_data in enumerate(subscriptions_data):
        if not isinstance(subscription_data, dict):
            continue
        
        bulk_subscription_id = (subscription_data.get("MembershipId") or 
                              subscription_data.get("MembershipID") or subscription_data.get("Id"))
        
        subscription_customer_id = (subscription_data.get("CustomerId") or
                                   subscription_data.get("CustomerID") or subscription_data.get("customer_id"))
        
        if not bulk_subscription_id or not subscription_customer_id:
            continue
        
        # Check for duplicate before processing
        if is_duplicate_event("subscription_update", bulk_subscription_id, tenant_id):
            print(f"SKIPPING DUPLICATE: subscription_update event for membership {bulk_subscription_id} (from bulk)")
            continue
        
        # Get complete subscription data from API with retry
        individual_url = f"https://www.sportivity.info/sportivity-api/Memberships/{bulk_subscription_id}"
        complete_subscription_data = call_sportivity_membership_api_with_retry(individual_url, token)
        
        if complete_subscription_data:
            log_json("ENRICHMENT_SPORTIVITY_SUBSCRIPTION", complete_subscription_data)
            enriched_payload = complete_subscription_data.copy()
        else:
            enriched_payload = subscription_data.copy()
        
        # Determine status with cancellation detection and add to payload (new field in snake_case)
        status = determine_subscription_status(bulk_subscription_id, tenant_id, "webhook", complete_subscription_data or subscription_data)
        enriched_payload["status"] = status
        enriched_payload["CustomerId"] = subscription_customer_id
        
        enriched_envelope = {
            "webhook_source": envelope.get("webhook_source") or "sportivity",
            "tenant_id": envelope.get("tenant_id"),
            "event_type": "subscription_update",
            "received_at": envelope.get("receivedAt"),
            "payload": enriched_payload,
            **business_context,
            "enriched_data": {
                "received_at_epoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
                "api_source": "sportivity",
                "original_event_type": "subscription_update",
                "subscription_id": bulk_subscription_id,
                "customer_id": subscription_customer_id,
                "status": status,
                "event_source": "bulk_webhook",
                "bulk_update_index": i,
                "requires_change_detection": True
            }
        }
        
        publish_to_translations(enriched_envelope)

def handle_suspension_event(envelope, token):
    """Handle suspension events by enriching with subscription information"""
    payload = envelope.get("payload", {})
    membership_id = payload.get("MembershipID")
    customer_id = payload.get("CustomerID")
    
    if not token:
        publish_error_event("No Sportivity token found for suspension event enrichment", "sportivity-enricher", envelope)
        return
        
    if not customer_id:
        publish_error_event("No CustomerID found in suspension event payload", "sportivity-enricher", envelope)
        return
    
    # Create enriched payload - start with original payload
    enriched_payload = payload.copy()
    subscription_name = None
    
    # Get subscription details if membership_id exists
    if membership_id:
        # Get membership/subscription details with retry
        url = f"https://www.sportivity.info/sportivity-api/Memberships/{membership_id}"
        api_data = call_sportivity_membership_api_with_retry(url, token)
        
        if api_data:
            log_json("ENRICHMENT_SPORTIVITY_MEMBERSHIP", api_data)
            
            # Extract subscription name from API data
            subscription_name = (api_data.get("Description") or 
                                api_data.get("MembershipDescription") or 
                                "Unknown Subscription")
        else:
            print(f"DEBUG: Failed to retrieve membership data for MembershipID: {membership_id}, subscription_name will be null")
            subscription_name = None
    else:
        print(f"DEBUG: No MembershipID in suspension event - cannot retrieve subscription name")
    
    # Add subscription_name to payload (can be None)
    enriched_payload["subscription_name"] = subscription_name
    
    # Add status field to indicate this is a suspension
    enriched_payload["status"] = "suspension"
    
    business_context = preserve_business_context(envelope)
    
    # Create enriched envelope for suspension event
    enriched_envelope = {
        "webhook_source": envelope.get("webhook_source") or "sportivity",
        "tenant_id": envelope.get("tenant_id"),
        "event_type": "suspension",
        "receivedAt": envelope.get("receivedAt"),
        "payload": enriched_payload,
        **business_context,
        "enrichedData": {
            "receivedAtEpoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
            "apiSource": "sportivity",
            "originalEventType": "suspension",
            "membershipId": membership_id,
            "customerId": customer_id,
            "subscriptionName": subscription_name,
            "requiresChangeDetection": False
        }
    }
    
    publish_to_translations(enriched_envelope)
    
def handle_addon_event(envelope, token):
    """Handle addon events - direct processing without enrichment"""
    payload = envelope.get("payload", {})
    business_context = preserve_business_context(envelope)
    
    # Pass through addon events directly - they already contain all needed data
    enriched_envelope = {
        "webhook_source": envelope.get("webhook_source") or "sportivity",
        "tenant_id": envelope.get("tenant_id"),
        "event_type": "addon",
        "receivedAt": envelope.get("receivedAt"),
        "payload": payload,
        **business_context,
        "enrichedData": {
            "receivedAtEpoch": parse_received_at_to_epoch(envelope.get("receivedAt")),
            "apiSource": "sportivity",
            "originalEventType": "addon",
            "requiresChangeDetection": False
        }
    }
    
    publish_to_translations(enriched_envelope)

@functions_framework.cloud_event
def sportivity_enricher(cloud_event):
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)
        
        log_json("INPUT", {"envelope": envelope, "payload": envelope.get("payload", {})})
        
        webhook_source = envelope.get("webhook_source", "").lower()
        event_type = envelope.get("event_type", "").lower()
        
        if webhook_source != "sportivity":
            return "OK"

        # Consolidated supported event types
        supported_event_types = {
            "customer_update_bulk", "customer_new", "customer_refresh",
            "subscription_update_bulk", "subscription_new", 
            "visit", "suspension", "addon"
        }
        
        if event_type not in supported_event_types:
            publish_error_event(f"Unsupported event type: {event_type}. Supported types: {supported_event_types}", "sportivity-enricher", envelope)
            return "OK"

        webhook_token = extract_webhook_token(envelope)
        if not webhook_token:
            print("DEBUG: No webhook token found in envelope")
            return "OK"

        sportivity_token = get_sportivity_token(webhook_token)
        if not sportivity_token:
            print(f"DEBUG: No sportivity token found for webhook_token: {webhook_token}")
            return "OK"
        
        print(f"DEBUG: About to dispatch to handler for event type: {event_type}")

        # Dispatch to consolidated handlers
        if event_type in ["customer_new", "customer_refresh"]:
            event_source = "refresh" if event_type == "customer_refresh" else "webhook"
            handle_customer_event(envelope, sportivity_token, event_source)
        elif event_type == "customer_update_bulk":
            handle_customer_bulk_event(envelope, sportivity_token)
        elif event_type == "subscription_new":
            handle_subscription_event(envelope, sportivity_token, "webhook")
        elif event_type == "subscription_update_bulk":
            handle_subscription_bulk_event(envelope, sportivity_token)
        elif event_type == "visit":
            handle_visit_event(envelope, sportivity_token)
        elif event_type == "suspension":
            handle_suspension_event(envelope, sportivity_token)
        elif event_type == "addon":
            handle_addon_event(envelope, sportivity_token)

        return "OK"
        
    except Exception as e:
        print(f"Error processing Pub/Sub message: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        
        # Try to get envelope info for error event
        try:
            envelope_for_error = json.loads(raw) if 'raw' in locals() else None
        except:
            envelope_for_error = None
            
        publish_error_event(f"Unexpected error in sportivity-enricher: {str(e)}", "sportivity-enricher", envelope_for_error)
        raise
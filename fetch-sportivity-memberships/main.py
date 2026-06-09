import json
import os
import requests
import time
from datetime import datetime
from google.cloud import pubsub_v1, firestore, bigquery
import functions_framework

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
TOPIC_ENRICHMENTS = "sportivity-enrichments"

publisher = pubsub_v1.PublisherClient()
enrichments_path = publisher.topic_path(PROJECT_ID, TOPIC_ENRICHMENTS)
firestore_client = firestore.Client()
bigquery_client = bigquery.Client()

# Global counters for reporting
stats = {
    "processed_tenants": 0,
    "processed_customers": 0,
    "customer_refresh_created": 0,
    "errors_created": 0,
    "api_calls_made": 0,
    "bigquery_queries": 0,
    "start_time": None,
    "current_tenant": None
}

def get_all_tenants():
    """Get all tenant configurations from Firestore"""
    try:
        tenants_ref = firestore_client.collection('tenants')
        docs = tenants_ref.stream()
        
        tenants = []
        for doc in docs:
            doc_data = doc.to_dict()
            if doc_data.get('sportivityToken'):
                tenants.append({
                    'tenant_id': doc.id,
                    'sportivity_token': doc_data.get('sportivityToken'),
                    'tenant_name': doc_data.get('tenantId', doc.id)
                })
        
        print(f"Found {len(tenants)} tenants with Sportivity tokens")
        return tenants
        
    except Exception as e:
        print(f"Error fetching tenants: {e}")
        return []

def get_customer_ids_from_bigquery(tenant_id):
    """Get all unique customer IDs for a tenant from BigQuery"""
    try:
        # Query the specific view table
        query = f"""
        SELECT customer_id 
        FROM `solid-future-452906-a2.gym_analytics.customers` 
        WHERE tenant_id = @tenant_id
        --AND email = 'dennis@basecampfitness.nl'
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id)
            ]
        )
        
        print(f"Querying BigQuery customers view for tenant: {tenant_id}")
        
        query_job = bigquery_client.query(query, job_config=job_config)
        results = query_job.result()
        
        customer_ids = []
        for row in results:
            if row.customer_id:
                customer_ids.append(row.customer_id)
        
        stats["bigquery_queries"] += 1
        print(f"Found {len(customer_ids)} unique customer IDs from BigQuery")
        return customer_ids
        
    except Exception as e:
        print(f"Error querying BigQuery customers view for tenant {tenant_id}: {e}")
        return []

def call_sportivity_api(url, token, retries=3):
    """Call Sportivity API with retry logic"""
    headers = {"accept": "application/json", "X-API-TOKEN": token, "Mem": "false"}
    
    for attempt in range(retries):
        try:
            print(f"API Call (attempt {attempt + 1}): {url}")
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            stats["api_calls_made"] += 1
            return resp.json()
            
        except requests.exceptions.RequestException as e:
            print(f"API call failed (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                print(f"API call failed after {retries} attempts")
                
    return None

def log_json(label, data):
    """Log JSON data in standardized format"""
    print(f"{label}: {json.dumps(data, default=str)}")

def process_tenant_customers(tenant):
    """Process all customers for a single tenant - create customer_refresh events only"""
    tenant_id = tenant['tenant_id']
    sportivity_token = tenant['sportivity_token']
    tenant_name = tenant['tenant_name']
    
    # Add INPUT logging for the tenant processing
    log_json("INPUT", {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "action": "bulk_customer_fetch",
        "service": "fetch-sportivity-memberships"
    })
    
    stats["current_tenant"] = tenant_name
    print(f"\nProcessing tenant: {tenant_name} (ID: {tenant_id})")
    
    # Get customer IDs from BigQuery
    customer_ids = get_customer_ids_from_bigquery(tenant_id)
    
    if not customer_ids:
        print(f"No customer IDs found in BigQuery for tenant {tenant_name}")
        return
    
    print(f"Found {len(customer_ids)} unique customers in BigQuery for tenant {tenant_name}")
    
    # Process each customer using Sportivity API
    for i, customer_id in enumerate(customer_ids, 1):
        print(f"   Customer {i}/{len(customer_ids)}: {customer_id}")
        
        # Get customer data from Sportivity API (without memberships)
        customer_url = f"https://www.sportivity.info/sportivity-api/Customers/{customer_id}?Mem=false"
        customer_data = call_sportivity_api(customer_url, sportivity_token)
        
        if not customer_data:
            print(f"   Failed to fetch customer {customer_id} from Sportivity API")
            stats["processed_customers"] += 1
            continue
        
        # Create customer_refresh event
        customer_envelope = {
            "webhook_source": "sportivity",
            "tenant_id": tenant_id,
            "event_type": "customer_refresh",
            "receivedAt": datetime.utcnow().isoformat() + "Z",
            "payload": customer_data,
            "enrichedData": {
                "receivedAtEpoch": int(datetime.utcnow().timestamp()),
                "apiSource": "sportivity",
                "originalEventType": "customer_refresh",
                "triggeredBy": "bulk_customer_fetch",
                "customerId": customer_id,
                "requiresChangeDetection": True
            }
        }
        
        try:
            data = json.dumps(customer_envelope).encode("utf-8")
            publisher.publish(enrichments_path, data).result()
            stats["customer_refresh_created"] += 1
            log_json("TO_SPORTIVITY-ENRICHMENTS", {
                "envelope": customer_envelope,
                "payload": customer_envelope.get("payload", {})
            })
            print(f"   Published customer_refresh event for customer {customer_id}")
        except Exception as e:
            print(f"   Failed to publish customer event: {e}")
            stats["errors_created"] += 1
        
        stats["processed_customers"] += 1
        
        # Rate limiting - small delay between customers
        time.sleep(0.1)
        
        # Progress update every 50 customers
        if i % 50 == 0:
            print(f"   Progress: {i}/{len(customer_ids)} customers processed")
    
    stats["processed_tenants"] += 1
    print(f"Completed tenant {tenant_name}: {len(customer_ids)} customers processed")

def print_progress_report():
    """Print current progress"""
    if stats["start_time"]:
        elapsed = datetime.now() - stats["start_time"]
        elapsed_str = str(elapsed).split('.')[0]  # Remove microseconds
    else:
        elapsed_str = "Unknown"
    
    print(f"\n=== PROGRESS REPORT ===")
    print(f"   Runtime: {elapsed_str}")
    print(f"   Current tenant: {stats['current_tenant'] or 'None'}")
    print(f"   Processed tenants: {stats['processed_tenants']}")
    print(f"   Processed customers: {stats['processed_customers']}")
    print(f"   Customer refresh created: {stats['customer_refresh_created']}")
    print(f"   Error events created: {stats['errors_created']}")
    print(f"   API calls made: {stats['api_calls_made']}")
    print(f"   BigQuery queries: {stats['bigquery_queries']}")
    print(f"========================\n")

@functions_framework.http
def fetch_sportivity_memberships(request):
    """
    HTTP Cloud Function to fetch Sportivity customer data only
    
    Usage:
    GET /health - Health check
    POST / - Start bulk customer refresh
    GET /status - Get current progress (if running)
    """
    
    if request.method == 'GET':
        path = request.path or '/'
        
        if path == '/health':
            return {
                "status": "healthy",
                "service": "fetch-sportivity-memberships",
                "version": "5.0.0"
            }
        elif path == '/status':
            return {
                "status": "ready" if not stats["start_time"] else "running",
                "stats": stats.copy()
            }
        else:
            return {
                "service": "Fetch Sportivity Customer Data",
                "description": "Fetches Sportivity customer data - enricher creates both customer_refresh and subscription_refresh events",
                "endpoints": {
                    "GET /health": "Health check",
                    "POST /": "Start bulk customer refresh",
                    "GET /status": "Get current status"
                },
                "process": [
                    "1. Query BigQuery gym_analytics.customers for customer IDs per tenant",
                    "2. Fetch customer data from Sportivity API (Mem=false)",
                    "3. Send customer data as customer_refresh to sportivity-enrichments",
                    "4. Enricher creates customer_refresh + subscription_refresh events"
                ],
                "events_created": [
                    "customer_refresh: Customer profile data (requiresChangeDetection: true)"
                ],
                "architecture": {
                    "responsibility": "Only creates customer_refresh events",
                    "delegation": "Enricher service creates both customer_refresh and subscription_refresh events",
                    "principle": "Single responsibility - fetch service focuses on data retrieval"
                },
                "usage": "POST to this endpoint to start the bulk customer refresh process"
            }
    
    if request.method != 'POST':
        return {"error": "Method not allowed"}, 405
    
    # Initialize stats
    stats.update({
        "processed_tenants": 0,
        "processed_customers": 0,
        "customer_refresh_created": 0,
        "errors_created": 0,
        "api_calls_made": 0,
        "bigquery_queries": 0,
        "start_time": datetime.now(),
        "current_tenant": None
    })
    
    print(f"Starting bulk Sportivity customer refresh at {stats['start_time']}")
    
    try:
        # Get all tenants
        tenants = get_all_tenants()
        if not tenants:
            return {"error": "No tenants found with Sportivity tokens"}, 400
        
        print(f"Will process {len(tenants)} tenants")
        
        # Process each tenant
        for i, tenant in enumerate(tenants, 1):
            print(f"\nProcessing tenant {i}/{len(tenants)}")
            process_tenant_customers(tenant)
            
            # Print progress every tenant
            print_progress_report()
            
            # Small delay between tenants to avoid rate limiting
            time.sleep(1)
        
        # Final report
        end_time = datetime.now()
        total_time = end_time - stats["start_time"]
        
        print(f"\n=== FETCH COMPLETED ===")
        print(f"   Total runtime: {str(total_time).split('.')[0]}")
        print(f"   Tenants processed: {stats['processed_tenants']}/{len(tenants)}")
        print(f"   Customers processed: {stats['processed_customers']}")
        print(f"   Customer refresh created: {stats['customer_refresh_created']}")
        print(f"   Error events created: {stats['errors_created']}")
        print(f"   API calls made: {stats['api_calls_made']}")
        print(f"   BigQuery queries: {stats['bigquery_queries']}")
        print(f"   Architecture: Enricher creates customer_refresh + subscription_refresh events")
        print(f"==============================")
        
        return {
            "status": "completed",
            "message": "Bulk Sportivity customer refresh completed - enricher creates both customer and subscription events",
            "summary": {
                "runtime": str(total_time).split('.')[0],
                "tenants_processed": stats['processed_tenants'],
                "customers_processed": stats['processed_customers'],
                "customer_refresh_created": stats['customer_refresh_created'],
                "errors_created": stats['errors_created'],
                "api_calls_made": stats['api_calls_made'],
                "bigquery_queries": stats['bigquery_queries']
            },
            "completed_at": end_time.isoformat(),
            "events_published": {
                "customer_refresh": stats['customer_refresh_created']
            },
            "architecture_note": "Fetch service creates customer_refresh events, enricher creates both customer_refresh + subscription_refresh events"
        }
        
    except Exception as e:
        print(f"Bulk fetch failed: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        
        return {
            "status": "failed",
            "error": str(e),
            "stats": stats.copy()
        }, 500
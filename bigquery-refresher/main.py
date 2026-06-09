import functions_framework
import json
import os
from datetime import datetime, timezone, timedelta
from google.cloud import bigquery, pubsub_v1

PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
DATASET_ID = "gym_analytics"
TABLE_ID = "customers"
EVENTS_TOPIC = "events"

def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")

def publish_error_event(error_msg, service_name="nightly-customer-sync"):
    """Publish error event to events topic"""
    try:
        publisher = pubsub_v1.PublisherClient()
        events_topic_path = publisher.topic_path(PROJECT_ID, EVENTS_TOPIC)
        
        error_event = {
            "webhook_source": "system",
            "tenant_id": "system",
            "event_type": "system_error",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "customer_id": "dennis@habits.fit",
            "email": "dennis@habits.fit",
            "event_display_name": "Nightly Sync Error",
            "event_details": service_name,
            "event_secondary_details": "BigQuery Sync Failed",
            "payload": {
                "service": service_name,
                "error_message": f"**{error_msg}**"
            }
        }
        
        message_data = json.dumps(error_event).encode('utf-8')
        future = publisher.publish(events_topic_path, message_data)
        future.result()
        
    except Exception as publish_error:
        print(f"Failed to publish error event: {publish_error}")

def convert_bigquery_row_to_dict(row):
    """Convert BigQuery row to dictionary with dynamic field handling"""
    customer_data = {}
    
    # Define excluded internal fields
    excluded_fields = {'last_updated', 'processed_at'}
    
    # Define known date fields for explicit handling
    known_date_fields = {'birth_date', 'member_since', 'subscription_start_date', 'subscription_end_date'}
    
    # Define known timestamp fields that should retain time component
    known_timestamp_fields = {
    'next_appointment_at', 'next_checkin_at', 'last_visit',
    'last_visit_plus_14d', 'last_visit_plus_21d', 'last_visit_plus_28d',
    'last_visit_plus_42d', 'last_visit_plus_56d', 'last_visit_plus_70d'
    }
    
    for key, value in row.items():
        # Skip excluded internal fields
        if key in excluded_fields:
            continue
            
        # Handle null values
        if value is None:
            customer_data[key] = None
            continue
            
        # Apply type-specific conversions
        if key in known_date_fields:
            customer_data[key] = value.strftime("%Y-%m-%d")
        elif key in known_timestamp_fields:
            # Retain full ISO timestamp including time component
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                customer_data[key] = value.isoformat()
            else:
                customer_data[key] = str(value)
        elif isinstance(value, datetime):
            customer_data[key] = value.strftime("%Y-%m-%d")
        elif hasattr(value, 'date') and callable(getattr(value, 'date')):
            customer_data[key] = value.strftime("%Y-%m-%d")
        elif isinstance(value, bool):
            customer_data[key] = bool(value)
        elif isinstance(value, (int, float)):
            customer_data[key] = value
        else:
            customer_data[key] = str(value)
    
    return customer_data

def create_customer_update_event(customer_data, sync_timestamp):
    """Create a customer_update event envelope with status=refresh"""
    # Extract required identifiers
    customer_id = customer_data.get('customer_id')
    email = customer_data.get('email')
    tenant_id = customer_data.get('tenant_id')
    
    if not customer_id or not tenant_id:
        print(f"Warning: Missing required identifiers - customer_id: {customer_id}, tenant_id: {tenant_id}")
        return None
    
    # Add status field to payload
    payload_with_status = customer_data.copy()
    payload_with_status['status'] = 'refresh'
    
    # Create the event envelope
    event_envelope = {
        "webhook_source": "system",
        "tenant_id": tenant_id,
        "event_type": "customer_update",
        "received_at": sync_timestamp,
        "customer_id": customer_id,
        "email": email,
        "event_display_name": "Customer Update",
        "event_details": f"Customer {customer_id}",
        "event_secondary_details": "Nightly Sync",
        "payload": payload_with_status
    }
    
    # Remove None values from envelope (but keep them in payload for completeness)
    event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
    
    return event_envelope

def batch_publish_events(publisher, topic_path, events, batch_size=100):
    """Publish events in batches with error handling"""
    total_events = len(events)
    published_count = 0
    failed_count = 0
    
    print(f"Publishing {total_events} events in batches of {batch_size}")
    
    for i in range(0, total_events, batch_size):
        batch = events[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total_events + batch_size - 1) // batch_size
        
        print(f"Processing batch {batch_num}/{total_batches} ({len(batch)} events)")
        
        batch_futures = []
        batch_events = []
        
        for event in batch:
            try:
                message_data = json.dumps(event).encode('utf-8')
                future = publisher.publish(topic_path, message_data)
                batch_futures.append(future)
                batch_events.append(event)
            except Exception as e:
                print(f"Failed to queue event for customer {event.get('customer_id', 'unknown')}: {e}")
                failed_count += 1
        
        # Wait for batch to complete
        for j, future in enumerate(batch_futures):
            try:
                message_id = future.result(timeout=30)
                published_count += 1
                
                if j == 0:  # Log first event of each batch
                    log_json("PUBLISHED_EVENT_SAMPLE", {
                        "message_id": message_id,
                        "customer_id": batch_events[j].get('customer_id'),
                        "tenant_id": batch_events[j].get('tenant_id'),
                        "event_type": batch_events[j].get('event_type'),
                        "batch": f"{batch_num}/{total_batches}"
                    })
                    
            except Exception as e:
                print(f"Failed to publish event for customer {batch_events[j].get('customer_id', 'unknown')}: {e}")
                failed_count += 1
    
    return published_count, failed_count

@functions_framework.http
def nightly_customer_sync(request):
    """HTTP Cloud Function for nightly customer sync from BigQuery to events topic - Only customers updated yesterday"""
    
    sync_start_time = datetime.now(timezone.utc)
    sync_timestamp = sync_start_time.isoformat()
    
    try:
        # Initialize clients
        bq_client = bigquery.Client(project=PROJECT_ID)
        publisher = pubsub_v1.PublisherClient()
        events_topic_path = publisher.topic_path(PROJECT_ID, EVENTS_TOPIC)
        
        print(f"Starting nightly customer sync at {sync_timestamp}")
        
        # Calculate yesterday's date range for filtering
        yesterday_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        yesterday_end = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999) - timedelta(days=1)
        
        print(f"Filtering customers updated between {yesterday_start.isoformat()} and {yesterday_end.isoformat()}")
        
        # Query only customers updated yesterday using last_updated field
        query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`
        --WHERE last_updated >= @yesterday_start 
        --AND last_updated <= @yesterday_end
        ORDER BY customer_id, tenant_id
        """
        
        # Configure query with parameters
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("yesterday_start", "TIMESTAMP", yesterday_start),
                bigquery.ScalarQueryParameter("yesterday_end", "TIMESTAMP", yesterday_end)
            ]
        )
        
        print("Executing BigQuery query to fetch customers updated yesterday...")
        query_job = bq_client.query(query, job_config=job_config)
        results = query_job.result()
        
        # Convert results to events
        events_to_publish = []
        customers_processed = 0
        tenants_found = set()
        
        print("Processing BigQuery results...")
        for row in results:
            try:
                # Convert BigQuery row to dictionary with dynamic field mapping
                customer_data = convert_bigquery_row_to_dict(row)
                
                # Track tenant for reporting
                tenant_id = customer_data.get('tenant_id')
                if tenant_id:
                    tenants_found.add(tenant_id)
                
                # Create customer_update event with status=refresh
                event = create_customer_update_event(customer_data, sync_timestamp)
                
                if event:
                    events_to_publish.append(event)
                    customers_processed += 1
                    
                    # Log progress every 500 customers
                    if customers_processed % 500 == 0:
                        print(f"Processed {customers_processed} customers...")
                
            except Exception as row_error:
                print(f"Error processing customer row: {row_error}")
                continue
        
        print(f"Processed {customers_processed} customers from {len(tenants_found)} tenants (updated yesterday)")
        log_json("SYNC_STATS", {
            "customers_processed": customers_processed,
            "tenants_found": len(tenants_found),
            "tenant_ids": list(tenants_found),
            "filter_period": f"{yesterday_start.isoformat()} to {yesterday_end.isoformat()}"
        })
        
        if not events_to_publish:
            print("No customers were updated yesterday - no events to publish")
            return {
                "status": "success", 
                "message": "No customers updated yesterday",
                "filter_period": f"{yesterday_start.isoformat()} to {yesterday_end.isoformat()}"
            }, 200
        
        # Log sample event structure for verification
        if events_to_publish:
            log_json("SAMPLE_EVENT_STRUCTURE", {
                "event_type": events_to_publish[0].get("event_type"),
                "payload_keys": list(events_to_publish[0].get("payload", {}).keys()),
                "status_field": events_to_publish[0].get("payload", {}).get("status"),
                "sample_customer_id": events_to_publish[0].get("customer_id")
            })
        
        # Publish events in batches
        print(f"Publishing {len(events_to_publish)} customer_update events...")
        published_count, failed_count = batch_publish_events(
            publisher, events_topic_path, events_to_publish
        )
        
        # Calculate duration
        sync_end_time = datetime.now(timezone.utc)
        duration = (sync_end_time - sync_start_time).total_seconds()
        
        # Final summary
        summary = {
            "status": "success" if failed_count == 0 else "partial_success",
            "sync_timestamp": sync_timestamp,
            "duration_seconds": duration,
            "customers_processed": customers_processed,
            "events_published": published_count,
            "events_failed": failed_count,
            "tenants_synced": len(tenants_found),
            "tenant_ids": list(tenants_found),
            "filter_period": f"{yesterday_start.isoformat()} to {yesterday_end.isoformat()}"
        }
        
        log_json("SYNC_COMPLETE", summary)
        
        if failed_count > 0:
            error_msg = f"Nightly sync completed with {failed_count} failures out of {customers_processed} customers (filtered to yesterday's updates)"
            publish_error_event(error_msg)
            return summary, 207  # Multi-status
        
        return summary, 200
        
    except Exception as e:
        error_msg = f"Nightly customer sync failed: {str(e)}"
        print(f"ERROR: {error_msg}")
        
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        publish_error_event(error_msg)
        
        return {
            "status": "error",
            "error": str(e),
            "sync_timestamp": sync_timestamp
        }, 500

# Cloud Scheduler entry point (can also be triggered via HTTP)
@functions_framework.cloud_event
def scheduled_customer_sync(cloud_event):
    """Cloud Scheduler entry point for nightly customer sync"""
    
    # Cloud Scheduler triggers this, but we'll call the main HTTP function
    # This allows the function to be triggered both by scheduler and manually via HTTP
    
    print("Triggered by Cloud Scheduler")
    
    try:
        # Create a mock request object for the HTTP function
        class MockRequest:
            def __init__(self):
                pass
        
        mock_request = MockRequest()
        result, status_code = nightly_customer_sync(mock_request)
        
        if status_code == 200:
            print("Scheduled sync completed successfully")
        else:
            print(f"Scheduled sync completed with status: {status_code}")
            
        return "OK"
        
    except Exception as e:
        print(f"Scheduled sync failed: {e}")
        publish_error_event(f"Scheduled customer sync failed: {str(e)}")
        raise

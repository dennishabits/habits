import base64
import json
import os
from datetime import datetime, timezone
from google.cloud import bigquery
from google.cloud.exceptions import NotFound
import functions_framework

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT") or "solid-future-452906-a2"
DATASET_ID = "gym_analytics"

# Initialize BigQuery client
bq_client = bigquery.Client()


def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")


def ensure_dataset_exists():
    """Create the BigQuery dataset if it doesn't exist"""
    dataset_ref = bq_client.dataset(DATASET_ID)
    try:
        bq_client.get_dataset(dataset_ref)
    except NotFound:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "europe-west1"
        dataset.description = "Gym analytics raw data"
        
        # Set data retention (1 year = 365 days)
        dataset.default_table_expiration_ms = 365 * 24 * 60 * 60 * 1000
        
        bq_client.create_dataset(dataset)


def get_raw_events_schema():
    """Schema for raw events - matches migrated table structure"""
    return [
        bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("tenant_id", "STRING"),
        bigquery.SchemaField("webhook_source", "STRING"),
        bigquery.SchemaField("event_type", "STRING"),
        bigquery.SchemaField("received_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("raw_payload", "JSON"),
        bigquery.SchemaField("customer_id", "STRING"),
        bigquery.SchemaField("email", "STRING"),
    ]


def create_table_if_not_exists(table_id, schema):
    """Create a BigQuery table if it doesn't exist"""
    table_ref = bq_client.dataset(DATASET_ID).table(table_id)
    try:
        bq_client.get_table(table_ref)
        return True
    except NotFound:
        # Table doesn't exist, create it
        table = bigquery.Table(table_ref, schema=schema)
        
        # Configure partitioning for performance
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="received_at"
        )
        table.clustering_fields = ["tenant_id", "webhook_source"]
        
        bq_client.create_table(table)
        return True


def generate_event_id(envelope):
    """Generate a unique event ID for deduplication"""
    import hashlib
    
    # Create ID from the entire envelope to ensure uniqueness
    envelope_str = json.dumps(envelope, sort_keys=True)
    return hashlib.md5(envelope_str.encode()).hexdigest()


def insert_raw_event(envelope):
    """Insert raw event with clean architecture"""
    
    # Extract standardized envelope fields with fallbacks
    tenant_id = envelope.get("tenant_id") or envelope.get("tenantId")
    webhook_source = envelope.get("webhook_source") or envelope.get("source")
    event_type = envelope.get("event_type") or envelope.get("eventType")
    customer_id = envelope.get("customer_id") or envelope.get("customerId")
    email = envelope.get("email")  # ADDED: Extract email field
    
    # Get the inner payload (business data only)
    payload = envelope.get("payload", {})
    
    # Generate current timestamp for when we received this event
    received_at_str = envelope.get("receivedAt")
    if received_at_str:
        try:
            # Try to parse the receivedAt timestamp from envelope
            if received_at_str.endswith('Z'):
                received_at = datetime.fromisoformat(received_at_str.replace('Z', '+00:00'))
            else:
                received_at = datetime.fromisoformat(received_at_str)
        except:
            # Fallback to current time if parsing fails
            received_at = datetime.now(timezone.utc)
    else:
        received_at = datetime.now(timezone.utc)
    
    # Create row with clean structure
    row = {
        "event_id": generate_event_id(envelope),
        "tenant_id": tenant_id,
        "webhook_source": webhook_source,
        "event_type": event_type,
        "received_at": received_at.strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
        "customer_id": str(customer_id) if customer_id else None,
        "email": email,  # ADDED: Include email field
        
        # Store only the inner payload (business data + any business context fields)
        "raw_payload": json.dumps(payload) if payload else None,
    }
    
    # Insert into BigQuery
    table_ref = bq_client.dataset(DATASET_ID).table("raw_events")
    errors = bq_client.insert_rows_json(table_ref, [row])
    
    if errors:
        print(f"❌ Error inserting raw event: {errors}")
        return None
    
    return row


def setup_bigquery_tables():
    """Initialize required BigQuery tables"""
    ensure_dataset_exists()
    success = create_table_if_not_exists("raw_events", get_raw_events_schema())
    
    if not success:
        raise Exception("Failed to setup BigQuery tables")


@functions_framework.cloud_event
def bigquery_listener(cloud_event):
    """Main Cloud Function entry point - clean raw data ingestion"""
    try:
        # Setup tables on first run
        setup_bigquery_tables()
        
        # Extract Pub/Sub message from CloudEvent
        message_data = cloud_event.data.get("message", {}).get("data")
        
        if not message_data:
            print("No message data found")
            return "OK"
        
        # Decode Pub/Sub message
        raw_message = base64.b64decode(message_data).decode("utf-8")
        envelope = json.loads(raw_message)
        
        log_json("INPUT", envelope)
        
        # Store raw event with clean architecture
        inserted_row = insert_raw_event(envelope)
        
        if inserted_row:
            log_json("OUTPUT", inserted_row)
        
        return "OK"
        
    except Exception as e:
        print(f"❌ Error processing Pub/Sub message: {e}")
        import traceback
        print(f"🐛 Full traceback: {traceback.format_exc()}")
        raise  # Re-raise to trigger Pub/Sub retry if needed
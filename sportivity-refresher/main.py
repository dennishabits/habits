import functions_framework
import base64
import json
from google.cloud import bigquery
from google.cloud import pubsub_v1
from datetime import datetime, timezone

def log_json(label, data):
    """Pretty print JSON data for logging"""
    print(f"{label}: {json.dumps(data, default=str)}")

def publish_error_event(error_msg, service_name, webhook_source=None, tenant_id=None, event_type=None):
    """Publish error event to events topic"""
    try:
        publisher = pubsub_v1.PublisherClient()
        events_topic_path = publisher.topic_path('solid-future-452906-a2', 'events')
        
        error_event = {
            "webhook_source": "system",
            "tenant_id": tenant_id or "unknown",
            "event_type": "system_error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "service": service_name,
                "error": f"**{error_msg}**",
                "context": {
                    "original_webhook_source": webhook_source,
                    "original_event_type": event_type
                },
                "recipient": "dennis@habits.fit"
            }
        }
        
        message_data = json.dumps(error_event).encode('utf-8')
        future = publisher.publish(events_topic_path, message_data)
        future.result()
        
    except Exception as publish_error:
        print(f"Failed to publish error event: {publish_error}")

@functions_framework.cloud_event
def handle_event(cloud_event):
    """Gen 2 Pub/Sub function - REFRESH LOGIC DISABLED
    
    This service previously created customer_refresh events for 'omzetting' scenarios,
    but this is now redundant since the consolidated enricher properly handles
    subscription_new webhooks with BigQuery existence checking.
    
    The service is kept for potential future refresh functionality but currently
    only logs events without processing them.
    """
    try:
        # Extract Pub/Sub message from CloudEvent
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        parsed_event = json.loads(raw)
        
        log_json("INPUT", parsed_event)
        
        # Extract basic event information for logging
        event_type = parsed_event.get('event_type') or parsed_event.get('eventType') or parsed_event.get('type')
        webhook_source = parsed_event.get('webhook_source') or parsed_event.get('source')
        tenant_id = parsed_event.get('tenant_id') or parsed_event.get('tenantId')
        
        customer_id = (parsed_event.get('customerId') or 
                      parsed_event.get('CustomerId') or
                      parsed_event.get('payload', {}).get('CustomerId') or
                      parsed_event.get('payload', {}).get('customerId') or
                      parsed_event.get('customer_id'))
        
        # Log the event but don't process it
        log_json("EVENT_RECEIVED_NOT_PROCESSED", {
            "event_type": event_type,
            "webhook_source": webhook_source,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "reason": "refresh_logic_disabled",
            "note": "Consolidated enricher now handles omzetting scenarios with normal webhooks"
        })
        
        # REFRESH LOGIC REMOVED:
        # The previous logic that created customer_refresh events has been removed
        # because the consolidated enricher now properly handles:
        # 1. subscription_new webhooks with BigQuery existence checking
        # 2. Automatic determination of subscription_new vs subscription_refresh
        # 3. Complete customer data refresh when needed
        #
        # This eliminates duplicate processing and redundant customer_refresh events
        
    except Exception as e:
        error_msg = f"Unexpected error in sportivity-refresher: {str(e)}"
        print(f"❌ ERROR: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Try to extract context for error event
        webhook_source = None
        tenant_id = None 
        event_type = None
        
        try:
            if 'parsed_event' in locals():
                webhook_source = parsed_event.get('webhook_source') or parsed_event.get('source')
                tenant_id = parsed_event.get('tenant_id') or parsed_event.get('tenantId')
                event_type = parsed_event.get('event_type') or parsed_event.get('eventType') or parsed_event.get('type')
        except:
            pass
            
        publish_error_event(error_msg, "sportivity-refresher", webhook_source, tenant_id, event_type)
    
    return 'OK'
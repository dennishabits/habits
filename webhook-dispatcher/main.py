import os
import json
import logging
from datetime import datetime
import uuid
import functions_framework
from urllib.parse import parse_qs

from google.cloud import pubsub_v1, firestore

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Configuration
PROJECT_ID = os.environ.get("PROJECT_ID", "solid-future-452906-a2")

# Initialize services
firestore_client = firestore.Client()

# Initialize PubSub publisher
publisher = pubsub_v1.PublisherClient()

# Simple metrics storage
class Metrics:
    def __init__(self):
        self.webhooks_received = 0
        self.events_published = 0
        self.events_skipped = 0
        self.errors = 0

metrics = Metrics()

def publish_error_event(error_message, service_name, webhook_source=None, tenant_id=None, original_event_type=None, customer_id=None):
    """Publish error event to events topic"""
    try:
        error_event = {
            "event_type": "error",
            "webhook_source": webhook_source or "unknown",
            "tenant_id": tenant_id or "unknown",
            "payload": {
                "service": service_name,
                "error_message": f"**{error_message}**",
                "original_event_type": original_event_type,
                "customer_id": customer_id,
                "email": "dennis@habits.fit"
            },
            "receivedAt": datetime.utcnow().isoformat(),
            "event_id": str(uuid.uuid4())
        }
        
        topic_path = publisher.topic_path(PROJECT_ID, "events")
        message_json = json.dumps(error_event)
        message_bytes = message_json.encode('utf-8')
        
        attributes = {
            "event_type": "error",
            "webhook_source": webhook_source or "unknown",
            "tenant_id": tenant_id or "unknown"
        }
        
        logger.info(f"TO_EVENTS: {json.dumps(error_event)}")
        future = publisher.publish(topic_path, message_bytes, **attributes)
        future.result(timeout=30)
        
    except Exception as e:
        logger.error(f"❌ Failed to publish error event: {e}")

def determine_event_routing(webhook_source, webhook_data, event_type_param=None):
    """
    Determine event name and target topic based on webhook_source and content.
    Returns None if event should not be posted.
    """
    
    if webhook_source == 'acuity':
        if event_type_param:
            event_name = event_type_param
        else:
            event_name = 'acuity_webhook'
        
        return {
            'event_name': event_name,
            'topic': 'acuity-enrichments',
            'description': f'Acuity webhook - event type: {event_name}'
        }
    
    elif webhook_source == 'sportivity':
        logger.info(f"DEBUG: Processing Sportivity webhook, eventType={webhook_data.get('eventType')}, type={webhook_data.get('type')}")
        
        event_types = webhook_data.get('eventType', [])
        if isinstance(event_types, str):
            event_types = [event_types]
        
        customer_id = webhook_data.get('CustomerId') or webhook_data.get('CustomerID')
        
        if event_types:
            event_name = event_types[0]
            
            sportivity_event_mapping = {
                'CustomersUpdate': 'customer_update_bulk',
                'CustomerUpdate': 'customer_update_bulk',
                'CustomersNew': 'customer_new',
                'CustomerNew': 'customer_new',
                'MembershipUpdate': 'subscription_update_bulk', 
                'MembershipNew': 'subscription_new'
            }
            
            if event_name in sportivity_event_mapping:
                standardized_event_name = sportivity_event_mapping[event_name]
                return {
                    'event_name': standardized_event_name,
                    'topic': 'sportivity-enrichments',
                    'description': f'Sportivity webhook - event type: {standardized_event_name} (from {event_name})'
                }
            else:
                return {
                    'event_name': 'error',
                    'topic': 'events',
                    'description': f'Unrecognized Sportivity event type: {event_name}',
                    'error_details': {
                        'original_event_type': event_name,
                        'error_message': f'Unrecognized Sportivity event type: {event_name}',
                        'customer_id': customer_id
                    }
                }
        else:
            if webhook_data.get('type'):
                event_name = webhook_data['type']
                
                sportivity_event_mapping = {
                    'CustomersUpdate': 'customer_update_bulk',
                    'CustomerUpdate': 'customer_update_bulk',
                    'CustomersNew': 'customer_new',
                    'CustomerNew': 'customer_new',
                    'MembershipUpdate': 'subscription_update_bulk',
                    'MembershipNew': 'subscription_new'
                }
                
                logger.info(f"DEBUG: event_name='{event_name}', available_mappings={list(sportivity_event_mapping.keys())}")
                
                if event_name in sportivity_event_mapping:
                    standardized_event_name = sportivity_event_mapping[event_name]
                    return {
                        'event_name': standardized_event_name,
                        'topic': 'sportivity-enrichments', 
                        'description': f'Sportivity webhook - type field: {standardized_event_name} (from {event_name})'
                    }
                else:
                    return {
                        'event_name': 'error',
                        'topic': 'events',
                        'description': f'Unrecognized Sportivity type: {event_name}',
                        'error_details': {
                            'original_event_type': event_name,
                            'error_message': f'Unrecognized Sportivity type field: {event_name}',
                            'customer_id': customer_id
                        }
                    }
            
            elif webhook_data.get("EntryDate") and webhook_data.get("Customersid") and webhook_data.get("Gate"):
                event_name = "visit"
            
            elif webhook_data.get("BlockageID") and webhook_data.get("CustomerID") and webhook_data.get("MembershipID"):
                event_name = "suspension"
            
            elif webhook_data.get("MembershipId") and webhook_data.get("CustomerId") and webhook_data.get("Addonid"):
                event_name = "addon"
            
            else:
                return {
                    'event_name': 'error',
                    'topic': 'events',
                    'description': 'Unrecognized Sportivity webhook payload structure',
                    'error_details': {
                        'original_event_type': 'unknown',
                        'error_message': 'Could not identify Sportivity event type from payload structure',
                        'payload_keys': list(webhook_data.keys()) if webhook_data else [],
                        'customer_id': customer_id
                    }
                }
            
            return {
                'event_name': event_name,
                'topic': 'sportivity-enrichments',
                'description': f'Sportivity webhook - detected as: {event_name}'
            }
    
    elif webhook_source == 'customerio':
        if event_type_param == 'crm_task':
            return {
                'event_name': 'crm_task',
                'topic': 'crm-translations',
                'description': 'CRM task from Customer.io journey - to translations for processing'
            }
        
        if event_type_param:
            event_name = event_type_param
        else:
            customerio_metric = webhook_data.get('metric')
            customerio_object_type = webhook_data.get('object_type')
            
            if customerio_object_type not in ['email', 'sms']:
                return None
            
            if customerio_metric not in ['opened', 'clicked', 'bounced']:
                return None
            
            event_name = f"{customerio_object_type}_{customerio_metric}"
        
        return {
            'event_name': event_name,
            'topic': 'customerio-translations',
            'description': f'Customer.io webhook - {event_name} to translations for cleanup'
        }
    
    elif webhook_source == 'slack':
        # Route lead_call_interaction to dedicated topic
        if event_type_param == 'interactive':
            return {
                'event_name': 'lead_call_interaction',
                'topic': 'slack-interactions',
                'description': 'Slack button interaction for lead call task'
            }

        # All other Slack events go to slack-translations
        if event_type_param:
            event_name = event_type_param
        else:
            slack_type = webhook_data.get('type')
            slack_event = webhook_data.get('event', {})
            
            if slack_type == 'event_callback' and isinstance(slack_event, dict):
                event_name = slack_event.get('type', 'slack_event')
            elif slack_type:
                event_name = slack_type
            else:
                event_name = 'slack_event'
        
        return {
            'event_name': event_name,
            'topic': 'slack-translations',
            'description': f'Slack webhook - to translations for cleanup as {event_name}'
        }
    
    elif webhook_source == 'leadform':
        if event_type_param:
            event_name = event_type_param
        else:
            if webhook_data.get('orderId') or webhook_data.get('order_id') or webhook_data.get('order_number'):
                event_name = 'order'
            elif webhook_data.get('subject') or webhook_data.get('message') or webhook_data.get('inquiry'):
                event_name = 'contactform_submitted'
            elif webhook_data.get('email'):
                event_name = 'lead_submitted'
            else:
                return None
        
        return {
            'event_name': event_name,
            'topic': 'leadform-enrichments',
            'description': f'Leadform webhook - to enrichments as {event_name}'
        }
    
    else:
        return None

def extract_business_context_fields(webhook_data, webhook_source=None):
    """
    Extract business context fields from webhook payload.
    Maps legacy field names to standardized names.
    """
    business_fields = {}
    
    if webhook_source == 'customerio':
        customerio_object_type = webhook_data.get('object_type')
        
        if customerio_object_type:
            business_fields['traffic_source'] = customerio_object_type
        
        customerio_data = webhook_data.get('data', {})
        
        if customerio_data.get('broadcast_id'):
            business_fields['campaign_source'] = f"broadcast_{customerio_data['broadcast_id']}"
        elif customerio_data.get('journey_id'):
            business_fields['campaign_source'] = f"journey_{customerio_data['journey_id']}"
        
        for potential_field in ['campaign', 'source', 'medium', 'utm_campaign', 'utm_source', 'utm_medium']:
            if customerio_data.get(potential_field):
                if 'campaign' in potential_field:
                    business_fields['campaign_source'] = customerio_data[potential_field]
                elif 'source' in potential_field or 'medium' in potential_field:
                    business_fields['traffic_source'] = customerio_data[potential_field]
    else:
        if webhook_data.get('source'):
            business_fields['traffic_source'] = webhook_data['source']
        elif webhook_data.get('traffic_source'):
            business_fields['traffic_source'] = webhook_data['traffic_source']
        
        if webhook_data.get('pagename'):
            business_fields['pagename'] = webhook_data['pagename']
        elif webhook_data.get('page_source'):
            business_fields['page_source'] = webhook_data['page_source']
        elif webhook_data.get('page'):
            business_fields['page'] = webhook_data['page']
        
        if webhook_data.get('product'):
            business_fields['product_interest'] = webhook_data['product']
        elif webhook_data.get('product_interest'):
            business_fields['product_interest'] = webhook_data['product_interest']
        
        if webhook_data.get('campaign'):
            business_fields['campaign_source'] = webhook_data['campaign']
        elif webhook_data.get('campaign_source'):
            business_fields['campaign_source'] = webhook_data['campaign_source']
        
        if webhook_data.get('brand'):
            business_fields['brand'] = webhook_data['brand']
    
    business_fields = {k: v for k, v in business_fields.items() if v is not None}
    
    return business_fields

def publish_to_topic(topic_name, event_message):
    """Publish event message to specific PubSub topic"""
    try:
        topic_path = publisher.topic_path(PROJECT_ID, topic_name)
        
        message_json = json.dumps(event_message)
        message_bytes = message_json.encode('utf-8')
        
        attributes = {
            "event_type": event_message["event_type"],
            "webhook_source": event_message["webhook_source"],
            "tenant_id": event_message["tenant_id"]
        }
        
        logger.info(f"TO_{topic_name.upper().replace('-', '_')}: {json.dumps(event_message)}")
        
        future = publisher.publish(topic_path, message_bytes, **attributes)
        message_id = future.result(timeout=30)
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error publishing to topic {topic_name}: {e}")
        webhook_source = event_message.get("webhook_source")
        tenant_id = event_message.get("tenant_id") 
        event_type = event_message.get("event_type")
        customer_id = event_message.get("payload", {}).get("customer_id") if event_message.get("payload") else None
        publish_error_event(f"Failed to publish to topic {topic_name}: {str(e)}", "webhook-dispatcher", webhook_source, tenant_id, event_type, customer_id)
        return False

@functions_framework.http
def webhook_dispatcher(request):
    """
    Cloud Functions entry point for webhook dispatcher
    Handles URL format: https://webhook.habits.fit/?source={webhook_source}&token={token}&type={eventType}
    """
    try:
        if request.method == 'OPTIONS':
            headers = {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
                'Access-Control-Max-Age': '3600'
            }
            return ('', 204, headers)
        
        if request.method == 'GET':
            if request.path == '/health':
                return {
                    "status": "healthy",
                    "service": "webhook-dispatcher",
                    "version": "2.1.0",
                    "project_id": PROJECT_ID
                }
            elif request.path == '/metrics':
                return {
                    "webhooks_received": metrics.webhooks_received,
                    "events_published": metrics.events_published,
                    "events_skipped": metrics.events_skipped,
                    "errors": metrics.errors,
                    "status": "running"
                }
            else:
                return {"message": "Webhook Dispatcher Service - habits.fit"}
        
        if request.method != 'POST':
            return {"error": "Method not allowed"}, 405
        
        # Extract query parameters
        webhook_source = request.args.get('source')
        token = request.args.get('token')
        event_type = request.args.get('type')
        
        if not webhook_source:
            metrics.errors += 1
            return {"error": "Missing 'source' parameter"}, 400
        
        if not token:
            metrics.errors += 1
            return {"error": "Missing 'token' parameter"}, 400
        
        if len(token) < 10:
            metrics.errors += 1
            return {"error": "Invalid token"}, 400
        
        # Parse webhook body
        # Slack interactions send application/x-www-form-urlencoded with a
        # nested JSON string in the 'payload' field — parse that inner JSON
        webhook_data = {}
        try:
            content_type = request.headers.get('Content-Type', '')
            
            if 'application/json' in content_type:
                webhook_data = request.get_json() or {}
            elif 'application/x-www-form-urlencoded' in content_type:
                form_data = request.form.to_dict()
                # Slack interaction payloads arrive as JSON string in 'payload' field
                if 'payload' in form_data and webhook_source == 'slack':
                    try:
                        webhook_data = json.loads(form_data['payload'])
                    except json.JSONDecodeError:
                        webhook_data = form_data
                else:
                    webhook_data = form_data
            else:
                try:
                    webhook_data = request.get_json(force=True) or {}
                except:
                    webhook_data = request.form.to_dict() or {}
                
        except Exception as e:
            metrics.errors += 1
            return {"error": "Invalid webhook payload"}, 400
        
        logger.info(f"INPUT: {json.dumps({'envelope': webhook_data, 'payload': webhook_data})}")
        
        business_context = extract_business_context_fields(webhook_data, webhook_source)
        
        routing_config = determine_event_routing(webhook_source, webhook_data, event_type)
        
        if not routing_config:
            metrics.events_skipped += 1
            return {
                "status": "skipped",
                "message": f"No routing configured for webhook_source: {webhook_source}",
                "webhook_source": webhook_source
            }
        
        if routing_config.get('error_details'):
            error_details = routing_config['error_details']
            
            event_message = {
                "event_type": "error",
                "webhook_source": webhook_source,
                "tenant_id": token,
                "payload": {
                    "service": "webhook-dispatcher",
                    "error_message": f"**{error_details['error_message']}**",
                    "original_event_type": error_details.get('original_event_type'),
                    "payload_keys": error_details.get('payload_keys'),
                    "customer_id": error_details.get('customer_id'),
                    "email": "dennis@habits.fit"
                },
                "receivedAt": datetime.utcnow().isoformat(),
                "event_id": str(uuid.uuid4())
            }
        else:
            event_message = {
                # Technical/Routing Fields
                "event_type": routing_config['event_name'],
                "tenant_id": token,
                "webhook_source": webhook_source,
                
                # Original payload
                "payload": webhook_data,
                
                # Business Context Fields
                **business_context,
                
                # System metadata
                "receivedAt": datetime.utcnow().isoformat(),
                "event_id": str(uuid.uuid4()),
                "webhook_metadata": {
                    "source_param": webhook_source,
                    "token_param": token,
                    "type_param": event_type,
                    "user_agent": request.headers.get("User-Agent", ""),
                    "content_type": request.headers.get("Content-Type", ""),
                    "x_forwarded_for": request.headers.get("X-Forwarded-For", ""),
                    "remote_addr": request.remote_addr
                }
            }
        
        success = publish_to_topic(routing_config['topic'], event_message)
        
        if success:
            metrics.webhooks_received += 1
            metrics.events_published += 1
            
            return {
                "status": "success",
                "message": "Webhook processed successfully",
                "webhook_source": webhook_source,
                "event_type": routing_config['event_name'],
                "topic": routing_config['topic'],
                "event_id": event_message["event_id"],
                "business_context": list(business_context.keys()) if business_context else []
            }
        else:
            metrics.errors += 1
            return {"error": "Failed to publish event"}, 500
        
    except Exception as e:
        logger.error(f"❌ Error processing webhook: {e}")
        metrics.errors += 1
        
        try:
            publish_error_event(f"General service error: {str(e)}", "webhook-dispatcher")
        except:
            pass
            
        return {"error": "Internal server error"}, 500
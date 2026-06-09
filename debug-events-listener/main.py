import base64
import json

def debug_events_listener(event, context):
    """Debug function to log all events sent to 'events' topic"""
    try:
        # Decode message and print as single JSON line
        raw = base64.b64decode(event["data"]).decode("utf-8")
        envelope = json.loads(raw)
        print(json.dumps(envelope))
        
    except Exception as e:
        print(f"Debug error: {e}")

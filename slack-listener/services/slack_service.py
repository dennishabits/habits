import logging
from typing import Dict, Any, Optional, List
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .bigquery_service import BigQueryService

logger = logging.getLogger(__name__)

class SlackService:
    """Service for interacting with Slack API"""
    
    def __init__(self):
        self.bq_service = BigQueryService("solid-future-452906-a2")
        self.messages_sent = 0
        self.client_cache = {}  # Cache Slack clients per tenant
    
    def get_slack_client(self, tenant_id: str) -> WebClient:
        """Get Slack client for specific tenant"""
        if tenant_id not in self.client_cache:
            # Get tenant's Slack bot token from Firestore/BigQuery
            tenant_config = self.bq_service.get_tenant_config(tenant_id)
            if not tenant_config or not tenant_config.get('slack_bot_token'):
                raise ValueError(f"No Slack bot token found for tenant: {tenant_id}")
            
            bot_token = tenant_config['slack_bot_token']
            self.client_cache[tenant_id] = WebClient(token=bot_token)
            logger.info(f"🔑 Created Slack client for tenant: {tenant_id}")
        
        return self.client_cache[tenant_id]
    
    def send_lead_message(self, tenant_id: str, channel: str, message: Dict[str, Any], lead_id: str) -> Dict[str, Any]:
        """Send a new lead message to Slack channel"""
        try:
            client = self.get_slack_client(tenant_id)
            
            # Send message with blocks (rich formatting)
            response = client.chat_postMessage(
                channel=channel,
                blocks=message.get('blocks', []),
                text=message.get('text', 'New Lead'),  # Fallback text
                metadata={
                    "event_type": "lead_submitted",
                    "event_payload": {
                        "lead_id": lead_id,
                        "tenant_id": tenant_id
                    }
                }
            )
            
            self.messages_sent += 1
            logger.info(f"📤 Sent lead message to {channel}: {response['ts']}")
            
            return {
                'ts': response['ts'],
                'channel': response['channel'],
                'success': True
            }
            
        except SlackApiError as e:
            logger.error(f"❌ Slack API error: {e.response['error']}")
            raise
        except Exception as e:
            logger.error(f"❌ Error sending Slack message: {e}")
            raise
    
    def update_message(self, tenant_id: str, channel: str, message_ts: str, updated_message: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing Slack message"""
        try:
            client = self.get_slack_client(tenant_id)
            
            response = client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=updated_message.get('blocks', []),
                text=updated_message.get('text', 'Updated Lead')
            )
            
            logger.info(f"📝 Updated message: {message_ts}")
            
            return {
                'ts': response['ts'],
                'channel': response['channel'],
                'success': True
            }
            
        except SlackApiError as e:
            logger.error(f"❌ Slack API error updating message: {e.response['error']}")
            raise
        except Exception as e:
            logger.error(f"❌ Error updating Slack message: {e}")
            raise
    
    def add_reaction(self, tenant_id: str, channel: str, message_ts: str, reaction: str) -> bool:
        """Add reaction emoji to a message"""
        try:
            client = self.get_slack_client(tenant_id)
            
            client.reactions_add(
                channel=channel,
                timestamp=message_ts,
                name=reaction
            )
            
            logger.info(f"👍 Added reaction {reaction} to message: {message_ts}")
            return True
            
        except SlackApiError as e:
            if e.response['error'] == 'already_reacted':
                logger.debug(f"Reaction {reaction} already exists on message: {message_ts}")
                return True
            else:
                logger.error(f"❌ Slack API error adding reaction: {e.response['error']}")
                raise
        except Exception as e:
            logger.error(f"❌ Error adding Slack reaction: {e}")
            raise
    
    def find_lead_message_by_email(self, tenant_id: str, customer_email: str) -> Optional[Dict[str, Any]]:
        """Find existing lead message in #leads channel by customer email"""
        try:
            client = self.get_slack_client(tenant_id)
            
            # Search recent messages in #leads channel
            # Note: This is a simplified implementation
            # In production, you might want to store message_ts in your database
            
            # Get channel ID for #leads
            channels_response = client.conversations_list(types="public_channel")
            leads_channel_id = None
            
            for channel in channels_response['channels']:
                if channel['name'] == 'leads':
                    leads_channel_id = channel['id']
                    break
            
            if not leads_channel_id:
                logger.warning("⚠️ #leads channel not found")
                return None
            
            # Get recent messages from #leads channel
            history_response = client.conversations_history(
                channel=leads_channel_id,
                limit=50  # Check last 50 messages
            )
            
            # Look for message containing this email
            for message in history_response['messages']:
                if 'blocks' in message:
                    # Search through message blocks for email
                    message_text = self._extract_text_from_blocks(message['blocks'])
                    if customer_email.lower() in message_text.lower():
                        return {
                            'ts': message['ts'],
                            'channel': leads_channel_id,
                            'blocks': message['blocks'],
                            'text': message_text
                        }
            
            logger.debug(f"🔍 No existing message found for email: {customer_email}")
            return None
            
        except SlackApiError as e:
            logger.error(f"❌ Slack API error searching messages: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"❌ Error searching Slack messages: {e}")
            return None
    
    def _extract_text_from_blocks(self, blocks: List[Dict]) -> str:
        """Extract plain text from Slack blocks for searching"""
        text_parts = []
        
        for block in blocks:
            if block.get('type') == 'section' and 'text' in block:
                if block['text'].get('type') == 'mrkdwn':
                    text_parts.append(block['text']['text'])
            elif block.get('type') == 'context' and 'elements' in block:
                for element in block['elements']:
                    if element.get('type') == 'mrkdwn':
                        text_parts.append(element['text'])
        
        return ' '.join(text_parts)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Return service metrics"""
        return {
            "messages_sent": self.messages_sent,
            "active_clients": len(self.client_cache)
        }

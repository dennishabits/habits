import logging
from typing import Dict, Any, Optional
from datetime import datetime

from .bigquery_service import BigQueryService
from .slack_service import SlackService
from utils.slack_formatter import SlackMessageFormatter

logger = logging.getLogger(__name__)

class EventService:
    """Service to process events and coordinate Slack actions"""
    
    def __init__(self, bq_service: BigQueryService, slack_service: SlackService):
        self.bq_service = bq_service
        self.slack_service = slack_service
        self.formatter = SlackMessageFormatter()
        self.events_processed = 0
    
    def process_event(self, event_data: Dict[str, Any]) -> None:
        """Main event processing dispatcher"""
        try:
            source = event_data.get('source', '')
            event_type = event_data.get('eventType', '')
            tenant_id = event_data.get('tenantId', '')
            
            logger.info(f"🔄 Processing {source}:{event_type} for tenant {tenant_id}")
            
            if event_type == 'lead_submitted':
                self.handle_lead_submitted(event_data)
            elif event_type == 'booking':
                self.handle_booking(event_data)
            elif event_type == 'membershipnew':
                self.handle_membership_new(event_data)
            elif event_type.startswith('slack_'):
                self.handle_slack_event(event_data)
            else:
                logger.warning(f"⚠️ Unknown event type: {event_type}")
            
            self.events_processed += 1
            
        except Exception as e:
            logger.error(f"❌ Error processing event: {e}")
            raise
    
    def handle_lead_submitted(self, event_data: Dict[str, Any]) -> None:
        """Handle new lead submission - send to #leads channel"""
        try:
            tenant_id = event_data.get('tenantId')
            payload = event_data.get('payload', {})
            
            # Extract lead information
            customer_email = payload.get('email') or payload.get('Email')
            customer_phone = payload.get('phone') or payload.get('PhoneNumber')
            customer_name = payload.get('name') or f"{payload.get('FirstName', '')} {payload.get('LastName', '')}".strip()
            
            logger.info(f"📧 New lead: {customer_name} ({customer_email})")
            
            # Look up customer history
            customer_info = None
            if customer_email:
                customer_info = self.bq_service.get_customer_by_email(tenant_id, customer_email)
            
            # Create lead message for Slack
            lead_message = self.formatter.format_lead_message(
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_history=customer_info,
                lead_source=payload.get('source', 'website'),
                lead_id=payload.get('lead_id') or event_data.get('event_id')
            )
            
            # Send to Slack #leads channel
            message_result = self.slack_service.send_lead_message(
                tenant_id=tenant_id,
                channel='#leads',
                message=lead_message,
                lead_id=payload.get('lead_id') or event_data.get('event_id')
            )
            
            logger.info(f"✅ Lead message sent to Slack: {message_result.get('ts')}")
            
        except Exception as e:
            logger.error(f"❌ Error handling lead submission: {e}")
            raise
    
    def handle_booking(self, event_data: Dict[str, Any]) -> None:
        """Handle appointment booking - update existing lead message"""
        try:
            tenant_id = event_data.get('tenantId')
            payload = event_data.get('payload', {})
            
            # Extract booking information
            customer_email = payload.get('email') or payload.get('Email')
            appointment_type = payload.get('appointmentType') or payload.get('type', 'appointment')
            appointment_date = payload.get('datetime') or payload.get('date')
            
            logger.info(f"📅 Appointment booked: {customer_email} - {appointment_type}")
            
            # Find existing lead message in Slack (we'll implement this lookup)
            lead_message = self.slack_service.find_lead_message_by_email(tenant_id, customer_email)
            
            if lead_message:
                # Update existing message with appointment info
                updated_message = self.formatter.add_appointment_to_message(
                    original_message=lead_message,
                    appointment_type=appointment_type,
                    appointment_date=appointment_date
                )
                
                self.slack_service.update_message(
                    tenant_id=tenant_id,
                    channel='#leads',
                    message_ts=lead_message['ts'],
                    updated_message=updated_message
                )
                
                logger.info(f"✅ Updated lead message with appointment: {lead_message['ts']}")
            else:
                logger.warning(f"⚠️ No existing lead message found for {customer_email}")
        
        except Exception as e:
            logger.error(f"❌ Error handling booking: {e}")
            raise
    
    def handle_membership_new(self, event_data: Dict[str, Any]) -> None:
        """Handle new membership - update lead message as converted"""
        try:
            tenant_id = event_data.get('tenantId')
            payload = event_data.get('payload', {})
            
            # Extract membership information
            customer_id = payload.get('customerId')
            membership_type = payload.get('MembershipDescription', 'membership')
            
            logger.info(f"🎉 New membership: {customer_id} - {membership_type}")
            
            # Get customer details to find email
            customer_info = self.bq_service.get_customer_by_id(tenant_id, customer_id)
            if not customer_info:
                logger.warning(f"⚠️ Customer not found: {customer_id}")
                return
            
            customer_email = customer_info.get('email')
            
            # Find and update lead message
            lead_message = self.slack_service.find_lead_message_by_email(tenant_id, customer_email)
            
            if lead_message:
                # Update message to show conversion
                updated_message = self.formatter.mark_lead_as_converted(
                    original_message=lead_message,
                    membership_type=membership_type,
                    conversion_date=datetime.utcnow().isoformat()
                )
                
                self.slack_service.update_message(
                    tenant_id=tenant_id,
                    channel='#leads',
                    message_ts=lead_message['ts'],
                    updated_message=updated_message
                )
                
                # Add celebration reaction
                self.slack_service.add_reaction(
                    tenant_id=tenant_id,
                    channel='#leads',
                    message_ts=lead_message['ts'],
                    reaction='tada'
                )
                
                logger.info(f"🎉 Marked lead as converted: {lead_message['ts']}")
            else:
                logger.warning(f"⚠️ No existing lead message found for {customer_email}")
        
        except Exception as e:
            logger.error(f"❌ Error handling membership: {e}")
            raise
    
    def handle_slack_event(self, event_data: Dict[str, Any]) -> None:
        """Handle events from Slack (reactions, button clicks, etc.)"""
        try:
            event_type = event_data.get('eventType')
            payload = event_data.get('payload', {})
            
            logger.info(f"💬 Handling Slack event: {event_type}")
            
            if event_type == 'slack_reaction_added':
                self.handle_slack_reaction(event_data)
            elif event_type.startswith('slack_interactive'):
                self.handle_slack_interaction(event_data)
            else:
                logger.debug(f"🤷 Unhandled Slack event: {event_type}")
        
        except Exception as e:
            logger.error(f"❌ Error handling Slack event: {e}")
            raise
    
    def handle_slack_reaction(self, event_data: Dict[str, Any]) -> None:
        """Handle Slack reaction events (e.g., ✅ for task completion)"""
        payload = event_data.get('payload', {})
        event = payload.get('event', {})
        
        reaction = event.get('reaction')
        user = event.get('user')
        
        logger.info(f"👍 Reaction added: {reaction} by {user}")
        
        # TODO: Implement reaction-based task updates
        # e.g., ✅ = mark task complete, ❌ = mark as declined
    
    def handle_slack_interaction(self, event_data: Dict[str, Any]) -> None:
        """Handle Slack button/interaction events"""
        payload = event_data.get('payload', {})
        
        logger.info(f"🔘 Slack interaction: {payload.get('type')}")
        
        # TODO: Implement button interactions
        # e.g., "Call Made", "Appointment Booked", "Decline Lead"
    
    def get_metrics(self) -> Dict[str, Any]:
        """Return service metrics"""
        return {
            "events_processed": self.events_processed,
            "service_status": "running"
        }

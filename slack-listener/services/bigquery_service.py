import logging
from typing import Dict, Any, Optional
from google.cloud import bigquery, firestore

logger = logging.getLogger(__name__)

class BigQueryService:
    """Service for querying customer data from BigQuery and tenant config from Firestore"""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.bq_client = bigquery.Client(project=project_id)
        self.firestore_client = firestore.Client(project=project_id)
        self.dataset_id = "gym_analytics"
    
    def get_customer_by_email(self, tenant_id: str, email: str) -> Optional[Dict[str, Any]]:
        """Get customer information by email from the customers view"""
        try:
            query = f"""
            SELECT 
                customer_id,
                first_name,
                last_name,
                email,
                phone_mobile,
                phone_private,
                member_since,
                location_name,
                customer_status,
                has_active_membership,
                membership_description,
                membership_amount,
                membership_start_date,
                membership_end_date,
                customer_last_updated
            FROM `{self.project_id}.{self.dataset_id}.customers`
            WHERE tenant_id = @tenant_id 
            AND LOWER(email) = LOWER(@email)
            ORDER BY customer_last_updated DESC
            LIMIT 1
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                    bigquery.ScalarQueryParameter("email", "STRING", email),
                ]
            )
            
            results = self.bq_client.query(query, job_config=job_config)
            rows = list(results)
            
            if rows:
                # Convert Row to dict
                customer_data = dict(rows[0])
                logger.debug(f"👤 Found customer: {customer_data.get('first_name')} {customer_data.get('last_name')}")
                return customer_data
            else:
                logger.debug(f"👤 No customer found for email: {email}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error querying customer by email: {e}")
            return None
    
    def get_customer_by_id(self, tenant_id: str, customer_id: str) -> Optional[Dict[str, Any]]:
        """Get customer information by customer ID"""
        try:
            query = f"""
            SELECT *
            FROM `{self.project_id}.{self.dataset_id}.customers`
            WHERE tenant_id = @tenant_id 
            AND customer_id = @customer_id
            LIMIT 1
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                    bigquery.ScalarQueryParameter("customer_id", "STRING", customer_id),
                ]
            )
            
            results = self.bq_client.query(query, job_config=job_config)
            rows = list(results)
            
            if rows:
                customer_data = dict(rows[0])
                logger.debug(f"👤 Found customer by ID: {customer_data.get('first_name')} {customer_data.get('last_name')}")
                return customer_data
            else:
                logger.debug(f"👤 No customer found for ID: {customer_id}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error querying customer by ID: {e}")
            return None
    
    def get_tenant_config(self, tenant_token: str) -> Optional[Dict[str, Any]]:
        """Get tenant configuration from Firestore using tenant token"""
        try:
            tenant_doc = self.firestore_client.collection("tenants").document(tenant_token).get()
            
            if tenant_doc.exists:
                tenant_data = tenant_doc.to_dict()
                logger.debug(f"🏢 Found tenant config: {tenant_data.get('tenantId')}")
                return tenant_data
            else:
                logger.warning(f"🏢 No tenant config found for token: {tenant_token}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error getting tenant config: {e}")
            return None
    
    def get_recent_lead_events(self, tenant_id: str, limit: int = 10) -> list:
        """Get recent lead events for a tenant"""
        try:
            query = f"""
            SELECT 
                event_id,
                event_type,
                received_at,
                raw_payload,
                customer_id
            FROM `{self.project_id}.{self.dataset_id}.raw_events`
            WHERE tenant_id = @tenant_id 
            AND event_type IN ('lead_submitted', 'booking', 'membershipnew')
            ORDER BY received_at DESC
            LIMIT @limit
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ]
            )
            
            results = self.bq_client.query(query, job_config=job_config)
            events = [dict(row) for row in results]
            
            logger.debug(f"📊 Found {len(events)} recent events for tenant: {tenant_id}")
            return events
            
        except Exception as e:
            logger.error(f"❌ Error querying recent events: {e}")
            return []
    
    def store_slack_message_reference(self, tenant_id: str, lead_id: str, message_ts: str, channel: str):
        """Store reference to Slack message for later updates (optional - for better message tracking)"""
        try:
            # This could be a separate table to track Slack message references
            # For now, we'll skip this and rely on email-based message lookup
            logger.debug(f"📝 Would store message reference: {message_ts} for lead: {lead_id}")
            
        except Exception as e:
            logger.error(f"❌ Error storing message reference: {e}")
    
    def get_tenant_metrics(self, tenant_id: str) -> Dict[str, Any]:
        """Get basic metrics for a tenant"""
        try:
            # Count recent leads
            query = f"""
            SELECT 
                COUNT(*) as total_events,
                COUNTIF(event_type = 'lead_submitted') as leads_count,
                COUNTIF(event_type = 'booking') as bookings_count,
                COUNTIF(event_type = 'membershipnew') as conversions_count
            FROM `{self.project_id}.{self.dataset_id}.raw_events`
            WHERE tenant_id = @tenant_id 
            AND received_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                ]
            )
            
            results = self.bq_client.query(query, job_config=job_config)
            row = list(results)[0]
            
            metrics = dict(row)
            logger.debug(f"📊 Tenant metrics: {metrics}")
            return metrics
            
        except Exception as e:
            logger.error(f"❌ Error getting tenant metrics: {e}")
            return {
                "total_events": 0,
                "leads_count": 0, 
                "bookings_count": 0,
                "conversions_count": 0
            }

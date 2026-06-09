import base64
import json
import os
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
from google.cloud import pubsub_v1, bigquery
import phonenumbers
import functions_framework


def log_json(label, envelope, payload=None):
    """Log JSON data in the standardized format"""
    if payload is None:
        payload = envelope.get('payload', {})
    
    log_data = {
        "envelope": envelope,
        "payload": payload
    }
    print(f"{label}: {json.dumps(log_data, default=str)}")


def normalize_value(value):
    """Normalize values for comparison - treat empty strings and None as None"""
    if value == "" or value is None:
        return None
    return value


def normalize_sportivity_date(date_value, field_name="unknown"):
    """
    Convert Sportivity date formats to ISO 8601 format with Europe/Amsterdam timezone
    Handles: DD-MM-YYYY, DD/MM/YYYY, DD-MM-YYYY HH:MM, epoch timestamps
    Returns: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS+XX:XX format
    """
    if not date_value or date_value == "":
        return None
    
    try:
        # Handle epoch timestamps (convert to ISO date)
        if isinstance(date_value, (int, float)):
            # Convert epoch timestamp to date (assume it's in seconds)
            dt = datetime.fromtimestamp(date_value)
            return dt.strftime('%Y-%m-%d')
        
        # Handle string epoch timestamps
        if isinstance(date_value, str) and date_value.isdigit() and len(date_value) >= 10:
            # Convert string epoch timestamp to date
            dt = datetime.fromtimestamp(int(date_value))
            return dt.strftime('%Y-%m-%d')
        
        if not isinstance(date_value, str):
            return date_value
        
        # If it already has timezone info, return as-is
        if date_value.endswith('Z') or '+' in date_value[-6:] or date_value.endswith('+00:00'):
            return date_value
        
        # Try to parse various Sportivity formats
        formats_to_try = [
            ('%d-%m-%Y %H:%M', True),      # DD-MM-YYYY HH:MM (needs timezone)
            ('%d-%m-%Y', False),           # DD-MM-YYYY (date only)
            ('%d/%m/%Y', False),           # DD/MM/YYYY (date only)
            ('%d/%m/%Y %H:%M', True),      # DD/MM/YYYY HH:MM (needs timezone)
            ('%Y-%m-%d', False),           # Already ISO format (date only)
            ('%Y-%m-%dT%H:%M:%S', True),   # Already ISO datetime format (needs timezone check)
        ]
        
        amsterdam_tz = ZoneInfo("Europe/Amsterdam")
        
        for fmt, needs_timezone in formats_to_try:
            try:
                dt = datetime.strptime(date_value, fmt)
                
                if needs_timezone:
                    # Assume the datetime is in Europe/Amsterdam timezone
                    dt_with_tz = dt.replace(tzinfo=amsterdam_tz)
                    return dt_with_tz.isoformat()
                else:
                    # Date only - return as YYYY-MM-DD
                    return dt.strftime('%Y-%m-%d')
                    
            except ValueError:
                continue
        
        # If no format worked, return original value
        print(f"Warning: Could not parse date '{date_value}' for field '{field_name}'")
        return date_value
        
    except Exception as e:
        print(f"Error normalizing date '{date_value}' for field '{field_name}': {e}")
        return date_value


def parse_date_for_bigquery(date_str):
    """Parse Sportivity date to date string for BigQuery comparison"""
    if not date_str or date_str == "":
        return None
    try:
        if isinstance(date_str, str):
            if " " in date_str:
                dt = datetime.strptime(date_str.split()[0], "%d-%m-%Y")
            elif "-" in date_str:
                dt = datetime.strptime(date_str, "%d-%m-%Y")
            elif "/" in date_str:
                dt = datetime.strptime(date_str, "%d/%m/%Y")
            else:
                dt = datetime.fromisoformat(date_str)
            return dt.strftime("%Y-%m-%d")
        elif isinstance(date_str, (int, float)):
            dt = datetime.fromtimestamp(date_str)
            return dt.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"Could not parse date '{date_str}': {e}")
    return None


def parse_date_to_date_object(date_str):
    """Parse various date formats to Python date object for comparison"""
    if not date_str or date_str == "":
        return None
    
    try:
        if isinstance(date_str, str):
            # Handle ISO format dates
            if 'T' in date_str:
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                return dt.date()
            elif len(date_str) == 10 and '-' in date_str:
                return datetime.strptime(date_str, '%Y-%m-%d').date()
            # Handle Sportivity format dates
            elif " " in date_str:
                dt = datetime.strptime(date_str.split()[0], "%d-%m-%Y")
                return dt.date()
            elif "/" in date_str:
                dt = datetime.strptime(date_str, "%d/%m/%Y")
                return dt.date()
            elif "-" in date_str and len(date_str.split('-')[0]) <= 2:
                dt = datetime.strptime(date_str, "%d-%m-%Y")
                return dt.date()
        elif isinstance(date_str, (int, float)):
            dt = datetime.fromtimestamp(date_str)
            return dt.date()
            
    except Exception as e:
        print(f"Could not parse date '{date_str}' to date object: {e}")
    
    return None


def calculate_subscription_status(start_date_str, end_date_str):
    """
    Calculate subscription_active and subscription_future based on dates
    Uses Europe/Amsterdam timezone for current date
    """
    amsterdam_tz = ZoneInfo("Europe/Amsterdam")
    current_date = datetime.now(amsterdam_tz).date()
    
    start_date = parse_date_to_date_object(start_date_str)
    end_date = parse_date_to_date_object(end_date_str)
    
    # Calculate subscription_future
    subscription_future = False
    if start_date and start_date > current_date:
        subscription_future = True
    
    # Calculate subscription_active
    subscription_active = False
    
    if start_date and start_date <= current_date:
        if end_date is None:
            # No end date means active (unlimited)
            subscription_active = True
        elif end_date >= current_date:
            # Current date is between start and end
            subscription_active = True
    
    return subscription_active, subscription_future


def process_enricher_subscription_fields(payload):
    """
    Process subscription fields that come from the enricher in standardized format
    The enricher now provides: subscription_name, subscription_start_date, subscription_end_date, 
    subscription_active, subscription_future, subscription_cancelled, subscription_price
    """
    processed_payload = payload.copy()
    
    # Boolean fields - ensure they're actual booleans
    boolean_fields = [
        'subscription_active', 'subscription_cancelled', 'subscription_future',
        'active', 'addon_on', 'terminated', 'future_membership', 'unlimited_visits'
    ]
    
    for field in boolean_fields:
        if field in processed_payload:
            value = processed_payload[field]
            if isinstance(value, str):
                processed_payload[field] = value.lower() == "true"
            elif value is not None:
                processed_payload[field] = bool(value)
    
    # Date fields - normalize to ISO format
    date_fields = [
        'subscription_start_date', 'subscription_end_date', 'last_visit',
        'start_date', 'contract_end_date', 'cancelled_per_date',
        'birth_date', 'member_since', 'suspension_start_date', 'suspension_end_date'
    ]
    
    for field in date_fields:
        if field in processed_payload and processed_payload[field]:
            processed_payload[field] = normalize_sportivity_date(processed_payload[field], field)
    
    # Price fields - ensure numeric
    price_fields = ['subscription_price', 'amount', 'membership_amount']
    for field in price_fields:
        if field in processed_payload and processed_payload[field] is not None:
            try:
                processed_payload[field] = float(processed_payload[field])
            except (ValueError, TypeError):
                processed_payload[field] = 0.0
    
    return processed_payload


def map_customer_api_to_bigquery(api_data):
    """Map Sportivity API customer data to BigQuery schema - only fields that exist in API"""
    return {
        'customer_id': normalize_value(str(api_data.get('CustomerId') or api_data.get('Id') or api_data.get('customer_id'))),
        'email': normalize_value(api_data.get('Email') or api_data.get('email')),
        'firstname': normalize_value(api_data.get('FirstName') or api_data.get('firstname')),
        'lastname': normalize_value(api_data.get('LastName') or api_data.get('lastname')),
        'birth_date': parse_date_for_bigquery(api_data.get('BirthDate') or api_data.get('DateOfBirth') or api_data.get('birth_date')),
        'gender': normalize_value(api_data.get('Gender') or api_data.get('gender')),
        'phone_number': normalize_value(api_data.get('PhoneMobile') or api_data.get('Phone') or api_data.get('phone_number')),
        'street': normalize_value(api_data.get('Address') or api_data.get('street')),
        'house_number': normalize_value(api_data.get('HouseNumber') or api_data.get('house_number')),
        'city': normalize_value(api_data.get('City') or api_data.get('city')),
        'zip_code': normalize_value(api_data.get('Zipcode') or api_data.get('PostalCode') or api_data.get('zip_code') or api_data.get('zip')),
        'country': normalize_value(api_data.get('Country') or api_data.get('country')),
        'member_since': parse_date_for_bigquery(api_data.get('MemberSince') or api_data.get('member_since')),
        'iban': normalize_value(api_data.get('IBAN') or api_data.get('iban')),
        # Include subscription fields from enricher
        'subscription_name': normalize_value(api_data.get('subscription_name')),
        'subscription_start_date': parse_date_for_bigquery(api_data.get('subscription_start_date')),
        'subscription_end_date': parse_date_for_bigquery(api_data.get('subscription_end_date')),
        'subscription_active': api_data.get('subscription_active'),
        'subscription_future': api_data.get('subscription_future'),
        'subscription_cancelled': api_data.get('subscription_cancelled'),
        'last_visit': parse_date_for_bigquery(api_data.get('last_visit')),
    }


def map_membership_api_to_bigquery(api_data):
    """Map Sportivity API membership data to BigQuery schema - FIXED to use subscription_id"""
    return {
        'customer_id': normalize_value(str(api_data.get('CustomerId') or api_data.get('customer_id') or api_data.get('CustomerID'))),
        'subscription_id': normalize_value(str(api_data.get('MembershipId') or api_data.get('MembershipID') or api_data.get('Id'))),
        'subscription_name': normalize_value(api_data.get('Description') or api_data.get('subscription_name')),
        'start_date': parse_date_for_bigquery(api_data.get('StartDate')),
        'end_date': parse_date_for_bigquery(api_data.get('ContractEndDate') or api_data.get('EndDate')),
        'access_end_date': parse_date_for_bigquery(api_data.get('AccessEndDate') or api_data.get('acces_end_date')),
        'payment_end_date': parse_date_for_bigquery(api_data.get('PaymentEndDate')),
        'cancelled_per_date': parse_date_for_bigquery(api_data.get('CancelledPerDate') or api_data.get('CancelledAtDate')),
        'cancel_reason': normalize_value(api_data.get('CancelReason') or api_data.get('cancel_reason')),
        'subscription_price': api_data.get('MembershipAmount') or api_data.get('Amount') or api_data.get('AmountWithDiscount'),
        'is_terminated': api_data.get('Terminated'),
        'is_future_membership': api_data.get('FutureMembership'),
        'unlimited_visits': api_data.get('UnlimitedVisits'),
        'visits_left': api_data.get('VisitsLeft'),
        'is_swimming': api_data.get('IsSwimming') or api_data.get('Swimming'),
        'subscription_status': normalize_value(api_data.get('Status') or api_data.get('MembershipStatus')),
    }


def get_customer_from_bigquery(customer_id, tenant_id, bq_client, project_id):
    """Get customer record from BigQuery"""
    try:
        query = f"""
        SELECT *
        FROM `{project_id}.gym_analytics.customers`
        WHERE customer_id = @customer_id AND tenant_id = @tenant_id
        ORDER BY last_updated DESC
        LIMIT 1
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("customer_id", "STRING", customer_id),
                bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            ]
        )
        
        query_job = bq_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            customer_data = {}
            for key, value in row.items():
                if key in ['last_updated', 'processed_at']:
                    continue
                elif key in ['birth_date', 'member_since', 'subscription_end_date', 'subscription_start_date', 'last_visit']:
                    customer_data[key] = value.strftime("%Y-%m-%d") if value else None
                else:
                    customer_data[key] = normalize_value(value)
            return customer_data
            
    except Exception as e:
        print(f"Error querying customer from BigQuery: {e}")
    
    return None


def get_subscription_from_bigquery(subscription_id, tenant_id, bq_client, project_id):
    """Get subscription record from BigQuery - FIXED to use subscription_id"""
    try:
        query = f"""
        SELECT *
        FROM `{project_id}.gym_analytics.subscriptions`
        WHERE subscription_id = @subscription_id AND tenant_id = @tenant_id
        ORDER BY last_updated DESC
        LIMIT 1
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("subscription_id", "STRING", subscription_id),
                bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            ]
        )
        
        query_job = bq_client.query(query, job_config=job_config)
        results = query_job.result()
        
        for row in results:
            subscription_data = {}
            for key, value in row.items():
                if key in ['last_updated', 'processed_at']:
                    continue
                elif key in ['start_date', 'end_date', 'access_end_date', 'payment_end_date', 'cancelled_per_date']:
                    subscription_data[key] = value.strftime("%Y-%m-%d") if value else None
                else:
                    subscription_data[key] = normalize_value(value)
            return subscription_data
            
    except Exception as e:
        print(f"Error querying subscription from BigQuery: {e}")
    
    return None


def create_field_mapping():
    """Create comprehensive mapping from original Sportivity/BigQuery field names to final translated field names"""
    return {
        # Customer fields
        'CustomerId': 'customer_id', 'customer_id': 'customer_id', 'Id': 'customer_id', 'CustomerID': 'customer_id',
        'Email': 'email', 'email': 'email', 'EmailAddress': 'email',
        'FirstName': 'firstname', 'firstname': 'firstname', 
        'LastName': 'lastname', 'lastname': 'lastname',
        'BirthDate': 'birth_date', 'DateOfBirth': 'birth_date', 'birth_date': 'birth_date',
        'Gender': 'gender', 'gender': 'gender',
        'PhoneMobile': 'phone_number', 'Phone': 'phone_number', 'phone_number': 'phone_number',
        'Address': 'street', 'street': 'street',
        'HouseNumber': 'house_number', 'house_number': 'house_number',
        'City': 'city', 'city': 'city',
        'Zipcode': 'zip', 'PostalCode': 'zip', 'zip_code': 'zip', 'zip': 'zip',
        'Country': 'country', 'country': 'country',
        'MemberSince': 'member_since', 'member_since': 'member_since',
        'IBAN': 'iban', 'iban': 'iban',
        
        # Subscription/Membership fields - FIXED to use subscription_id
        'MembershipId': 'subscription_id', 'MembershipID': 'subscription_id', 'membership_id': 'subscription_id',
        'Description': 'subscription_name', 'subscription_name': 'subscription_name', 'MembershipDescription': 'subscription_name',
        'StartDate': 'suspension_start_date', 'start_date': 'start_date', 'suspension_start_date': 'suspension_start_date',
        'ContractEndDate': 'contract_end_date', 'contract_end_date': 'contract_end_date',
        'EndDate': 'suspension_end_date', 'end_date': 'contract_end_date', 'suspension_end_date': 'suspension_end_date',
        'AccessEndDate': 'access_end_date', 'acces_end_date': 'access_end_date', 'access_end_date': 'access_end_date',
        'PaymentEndDate': 'payment_end_date', 'payment_end_date': 'payment_end_date',
        'CancelledPerDate': 'cancelled_per_date', 'CancelledAtDate': 'cancelled_per_date', 'cancelled_per_date': 'cancelled_per_date',
        'CancelPerDate': 'cancelled_per_date', 'cancel_per_date': 'cancelled_per_date',
        'CancelReason': 'cancel_reason', 'cancel_reason': 'cancel_reason',
        'MembershipAmount': 'subscription_price', 'Amount': 'subscription_price', 'AmountWithDiscount': 'subscription_price',
        'amount_with_discount': 'subscription_price', 'membership_amount': 'subscription_price',
        'Terminated': 'is_terminated', 'is_terminated': 'is_terminated',
        'FutureMembership': 'is_future_membership', 'is_future_membership': 'is_future_membership',
        'UnlimitedVisits': 'unlimited_visits', 'unlimited_visits': 'unlimited_visits',
        'VisitsLeft': 'visits_left', 'visits_left': 'visits_left',
        'IsSwimming': 'is_swimming', 'Swimming': 'is_swimming', 'is_swimming': 'is_swimming',
        'Status': 'subscription_status', 'MembershipStatus': 'subscription_status', 'subscription_status': 'subscription_status',
        
        # Subscription fields from enricher
        'subscription_start_date': 'subscription_start_date', 'subscription_end_date': 'subscription_end_date', 
        'subscription_active': 'subscription_active', 'subscription_future': 'subscription_future',
        'subscription_cancelled': 'subscription_cancelled',
        
        # Last visit field
        'LastVisit': 'last_visit', 'last_visit': 'last_visit',
        
        # Visit fields
        'EntryDate': 'entry_date', 'entry_date': 'entry_date',
        'Gate': 'gate', 'gate': 'gate',
        'NumberOfVisits': 'number_of_visits', 'number_of_visits': 'number_of_visits',
        'NumberOfVisitsLeft': 'number_of_visits_left', 'number_of_visits_left': 'number_of_visits_left',
        'Result': 'result', 'result': 'result',
        
        # Suspension fields
        'BlockageID': 'blockage_id', 'blockage_id': 'blockage_id',
        'CustomerID': 'customer_id',
        
        # Addon fields
        'Addonid': 'addon_id', 'addon_id': 'addon_id',
        'Active': 'active', 'active': 'active',
        'AddonOn': 'addon_on', 'addon_on': 'addon_on',
    }


def map_changed_fields_to_final_names(changed_fields, final_payload):
    """Map original field names in changed_fields to final translated field names"""
    if not changed_fields:
        return []
    
    field_mapping = create_field_mapping()
    mapped_fields = []
    
    for original_field in changed_fields:
        # First try direct mapping
        if original_field in field_mapping:
            final_field = field_mapping[original_field]
            # Ensure the final field actually exists in the payload
            if final_field in final_payload:
                mapped_fields.append(final_field)
                continue
        
        # If no direct mapping, check if the field exists as-is in the final payload
        if original_field in final_payload:
            mapped_fields.append(original_field)
            continue
        
        # Try snake_case conversion as fallback
        snake_case_field = convert_pascalcase_to_snake_case(original_field)
        if snake_case_field in final_payload:
            mapped_fields.append(snake_case_field)
            continue
        
        # If no mapping found, log warning but don't include in mapped_fields
        print(f"Warning: Could not map changed field '{original_field}' to final payload field. Available fields: {list(final_payload.keys())}")
    
    return mapped_fields


def detect_changes(old_data, new_data, exclude_fields=None, event_type=None):
    """Compare two data dictionaries and return changed fields"""
    default_excluded = [
        'customer_key', 'brand', 'tenant_id', 'webhook_source', 'event_type',
        'received_at', 'receivedAt', 'timestamp', 'event_id', 'membership_amount',
        'subscription_active', 'subscription_cancelled', 'subscription_end_date', 
        'subscription_future', 'subscription_start_date', 'has_swimming'
    ]
    
    # For subscription events, don't exclude subscription_name and subscription_price
    if event_type != 'subscription_update':
        default_excluded.append('subscription_name')
    
    exclude_fields = exclude_fields or []
    exclude_fields.extend(default_excluded)
    
    changed_fields = []
    
    if not old_data:
        for field in new_data.keys():
            if field not in exclude_fields:
                changed_fields.append(field)
        
        # For subscription events, always include essential fields even if it's a new record
        if event_type == 'subscription_update':
            essential_fields = ['subscription_name', 'subscription_price']
            for field in essential_fields:
                if field in new_data and field not in changed_fields:
                    changed_fields.append(field)
        
        return changed_fields
    
    for field, new_value in new_data.items():
        if field in exclude_fields:
            continue
            
        old_value = old_data.get(field)
        old_value = normalize_value(old_value)
        new_value = normalize_value(new_value)
        
        if old_value != new_value:
            changed_fields.append(field)
    
    # For subscription events, always include essential fields even if they didn't change
    if event_type == 'subscription_update':
        essential_fields = ['subscription_name', 'subscription_price']
        for field in essential_fields:
            if field in new_data and field not in changed_fields:
                changed_fields.append(field)
    
    return changed_fields


def normalize_dates_in_payload(payload):
    """Normalize all date fields in a payload to ISO 8601 format with proper timezone"""
    if not isinstance(payload, dict):
        return payload
    
    date_fields = {
        'EntryDate', 'StartDate', 'ContractEndDate', 'CancelPerDate', 'CancelledPerDate',
        'BirthDate', 'DateOfBirth', 'dob', 'created_at', 'updated_at', 'timestamp',
        'BookingDate', 'AppointmentDate', 'VisitDate', 'JoinDate', 'EndDate', 'MemberSince',
        'AccessEndDate', 'PaymentEndDate', 'CancelledAtDate', 'acces_end_date',
        'subscription_start_date', 'subscription_end_date', 'LastVisit', 'last_visit',
        'suspension_start_date', 'suspension_end_date'
    }
    
    normalized_payload = {}
    
    for key, value in payload.items():
        if isinstance(value, dict):
            normalized_payload[key] = normalize_dates_in_payload(value)
        elif isinstance(value, list):
            normalized_list = []
            for item in value:
                if isinstance(item, dict):
                    normalized_list.append(normalize_dates_in_payload(item))
                else:
                    normalized_list.append(item)
            normalized_payload[key] = normalized_list
        elif key in date_fields or key.lower().endswith('date') or key.lower().endswith('_date') or key.lower().endswith('visit'):
            normalized_payload[key] = normalize_sportivity_date(value, key)
        else:
            normalized_payload[key] = value
    
    return normalized_payload


def convert_pascalcase_to_snake_case(name):
    """Convert PascalCase or camelCase field names to snake_case"""
    if name in ['FirstName', 'firstname']:
        return 'firstname'
    if name in ['LastName', 'lastname']:
        return 'lastname'
    if name == 'acces_end_date':
        return 'access_end_date'
    if name.isupper() and len(name) <= 4:
        return name.lower()
    
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    
    return s2.lower()


def transform_gender(title):
    """Transform title to gender code"""
    if not title:
        return None
    title_lower = title.lower().strip()
    if title_lower in ['dhr', 'dhr.', 'mr', 'mr.']:
        return 'm'
    elif title_lower in ['mevr', 'mevr.', 'mrs', 'mrs.', 'ms', 'ms.']:
        return 'f'
    return None


def combine_lastname_fields(firstname, middlename, lastname):
    """Combine middlename and lastname into lastname field"""
    if not lastname:
        return lastname
    
    if middlename and middlename.strip():
        return f"{middlename.strip()} {lastname.strip()}"
    return lastname.strip()


def combine_house_number_fields(house_number, house_number_addition):
    """Combine house_number and house_number_addition into house_number field"""
    if not house_number:
        return house_number
    
    if house_number_addition and str(house_number_addition).strip():
        return f"{str(house_number).strip()} {str(house_number_addition).strip()}"
    return str(house_number).strip()


def apply_field_transformations(data):
    """Apply business logic transformations to field values including subscription status calculation"""
    if not isinstance(data, dict):
        return data
    
    # First process subscription fields from enricher
    transformed_data = process_enricher_subscription_fields(data)
    
    # Calculate subscription status based on dates (only if not already provided by enricher)
    if not transformed_data.get('subscription_active') and not transformed_data.get('subscription_future'):
        start_date = (transformed_data.get('start_date') or 
                      transformed_data.get('StartDate') or 
                      transformed_data.get('subscription_start_date'))
        end_date = (transformed_data.get('contract_end_date') or 
                    transformed_data.get('ContractEndDate') or 
                    transformed_data.get('end_date') or 
                    transformed_data.get('EndDate') or 
                    transformed_data.get('subscription_end_date'))
        
        if start_date or end_date:
            subscription_active, subscription_future = calculate_subscription_status(start_date, end_date)
            transformed_data['subscription_active'] = subscription_active
            transformed_data['subscription_future'] = subscription_future
    
    # Calculate subscription_cancelled based on CancelledPerDate (only if not already provided)
    if 'subscription_cancelled' not in transformed_data:
        cancelled_date = (transformed_data.get('cancelled_per_date') or 
                          transformed_data.get('CancelledPerDate') or 
                          transformed_data.get('CancelPerDate'))
        if cancelled_date:
            cancelled_date_obj = parse_date_to_date_object(cancelled_date)
            if cancelled_date_obj:
                amsterdam_tz = ZoneInfo("Europe/Amsterdam")
                current_date = datetime.now(amsterdam_tz).date()
                transformed_data['subscription_cancelled'] = cancelled_date_obj <= current_date
            else:
                transformed_data['subscription_cancelled'] = False
        else:
            transformed_data['subscription_cancelled'] = False
    
    # Transform title to gender
    if 'title' in transformed_data:
        gender = transform_gender(transformed_data['title'])
        if gender:
            transformed_data['gender'] = gender
        del transformed_data['title']
    
    # Combine lastname fields
    if 'lastname' in transformed_data:
        firstname = transformed_data.get('firstname', '')
        middlename = transformed_data.get('middlename', '') or transformed_data.get('middle_name', '')
        lastname = transformed_data.get('lastname', '')
        
        transformed_data['lastname'] = combine_lastname_fields(firstname, middlename, lastname)
        
        for field in ['middlename', 'middle_name']:
            if field in transformed_data:
                del transformed_data[field]
    
    # Combine house number fields
    if 'house_number' in transformed_data:
        house_number = transformed_data.get('house_number')
        house_number_addition = transformed_data.get('house_number_addition')
        
        transformed_data['house_number'] = combine_house_number_fields(house_number, house_number_addition)
        transformed_data.pop('house_number_addition', None)
    
    # Phone number normalization
    phone_number = None
    if 'phone_private' in transformed_data and transformed_data['phone_private']:
        phone_number = transformed_data['phone_private']
    elif 'phone_mobile' in transformed_data and transformed_data['phone_mobile']:
        phone_number = transformed_data['phone_mobile']
    
    if phone_number:
        phone_clean = str(phone_number).replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        
        if phone_clean.startswith('+'):
            if phone_clean.startswith('+31') and len(phone_clean) == 12:
                e164_number = phone_clean
            elif phone_clean.startswith('+46') and len(phone_clean) >= 12:
                e164_number = phone_clean
            elif phone_clean.startswith('+') and len(phone_clean) >= 10:
                e164_number = phone_clean
            else:
                clean_digits = phone_clean[1:]
                if clean_digits.startswith('0'):
                    e164_number = '+31' + clean_digits[1:]
                else:
                    e164_number = '+31' + clean_digits
        elif phone_clean.startswith('0031'):
            e164_number = '+31' + phone_clean[4:]
        elif phone_clean.startswith('0') and len(phone_clean) >= 10:
            e164_number = '+31' + phone_clean[1:]
        elif phone_clean.startswith('31') and len(phone_clean) == 11 and not phone_clean.startswith('316'):
            e164_number = '+' + phone_clean
        else:
            e164_number = '+31' + phone_clean
        
        if e164_number.startswith('+31'):
            expected_length = 12 if len(e164_number) > 11 else 11
            if len(e164_number) not in [11, 12]:
                print(f"Warning: Dutch phone number '{e164_number}' doesn't match expected E.164 format")
        
        transformed_data['phone_number'] = e164_number
    
    for field in ['phone_private', 'phone_mobile']:
        transformed_data.pop(field, None)
    
    # Field renames
    field_renames = {
        'zipcode': 'zip',
        'address': 'street',
        'membership_description': 'subscription_name',
        'membership_active': 'subscription_active',
        'amount_with_discount': 'subscription_price'
    }
    
    for old_name, new_name in field_renames.items():
        if old_name in transformed_data:
            transformed_data[new_name] = transformed_data.pop(old_name)
    
    # Fields to remove
    fields_to_remove = {
    'bic', 'company_location_id', 'company_location_name', 
    'customer_has_active_memberships', 'customer_id',
    'pass_number', 'opt_in', 'terms_accepted', 'customer_memberships',
    'customer_email', 'tenant_id', 'token', 'type', 'blockage_id'
    }
    
    for field in fields_to_remove:
        transformed_data.pop(field, None)
    
    return transformed_data


def convert_dict_keys_to_snake_case(data):
    """Recursively convert all dictionary keys from PascalCase to snake_case"""
    if isinstance(data, dict):
        converted_dict = {}
        for key, value in data.items():
            snake_key = convert_pascalcase_to_snake_case(key)
            if isinstance(value, dict):
                converted_dict[snake_key] = convert_dict_keys_to_snake_case(value)
            elif isinstance(value, list):
                converted_dict[snake_key] = [
                    convert_dict_keys_to_snake_case(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                converted_dict[snake_key] = value
        return converted_dict
    
    elif isinstance(data, list):
        return [
            convert_dict_keys_to_snake_case(item) if isinstance(item, dict) else item
            for item in data
        ]
    
    else:
        return data


def clean_and_transform_payload(payload):
    """Complete payload processing: 1. Convert PascalCase to snake_case 2. Apply field transformations and removals"""
    snake_case_payload = convert_dict_keys_to_snake_case(payload)
    cleaned_payload = apply_field_transformations(snake_case_payload)
    return cleaned_payload


class SportivityTranslator:
    def __init__(self, project_id):
        self.project_id = project_id
        self.publisher = pubsub_v1.PublisherClient()
        self.bq_client = bigquery.Client()
        self.events_topic = self.publisher.topic_path(project_id, "events")
    
    def publish_to_events(self, envelope):
        try:
            message_data = json.dumps(envelope).encode("utf-8")
            self.publisher.publish(self.events_topic, message_data).result()
        except Exception as e:
            print(f"Error publishing event: {e}")
    
    def publish_error_event(self, error_message, original_envelope=None):
        """Publish error event to events topic with dennis@habits.fit email"""
        try:
            error_envelope = {
                "webhook_source": "sportivity",
                "tenant_id": original_envelope.get("tenant_id") if original_envelope else None,
                "event_type": "error",
                "received_at": datetime.utcnow().isoformat() + 'Z',
                "customer_id": "dennis@habits.fit",
                "email": "dennis@habits.fit",
                "event_display_name": "Error",
                "event_details": "Sportivity Translator",
                "event_secondary_details": "Processing Error",
                "payload": {
                    "error_message": error_message,
                    "service": "sportivity-translator",
                    "original_event_type": original_envelope.get("event_type") if original_envelope else None
                }
            }
            
            error_envelope = {k: v for k, v in error_envelope.items() if v is not None}
            
            message_data = json.dumps(error_envelope).encode("utf-8")
            self.publisher.publish(self.events_topic, message_data).result()
            print(f"ERROR_EVENT_PUBLISHED: {json.dumps(error_envelope, default=str)}")
            
        except Exception as publish_error:
            print(f"Failed to publish error event: {publish_error}")
    
    def check_for_changes(self, envelope, payload, event_type):
        """Check if event requires change detection and has actual changes - FIXED to use subscription_id"""
        enriched_data = envelope.get("enriched_data", {}) or envelope.get("enrichedData", {})
        requires_change_detection = enriched_data.get("requires_change_detection", False) or enriched_data.get("requiresChangeDetection", False)
        
        tenant_id = envelope.get("tenant_id")
        if not tenant_id:
            print("Warning: No tenant_id found for change detection")
            return True, []
        
        try:
            event_type_lower = event_type.lower()
            
            # Customer update events - handle consolidated format
            if event_type_lower == 'customer_update':
                customer_id = (payload.get('CustomerId') or payload.get('customerid') or payload.get('CustomerID') or
                              payload.get('customer_id') or payload.get('Id') or payload.get('id'))
                
                if not customer_id:
                    print("Warning: No customer_id found for customer change detection")
                    return True, []
                
                previous_customer = get_customer_from_bigquery(str(customer_id), tenant_id, self.bq_client, self.project_id)
                current_customer = map_customer_api_to_bigquery(payload)
                changed_fields = detect_changes(previous_customer, current_customer, event_type='customer_update')
                
                print(f"ENRICHMENT_BIGQUERY_CUSTOMER: {json.dumps(previous_customer or {}, default=str)}")
                
                if requires_change_detection:
                    return len(changed_fields) > 0, changed_fields
                else:
                    return True, []
                
            # Subscription update events - FIXED to use subscription_id
            elif event_type_lower == 'subscription_update':
                subscription_id = (payload.get('MembershipId') or payload.get('MembershipID') or 
                                 payload.get('membership_id') or payload.get('Id') or payload.get('id'))
                
                if not subscription_id:
                    print("Warning: No subscription_id found for subscription change detection")
                    return True, []
                
                previous_subscription = get_subscription_from_bigquery(str(subscription_id), tenant_id, self.bq_client, self.project_id)
                current_subscription = map_membership_api_to_bigquery(payload)
                changed_fields = detect_changes(previous_subscription, current_subscription, event_type='subscription_update')
                
                print(f"ENRICHMENT_BIGQUERY_SUBSCRIPTION: {json.dumps(previous_subscription or {}, default=str)}")
                
                return len(changed_fields) > 0, changed_fields
                
        except Exception as e:
            print(f"Error in change detection: {e}")
            return True, []
        
        return True, []
    
    def get_standardized_display_fields(self, event_type, payload):
        """Get standardized display fields for Slack using status field"""
        
        # Extract status from payload (added by enricher)
        status = payload.get('status', 'update')
        
        if event_type == 'visit':
            location = payload.get('Location', 'Gym')
            duration = payload.get('Duration', '')
            secondary = f"{duration} min" if duration else "Gym Visit"
            return {
                "event_display_name": "Visit",
                "event_details": location,
                "event_secondary_details": secondary
            }
            
        elif event_type == 'customer_update':
            name = f"{payload.get('FirstName', '')} {payload.get('LastName', '')}".strip()
            
            if status == 'new':
                return {
                    "event_display_name": "Customer New",
                    "event_details": name or "Customer",
                    "event_secondary_details": "Profile Created"
                }
            elif status == 'refresh':
                return {
                    "event_display_name": "Customer Refreshed",
                    "event_details": name or "Customer",
                    "event_secondary_details": "Profile Refreshed"
                }
            else:
                return {
                    "event_display_name": "Customer Updated",
                    "event_details": name or "Customer",
                    "event_secondary_details": "Profile Updated"
                }
            
        elif event_type == 'subscription_update':
            subscription_name = (payload.get('MembershipDescription') or payload.get('subscription_name') or 
                               payload.get('Description') or 'Membership')
            
            if status == 'new':
                duration = payload.get('ContractDuration', '')
                period = payload.get('ContractDurationPeriod', '')
                secondary = f"{duration} {period}".strip() if duration and period else "New Membership"
                return {
                    "event_display_name": "Membership New",
                    "event_details": subscription_name,
                    "event_secondary_details": secondary
                }
            elif status == 'refresh':
                return {
                    "event_display_name": "Membership Refreshed",
                    "event_details": subscription_name,
                    "event_secondary_details": "Refreshed"
                }
            elif status == 'suspension':
                start_date = payload.get('StartDate', '')
                end_date = payload.get('EndDate', '')
                date_range = f"{start_date} - {end_date}" if start_date and end_date else "Suspended"
                return {
                    "event_display_name": "Membership Suspended",
                    "event_details": subscription_name,
                    "event_secondary_details": date_range
                }
            else:
                is_cancelled = payload.get('subscription_cancelled', False)
                secondary = "Cancelled" if is_cancelled else "Updated"
                return {
                    "event_display_name": "Membership Updated",
                    "event_details": subscription_name,
                    "event_secondary_details": secondary
                }
            
        elif event_type == 'addon':
            description = payload.get('Description', 'Addon')
            is_active = payload.get('Active', False)
            secondary = "Activated" if is_active else "Deactivated"
            return {
                "event_display_name": "Addon",
                "event_details": description,
                "event_secondary_details": secondary
            }
            
        else:
            return {
                "event_display_name": event_type.replace('_', ' ').title(),
                "event_details": "Activity",
                "event_secondary_details": "Sportivity Event"
            }
    
    def _create_event_payload(self, payload, event_type):
        """Create final event payload by cleaning and transforming the input payload"""
        try:
            # Clean and transform the payload (converts to snake_case and applies business logic)
            final_payload = clean_and_transform_payload(payload)
            
            return final_payload
            
        except Exception as e:
            print(f"Error creating event payload: {e}")
            return payload
    
    def translate_to_events(self, envelope):
        """Translate Sportivity webhook events to standardized event format"""
        try:
            payload = envelope.get("payload", {})
            event_type = envelope.get("event_type", "").lower()
            
            # Extract customer_id early for suspension events
            customer_id = (payload.get('Customersid') or payload.get('CustomerId') or payload.get('CustomerID') or 
                          payload.get('customer_id') or payload.get('memberId') or payload.get('userId') or 
                          payload.get('Id') or payload.get('id'))
            
            customer_email = (payload.get('CustomerEmail') or payload.get('Email') or payload.get('email') or 
                             payload.get('customer_email') or payload.get('EmailAddress') or payload.get('email_address'))
            
            # NEW: For subscription events without email, look it up in BigQuery
            if event_type in ['subscription_update', 'suspension'] and not customer_email and customer_id:
                tenant_id = envelope.get("tenant_id")
                if tenant_id:
                    try:
                        customer_data = get_customer_from_bigquery(str(customer_id), tenant_id, self.bq_client, self.project_id)
                        if customer_data and customer_data.get('email'):
                            customer_email = customer_data['email']
                            log_json("ENRICHMENT_EMAIL_FROM_BIGQUERY", envelope, {
                                "customer_id": customer_id,
                                "email": customer_email
                            })
                    except Exception as e:
                        print(f"Warning: Could not fetch customer email from BigQuery: {e}")
            
            # Handle suspension events - translate to subscription_update with status=suspension
            if event_type == 'suspension':
                # Add status field to payload for display field generation
                payload['status'] = 'suspension'
                # Change event_type to subscription_update
                event_type = 'subscription_update'
            
            has_changes, changed_fields = self.check_for_changes(envelope, payload, event_type)
            
            if not has_changes:
                print(f"No changes detected for {event_type}, skipping event")
                return None
            
            if not customer_id and not customer_email:
                self.publish_error_event(f"No customer identifier found in payload for event {event_type}", envelope)
                return None
            
            normalized_payload = normalize_dates_in_payload(payload)
            display_fields = self.get_standardized_display_fields(event_type, normalized_payload)
            final_payload = self._create_event_payload(normalized_payload, event_type)
            mapped_changed_fields = map_changed_fields_to_final_names(changed_fields, final_payload) if changed_fields else []
            
            log_json("CHANGED_FIELDS_MAPPING", {
                "original_changed_fields": changed_fields,
                "mapped_changed_fields": mapped_changed_fields,
                "final_payload_fields": list(final_payload.keys()),
                "event_type": event_type
            })
            
            event_envelope = {
                "webhook_source": "sportivity",
                "tenant_id": envelope.get("tenant_id"),
                "event_type": event_type,
                "received_at": envelope.get("received_at") or envelope.get("receivedAt"),
                "event_id": envelope.get("event_id"),
                "timestamp": normalized_payload.get("EntryDate") or normalized_payload.get("timestamp") or normalized_payload.get("created_at"),
                "customer_id": str(customer_id) if customer_id else None,
                "email": customer_email,
                **display_fields,
                "campaign_source": envelope.get("campaign_source"),
                "traffic_source": envelope.get("traffic_source"),
                "page_source": envelope.get("page_source"),
                "product_interest": envelope.get("product_interest"),
                "brand": envelope.get("brand"),
                "changed_fields": mapped_changed_fields,
                "payload": final_payload
            }
            
            event_envelope = {k: v for k, v in event_envelope.items() if v is not None}
            
            if event_envelope.get("payload"):
                event_envelope["payload"] = {k: v for k, v in event_envelope["payload"].items() if v is not None}
            
            self.publish_to_events(event_envelope)
            return event_envelope
            
        except Exception as e:
            self.publish_error_event(f"Translation error: {e}", envelope)
            return None


@functions_framework.cloud_event
def sportivity_pipeline(cloud_event):
    """Gen2 Cloud Function entry point for Pub/Sub trigger with proper CloudEvent signature"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)
        
        payload = envelope.get("payload", {})
        log_json("INPUT", envelope, payload)
        
        webhook_source = envelope.get("webhook_source", "").lower()
        event_type = envelope.get("event_type", "").lower()
        
        if webhook_source != "sportivity":
            print(f"Skipping non-sportivity event: {webhook_source}")
            return "OK"
        
        if not event_type:
            print(f"Skipping event with no event_type")
            return "OK"
        
        # Updated supported event types for consolidated enricher
        supported_event_types = {
            "customer_update", "subscription_update", 
            "visit", "suspension", "addon"
        }
        
        if event_type not in supported_event_types:
            print(f"Unsupported event type: {event_type}. Supported types: {supported_event_types}")
            return "OK"
        
        project_id = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
        translator = SportivityTranslator(project_id)
        
        translated_envelope = translator.translate_to_events(envelope)
        
        if translated_envelope:
            final_payload = translated_envelope.get("payload", {})
            log_json("TO_EVENTS", translated_envelope, final_payload)
        else:
            print(f"No translation output for event: {event_type}")
        
        return "OK"
        
    except Exception as e:
        import traceback
        error_msg = f"Error processing sportivity translation: {str(e)}"
        print(error_msg)
        print(f"Traceback: {traceback.format_exc()}")
        
        try:
            envelope_for_error = json.loads(raw) if 'raw' in locals() else None
        except:
            envelope_for_error = None
            
        try:
            translator = SportivityTranslator(os.environ.get("GCP_PROJECT", "solid-future-452906-a2"))
            translator.publish_error_event(error_msg, envelope_for_error)
        except:
            pass
            
        raise
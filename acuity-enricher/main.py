import base64
import json
import os
import requests
from google.cloud import pubsub_v1, bigquery, firestore
from datetime import datetime
import functions_framework

# === CONFIG ===
PROJECT_ID = os.environ.get("GCP_PROJECT") or "solid-future-452906-a2"
TOPIC_NAME = "acuity-translations"
EVENTS_TOPIC_NAME = "events"

bigquery_client = bigquery.Client()
firestore_client = firestore.Client()


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def get_acuity_credentials(tenant_id):
    try:
        tenant_doc = firestore_client.collection('tenants').document(tenant_id).get()
        if not tenant_doc.exists:
            raise Exception(f"Tenant not found: {tenant_id}")
        tenant_data = tenant_doc.to_dict()
        acuity_config = tenant_data.get('acuityConfig', {})
        api_key = acuity_config.get('apiKey')
        user_id = acuity_config.get('userId')
        if not api_key or not user_id:
            raise Exception(f"Missing Acuity credentials for tenant {tenant_id}")
        return {
            'api_key': api_key,
            'user_id': user_id,
            'lead_appointment_type_ids': [int(i) for i in acuity_config.get('leadAppointmentTypeIds', [])]
        }
    except Exception as e:
        print(f"❌ Credential error: {e}")
        raise


def fetch_acuity_data(appointment_id, credentials):
    try:
        url = f"https://acuityscheduling.com/api/v1/appointments/{appointment_id}"
        auth = (credentials['user_id'], credentials['api_key'])
        headers = {'Accept': 'application/json'}
        response = requests.get(url, auth=auth, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise Exception(f"Appointment {appointment_id} not found")
        elif response.status_code == 401:
            raise Exception("Acuity API authentication failed")
        elif response.status_code == 403:
            raise Exception(f"Acuity API plan limitation: {response.text}")
        else:
            raise Exception(f"Acuity API error: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Acuity API request failed: {e}")


def normalize_phone(phone):
    if not phone:
        return None
    return ''.join(filter(str.isdigit, phone))


def lookup_customer_in_bigquery(email, phone, tenant_id):
    try:
        if email and email.strip():
            query = f"""
            SELECT customer_id, email
            FROM `{PROJECT_ID}.gym_analytics.customers`
            WHERE tenant_id = @tenant_id
              AND email = @email
              AND customer_id IS NOT NULL
            ORDER BY
              CASE WHEN subscription_active = TRUE THEN 1 ELSE 2 END,
              member_since ASC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                bigquery.ScalarQueryParameter("email", "STRING", email.strip().lower()),
            ])
            results = bigquery_client.query(query, job_config=job_config).result()
            for row in results:
                log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
                    "lookup_method": "email",
                    "customer_found": True,
                    "customer_id": row.customer_id,
                    "email_from_bigquery": None
                })
                return {"customer_id": row.customer_id, "email": row.email}

        clean_phone = normalize_phone(phone)
        if clean_phone:
            query = f"""
            SELECT customer_id, email
            FROM `{PROJECT_ID}.gym_analytics.customers`
            WHERE tenant_id = @tenant_id
              AND REGEXP_REPLACE(phone_number, r'[^0-9]', '') = @clean_phone
              AND customer_id IS NOT NULL
            ORDER BY
              CASE WHEN subscription_active = TRUE THEN 1 ELSE 2 END,
              member_since ASC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
                bigquery.ScalarQueryParameter("clean_phone", "STRING", clean_phone),
            ])
            results = bigquery_client.query(query, job_config=job_config).result()
            for row in results:
                log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
                    "lookup_method": "phone_fallback",
                    "customer_found": True,
                    "customer_id": row.customer_id,
                    "email_from_bigquery": row.email
                })
                return {"customer_id": row.customer_id, "email": row.email}

        log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
            "lookup_method": "no_match",
            "customer_found": False,
            "customer_id": None,
            "email_from_bigquery": None
        })
        return None

    except Exception as e:
        print(f"❌ BigQuery error: {e}")
        raise


def preserve_business_context(envelope):
    business_fields = ['traffic_source', 'page_source', 'product_interest', 'campaign_source', 'brand']
    return {field: envelope[field] for field in business_fields if field in envelope and envelope[field] is not None}


def publish_unknown_email_task(publisher, tenant_id, appointment_data, envelope):
    """Publish a CRM task to #taken when a non-lead appointment has no matching customer."""
    try:
        firstname = appointment_data.get('firstName', '')
        lastname = appointment_data.get('lastName', '')
        full_name = f"{firstname} {lastname}".strip() or 'Onbekend'
        email = appointment_data.get('email', '')
        phone = appointment_data.get('phone', '')
        appointment_type = appointment_data.get('type', '')
        appointment_datetime = appointment_data.get('datetime', '')

        details = []
        if email:
            details.append({"label": "Email in Acuity", "value": email})
        if phone:
            details.append({"label": "Telefoon", "value": phone})
        if appointment_type:
            details.append({"label": "Afspraaktype", "value": appointment_type})
        if appointment_datetime:
            details.append({"label": "Afspraak", "value": appointment_datetime})

        task_envelope = {
            "webhook_source": "customerio",
            "tenant_id": tenant_id,
            "event_type": "crm_task",
            "received_at": datetime.utcnow().isoformat(),
            "customer_id": None,
            "email": email,
            "task_type": "member_admin",
            "payload": {
                "task_type": "member_admin",
                "action_type": "contact",
                "valid_minutes": 1440,
                "subject": full_name,
                "task_title": "Onbekend e-mailadres",
                "task_icon": "📋",
                "task_label": "Gebruik e-mailadres uit Sportivity in Acuity",
                "note": "Afspraak ingepland maar e-mailadres niet bekend in Sportivity.",
                "details": details,
                "visible": True
            }
        }

        events_topic_path = publisher.topic_path(PROJECT_ID, EVENTS_TOPIC_NAME)
        future = publisher.publish(events_topic_path, json.dumps(task_envelope).encode("utf-8"))
        message_id = future.result()

        log_json("TO_EVENTS_UNKNOWN_EMAIL_TASK", {
            "envelope": task_envelope,
            "payload": task_envelope["payload"],
            "message_id": message_id
        })

    except Exception as e:
        print(f"❌ Error publishing unknown email task: {e}")


def create_error_envelope(original_envelope, error_message):
    tenant_id = original_envelope.get("tenant_id")
    timestamp = int(datetime.utcnow().timestamp())
    error_payload = {
        "id": f"error_{timestamp}",
        "type": "enrichment_error",
        "error_message": error_message,
        "service": "acuity-enricher",
        "original_appointment_id": original_envelope.get("payload", {}).get("id"),
        "enriched": False,
        "enrichedAt": timestamp
    }
    business_context = preserve_business_context(original_envelope)
    error_envelope = {
        "webhook_source": "acuity",
        "tenant_id": tenant_id,
        "event_type": "error",
        "receivedAt": original_envelope.get("receivedAt"),
        "enrichedAt": timestamp,
        **business_context,
        "payload": error_payload
    }
    if original_envelope.get("email"):
        error_envelope["email"] = original_envelope.get("email")
    return error_envelope


@functions_framework.cloud_event
def acuity_enricher(cloud_event):
    """Main Cloud Function entry point - Gen 2 CloudEvent format"""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        webhook_source = envelope.get("webhook_source", "").lower()
        if webhook_source != "acuity":
            return

        tenant_id = envelope.get("tenant_id")
        payload = envelope.get("payload", {})
        appointment_id = payload.get("id")

        if not tenant_id:
            raise Exception("Missing tenant_id")
        if not appointment_id:
            raise Exception("Missing appointment ID")

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(PROJECT_ID, TOPIC_NAME)

        try:
            credentials = get_acuity_credentials(tenant_id)
            lead_appointment_type_ids = credentials.get('lead_appointment_type_ids', [])

            appointment_data = fetch_acuity_data(appointment_id, credentials)

            email = appointment_data.get("email")
            phone = appointment_data.get("phone")
            appointment_type_id = appointment_data.get("appointmentTypeID")

            log_json("ENRICHMENT_ACUITY_API", {
                "appointment_id": appointment_id,
                "success": True,
                "has_email": bool(email),
                "has_phone": bool(phone),
                "appointment_type": appointment_data.get("type"),
                "appointment_type_id": appointment_type_id,
                "calendar": appointment_data.get("calendar")
            })

            envelope_email = email
            envelope_phone = phone
            envelope_customer_id = None
            lookup_method = None

            if email:
                customer_data = lookup_customer_in_bigquery(email, phone, tenant_id)
                lookup_method = "email" + ("+phone" if phone else "")
                if customer_data:
                    envelope_customer_id = customer_data.get("customer_id")
            elif phone:
                customer_data = lookup_customer_in_bigquery(None, phone, tenant_id)
                lookup_method = "phone_only"
                if customer_data:
                    envelope_customer_id = customer_data.get("customer_id")
                    envelope_email = customer_data.get("email")
            else:
                log_json("APPOINTMENT_REJECTED_NO_IDENTIFIER", {
                    "appointment_id": appointment_id,
                    "appointment_type": appointment_data.get("type"),
                    "reason": "geen email of telefoonnummer — afspraak niet verwerkt"
                })
                return

            log_json("ENRICHMENT_BIGQUERY_LOOKUP", {
                "lookup_method": lookup_method,
                "customer_found": bool(customer_data),
                "customer_id": envelope_customer_id,
                "email_from_bigquery": envelope_email if customer_data and not email else None
            })

            # Signal unknown email for non-lead appointments without customer_id
            is_lead_appointment = int(appointment_type_id) in lead_appointment_type_ids if appointment_type_id else False
            if not envelope_customer_id and not is_lead_appointment:
                log_json("UNKNOWN_EMAIL_DETECTED", {
                    "appointment_type_id": appointment_type_id,
                    "appointment_type": appointment_data.get("type"),
                    "email": email,
                    "is_lead_appointment": False
                })
                publish_unknown_email_task(publisher, tenant_id, appointment_data, envelope)

            enriched_payload = appointment_data.copy()
            enriched_payload.update({
                "is_known_customer": bool(envelope_customer_id),
                "gym_customer_id": envelope_customer_id,
                "gym_customer_email": envelope_email,
                "enriched": True,
                "enrichedAt": int(datetime.utcnow().timestamp())
            })

            log_json("ENRICHMENT_RESULT", {
                "is_known_customer": bool(envelope_customer_id),
                "customer_id": envelope_customer_id,
                "final_email": envelope_email,
                "is_lead_appointment": is_lead_appointment
            })

            business_context = preserve_business_context(envelope)
            enriched_envelope = {
                "webhook_source": envelope.get("webhook_source"),
                "tenant_id": tenant_id,
                "event_type": envelope.get("event_type"),
                "receivedAt": envelope.get("receivedAt"),
                "enrichedAt": int(datetime.utcnow().timestamp()),
                **business_context,
                "payload": enriched_payload
            }

            if envelope_customer_id:
                enriched_envelope["customer_id"] = envelope_customer_id
            if envelope_email:
                enriched_envelope["email"] = envelope_email
            if envelope_phone:
                enriched_envelope["phone"] = envelope_phone

            log_json("TO_ACUITY-TRANSLATIONS", {
                "envelope": enriched_envelope,
                "payload": enriched_payload
            })

            future = publisher.publish(topic_path, json.dumps(enriched_envelope).encode("utf-8"))
            future.result()

        except Exception as enrichment_error:
            print(f"❌ ENRICHMENT ERROR: {str(enrichment_error)}")
            import traceback
            print(f"❌ STACK TRACE: {traceback.format_exc()}")
            error_envelope = create_error_envelope(envelope, str(enrichment_error))
            log_json("ERROR_OUTPUT", error_envelope)
            future = publisher.publish(topic_path, json.dumps(error_envelope).encode("utf-8"))
            future.result()

    except Exception as e:
        print(f"❌ CRITICAL ERROR: {str(e)}")
        import traceback
        print(f"❌ STACK TRACE: {traceback.format_exc()}")
        raise
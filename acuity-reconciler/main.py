import json
import os
import uuid
import requests
import functions_framework
from datetime import datetime, timezone, timedelta
from google.cloud import pubsub_v1, firestore, bigquery

PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
ACUITY_ENRICHMENTS_TOPIC = "acuity-enrichments"

firestore_client = firestore.Client()
bq_client = bigquery.Client()
publisher = pubsub_v1.PublisherClient()


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def get_tenants_with_acuity():
    docs = firestore_client.collection("tenants").stream()
    tenants = []
    for doc in docs:
        data = doc.to_dict()
        cfg = data.get("acuityConfig", {})
        if cfg.get("apiKey") and cfg.get("userId"):
            tenants.append({
                "tenant_id": doc.id,
                "user_id": cfg["userId"],
                "api_key": cfg["apiKey"],
            })
    return tenants


def fetch_acuity_appointments(user_id, api_key, min_date, max_date):
    """Fetch all appointments from Acuity within a date range."""
    try:
        resp = requests.get(
            "https://acuityscheduling.com/api/v1/appointments",
            auth=(user_id, api_key),
            params={
                "min": min_date,
                "max": max_date,
                "max_results": 1000,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"❌ Acuity API error: {e}")
        return []


def get_known_confirmation_pages(tenant_id, min_date, max_date):
    """Return the set of confirmation_pages already in BigQuery raw_events."""
    query = """
        SELECT DISTINCT JSON_VALUE(raw_payload, '$.confirmation_page') AS confirmation_page
        FROM `solid-future-452906-a2.gym_analytics.raw_events`
        WHERE webhook_source = 'acuity'
          AND event_type = 'appointment'
          AND tenant_id = @tenant_id
          AND TIMESTAMP(JSON_VALUE(raw_payload, '$.start_at')) BETWEEN @min_ts AND @max_ts
          AND JSON_VALUE(raw_payload, '$.confirmation_page') IS NOT NULL
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
        bigquery.ScalarQueryParameter("min_ts", "TIMESTAMP", min_date + "T00:00:00Z"),
        bigquery.ScalarQueryParameter("max_ts", "TIMESTAMP", max_date + "T23:59:59Z"),
    ])
    rows = bq_client.query(query, job_config=job_config).result()
    return {row.confirmation_page for row in rows if row.confirmation_page}


def publish_appointment_to_enricher(tenant_id, appointment):
    """Publish a minimal appointment envelope to acuity-enrichments so the enricher processes it."""
    envelope = {
        "webhook_source": "acuity",
        "tenant_id": tenant_id,
        "event_type": "appointment",
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "event_id": str(uuid.uuid4()),
        "reconciled": True,
        "payload": {
            "id": appointment["id"],
            "action": "scheduled",
        },
    }
    topic_path = publisher.topic_path(PROJECT_ID, ACUITY_ENRICHMENTS_TOPIC)
    future = publisher.publish(topic_path, json.dumps(envelope).encode("utf-8"))
    future.result(timeout=30)
    return envelope["event_id"]


@functions_framework.http
def acuity_reconciler(request):
    """
    Nightly reconciliation: fetch Acuity appointments, compare with BigQuery,
    publish missing ones to the enricher pipeline.

    Triggered by Cloud Scheduler (HTTP).
    Window: yesterday to +180 days (catches recently booked future appointments).
    """
    try:
        now = datetime.now(timezone.utc)
        min_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        max_date = (now + timedelta(days=180)).strftime("%Y-%m-%d")

        tenants = get_tenants_with_acuity()
        log_json("RECONCILER_START", {
            "tenants": len(tenants),
            "window": f"{min_date} → {max_date}",
        })

        total_checked = 0
        total_missing = 0
        total_published = 0

        for tenant in tenants:
            tenant_id = tenant["tenant_id"]

            appointments = fetch_acuity_appointments(
                tenant["user_id"], tenant["api_key"], min_date, max_date
            )
            if not appointments:
                continue

            # Only consider active (non-cancelled) appointments
            active = [a for a in appointments if not a.get("canceled")]

            known_pages = get_known_confirmation_pages(tenant_id, min_date, max_date)

            missing = [
                a for a in active
                if a.get("confirmationPage") and a["confirmationPage"] not in known_pages
            ]

            total_checked += len(active)
            total_missing += len(missing)

            for appt in missing:
                try:
                    event_id = publish_appointment_to_enricher(tenant_id, appt)
                    log_json("RECONCILER_PUBLISHED", {
                        "tenant_id": tenant_id,
                        "appointment_id": appt["id"],
                        "type": appt.get("type"),
                        "datetime": appt.get("datetime"),
                        "email": appt.get("email"),
                        "event_id": event_id,
                    })
                    total_published += 1
                except Exception as e:
                    log_json("RECONCILER_PUBLISH_ERROR", {
                        "tenant_id": tenant_id,
                        "appointment_id": appt.get("id"),
                        "error": str(e),
                    })

        summary = {
            "status": "success",
            "window": f"{min_date} → {max_date}",
            "tenants_processed": len(tenants),
            "appointments_checked": total_checked,
            "appointments_missing": total_missing,
            "appointments_published": total_published,
        }
        log_json("RECONCILER_DONE", summary)
        return summary, 200

    except Exception as e:
        print(f"❌ RECONCILER ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "error": str(e)}, 500

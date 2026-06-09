import base64
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import functions_framework
from google.cloud import bigquery, firestore
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import vertexai
from vertexai.generative_models import GenerativeModel

# Configuration
PROJECT_ID = os.environ.get("GCP_PROJECT", "solid-future-452906-a2")
REGION = "europe-west1"
REPORT_CHANNEL_ID = "C0B7NK3240K"
DENNIS_SLACK_ID = "D1KPR28A0"
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# Clients
bq_client = bigquery.Client()
firestore_client = firestore.Client()
slack_clients = {}

vertexai.init(project=PROJECT_ID, location=REGION)


def log_json(label, data):
    print(f"{label}: {json.dumps(data, default=str)}")


def get_slack_client(tenant_id):
    global slack_clients
    if tenant_id in slack_clients:
        return slack_clients[tenant_id]
    try:
        tenant_doc = firestore_client.collection("tenants").document(tenant_id).get()
        if not tenant_doc.exists:
            return None
        bot_token = tenant_doc.to_dict().get('slack_bot_token')
        if not bot_token or not bot_token.startswith('xoxb-'):
            return None
        client = WebClient(token=bot_token)
        slack_clients[tenant_id] = client
        return client
    except Exception as e:
        print(f"❌ Error creating Slack client: {e}")
        return None


def notify_dennis(client, error_description, context=None):
    try:
        context_text = f"\nContext: {json.dumps(context, default=str)}" if context else ""
        message = f"⚠️ *team-report*\n*<!here> Error:* *{error_description}*{context_text}"
        client.chat_postMessage(channel=DENNIS_SLACK_ID, text=message, mrkdwn=True)
    except Exception as e:
        print(f"❌ Failed to notify Dennis: {e}")


def get_daypart_window(daypart, report_date):
    """Return start and end hours for a daypart."""
    if daypart == 'ochtend':
        return 7, 12
    elif daypart == 'middag':
        return 16, 21
    return None, None


def query_task_performance(tenant_id, daypart, report_date):
    """Query task performance aggregates for the daypart."""
    start_hour, end_hour = get_daypart_window(daypart, report_date)

    query = f"""
    WITH daypart_tasks AS (
      SELECT
        task_type,
        completed,
        expired,
        response_time_minutes
      FROM `{PROJECT_ID}.gym_analytics.task_performance`
      WHERE tenant_id = @tenant_id
        AND task_date = @report_date
        AND daypart = @daypart
        AND task_type NOT IN ('reboot', 'evaluation', 'subscription_change')
    ),
    benchmark AS (
      SELECT
        ROUND(COUNTIF(completed) / NULLIF(COUNT(*), 0) * 100, 0) AS benchmark_completion_rate
      FROM `{PROJECT_ID}.gym_analytics.task_performance`
      WHERE tenant_id = @tenant_id
        AND daypart = @daypart
        AND task_date BETWEEN DATE_SUB(@report_date, INTERVAL 28 DAY) AND DATE_SUB(@report_date, INTERVAL 1 DAY)
        AND EXTRACT(DAYOFWEEK FROM task_date) = EXTRACT(DAYOFWEEK FROM DATE @report_date)
        AND task_type NOT IN ('reboot', 'evaluation', 'subscription_change')
    )
    SELECT
      t.task_type,
      COUNT(*) AS total,
      COUNTIF(t.completed) AS completed_count,
      COUNTIF(t.expired AND NOT t.completed) AS expired_count,
      ROUND(AVG(CASE WHEN t.completed THEN t.response_time_minutes END), 0) AS avg_response_time,
      ROUND(COUNTIF(t.completed) / NULLIF(COUNT(*), 0) * 100, 0) AS completion_rate,
      b.benchmark_completion_rate
    FROM daypart_tasks t
    CROSS JOIN benchmark b
    GROUP BY t.task_type, b.benchmark_completion_rate
    ORDER BY t.task_type
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ScalarQueryParameter("report_date", "DATE", report_date.strftime('%Y-%m-%d')),
            bigquery.ScalarQueryParameter("daypart", "STRING", daypart),
        ]
    )

    results = bq_client.query(query, job_config=job_config).result()
    return [dict(row) for row in results]


def query_appointments(tenant_id, daypart, report_date):
    """Query appointment aggregates for the daypart."""
    query = f"""
    SELECT
      activity,
      COUNT(*) AS total,
      SUM(duration_minutes) AS total_minutes,
      COUNTIF(followup_scheduled = TRUE) AS followup_count,
      COUNTIF(followup_scheduled IS NOT NULL) AS followup_applicable
    FROM `{PROJECT_ID}.gym_analytics.appointments`
    WHERE tenant_id = @tenant_id
      AND appointment_date = @report_date
      AND daypart = @daypart
    GROUP BY activity
    ORDER BY total DESC
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ScalarQueryParameter("report_date", "DATE", report_date.strftime('%Y-%m-%d')),
            bigquery.ScalarQueryParameter("daypart", "STRING", daypart),
        ]
    )

    results = bq_client.query(query, job_config=job_config).result()
    return [dict(row) for row in results]


def get_prompts(tenant_id):
    """Load report prompts from Firestore config."""
    try:
        doc = firestore_client.collection("config").document("team_report_prompt").get()
        if doc.exists:
            data = doc.to_dict()
            return data.get('management_prompt', ''), data.get('employee_prompt', '')
    except Exception as e:
        print(f"❌ Error loading prompts: {e}")
    return None, None


def build_data_summary(tasks, appointments, daypart, report_date):
    """Build a structured data summary for Gemini."""
    dutch_day = ['zondag', 'maandag', 'dinsdag', 'woensdag', 'donderdag', 'vrijdag', 'zaterdag']
    day_name = dutch_day[report_date.weekday() + 1 if report_date.weekday() < 6 else 0]
    daypart_label = 'ochtend (07:00-12:00)' if daypart == 'ochtend' else 'middag (16:00-21:00)'

    summary = {
        "date": report_date.strftime('%d-%m-%Y'),
        "day": day_name,
        "daypart": daypart_label,
        "tasks": [],
        "appointments": [],
        "totals": {}
    }

    # Task totals
    total_tasks = sum(t['total'] for t in tasks)
    total_completed = sum(t['completed_count'] for t in tasks)
    total_expired = sum(t['expired_count'] for t in tasks)
    benchmark = tasks[0]['benchmark_completion_rate'] if tasks else None
    overall_rate = round(total_completed / total_tasks * 100) if total_tasks > 0 else 0

    summary['totals'] = {
        "task_volume": total_tasks,
        "completion_rate": overall_rate,
        "expiry_count": total_expired,
        "benchmark_completion_rate": benchmark
    }

    for t in tasks:
        summary['tasks'].append({
            "task_type": t['task_type'],
            "total": t['total'],
            "completed": t['completed_count'],
            "expired": t['expired_count'],
            "completion_rate": t['completion_rate'],
            "avg_response_time_minutes": t['avg_response_time']
        })

    # Appointment totals
    total_appointments = sum(a['total'] for a in appointments)
    total_minutes = sum(a['total_minutes'] or 0 for a in appointments)
    total_followup_applicable = sum(a['followup_applicable'] or 0 for a in appointments)
    total_followup_scheduled = sum(a['followup_count'] or 0 for a in appointments)

    summary['totals']['appointment_volume'] = total_appointments
    summary['totals']['appointment_minutes'] = total_minutes
    summary['totals']['followup_scheduled'] = total_followup_scheduled
    summary['totals']['followup_applicable'] = total_followup_applicable

    for a in appointments:
        entry = {
            "activity": a['activity'],
            "total": a['total'],
            "total_minutes": a['total_minutes']
        }
        if a['followup_applicable'] and a['followup_applicable'] > 0:
            entry['followup_scheduled'] = a['followup_count']
            entry['followup_applicable'] = a['followup_applicable']
        summary['appointments'].append(entry)

    return summary


def call_gemini(prompt, data_summary):
    """Call Gemini with the prompt and data summary."""
    model = GenerativeModel("gemini-2.0-flash-lite")
    full_prompt = f"{prompt}\n\nData:\n{json.dumps(data_summary, ensure_ascii=False, indent=2)}"
    response = model.generate_content(full_prompt)
    return response.text


def post_to_slack(client, message, tenant_id):
    """Post a message to the report channel."""
    try:
        client.chat_postMessage(
            channel=REPORT_CHANNEL_ID,
            text=message,
            mrkdwn=True
        )
    except SlackApiError as e:
        if e.response['error'] == 'not_in_channel':
            client.conversations_join(channel=REPORT_CHANNEL_ID)
            client.chat_postMessage(
                channel=REPORT_CHANNEL_ID,
                text=message,
                mrkdwn=True
            )
        else:
            raise


@functions_framework.cloud_event
def team_report(cloud_event):
    """Gen 2 Pub/Sub triggered team report function."""
    try:
        message_data = cloud_event.data
        raw = base64.b64decode(message_data['message']['data']).decode('utf-8')
        envelope = json.loads(raw)

        log_json("INPUT", envelope)

        tenant_id = envelope.get('tenant_id')
        daypart = envelope.get('daypart')

        if not tenant_id or not daypart:
            print("❌ Missing tenant_id or daypart")
            return "OK"

        # Report date is today in Amsterdam time
        now_amsterdam = datetime.now(AMSTERDAM_TZ)
        report_date = now_amsterdam.date()

        # Query data
        tasks = query_task_performance(tenant_id, daypart, report_date)
        appointments = query_appointments(tenant_id, daypart, report_date)

        log_json("QUERY_TASKS", {"count": len(tasks), "tasks": tasks})
        log_json("QUERY_APPOINTMENTS", {"count": len(appointments), "appointments": appointments})

        if not tasks and not appointments:
            print(f"No data for {daypart} on {report_date}")
            return "OK"

        # Build data summary
        data_summary = build_data_summary(tasks, appointments, daypart, report_date)
        log_json("DATA_SUMMARY", data_summary)

        # Load prompts
        management_prompt, employee_prompt = get_prompts(tenant_id)

        client = get_slack_client(tenant_id)
        if not client:
            print(f"❌ No Slack client for tenant {tenant_id}")
            return "OK"

        # Management report
        if management_prompt:
            management_message = call_gemini(management_prompt, data_summary)
            post_to_slack(client, management_message, tenant_id)
            log_json("MANAGEMENT_REPORT_SENT", {"daypart": daypart, "date": str(report_date)})
        else:
            print("⚠️ No management prompt configured")

        # Employee report
        if employee_prompt:
            employee_message = call_gemini(employee_prompt, data_summary)
            post_to_slack(client, employee_message, tenant_id)
            log_json("EMPLOYEE_REPORT_SENT", {"daypart": daypart, "date": str(report_date)})
        else:
            print("⚠️ No employee prompt configured")

        return "OK"

    except Exception as e:
        import traceback
        print(f"❌ Error: {e}")
        print(f"🐛 {traceback.format_exc()}")
        try:
            client = get_slack_client(envelope.get('tenant_id', ''))
            if client:
                notify_dennis(client, str(e), context={"daypart": envelope.get('daypart')})
        except:
            pass
        raise
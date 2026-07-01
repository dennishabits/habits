import functions_framework
import json
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from google.cloud import firestore, pubsub_v1
from google.genai import Client as GenaiClient

# Clients
fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()

PROJECT_ID = "solid-future-452906-a2"
GEMINI_MODEL = "gemini-2.5-flash"
SERVICE_NAME = "agent-quality-reviewer"


def log(data: dict):
    sys.stdout.write(json.dumps(data, default=str) + "\n")
    sys.stdout.flush()


def publish_error_event(error_description: str):
    try:
        topic_path = publisher.topic_path(PROJECT_ID, "events")
        payload = {
            "envelope": {
                "webhook_source": SERVICE_NAME,
                "event_type": "service_error",
                "tenant_id": "system"
            },
            "payload": {
                "service": SERVICE_NAME,
                "error": f"**{error_description}**",
                "notification_email": "dennis@habits.fit",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        publisher.publish(topic_path, json.dumps(payload).encode("utf-8"))
    except Exception as e:
        log({"PUBLISH_ERROR_FAILED": str(e)})


# ── DATA COLLECTION ───────────────────────────────────────────────────────────

def load_error_logs(since: datetime) -> list[dict]:
    """Load all error_log docs created since the given timestamp."""
    docs = fs_client.collection("error_log") \
        .where(filter=firestore.FieldFilter("created_at", ">=", since.isoformat())) \
        .stream()
    results = []
    for doc in docs:
        d = doc.to_dict()
        d["_doc_id"] = doc.id
        results.append(d)
    return results


def load_previous_review() -> dict | None:
    """Load the most recent agent_reviews doc, if any."""
    docs = fs_client.collection("agent_reviews") \
        .order_by("run_date", direction=firestore.Query.DESCENDING) \
        .limit(1) \
        .stream()
    for doc in docs:
        d = doc.to_dict()
        d["_doc_id"] = doc.id
        return d
    return None


# ── DETERMINISTIC AGGREGATION ─────────────────────────────────────────────────

def aggregate(logs: list[dict]) -> dict:
    """
    Compute deterministic metrics from error_log documents.
    Returns a dict of plain numbers/strings — no PII, no raw signal text.
    """
    total = len(logs)
    if total == 0:
        return {
            "total_logs": 0,
            "signal_type_distribution": {},
            "unclear_share": None,
            "root_cause_distribution": {},
            "confidence_calibration": None,
            "avg_resolution_minutes": None,
            "recurrence_by_category": {},
        }

    # Signal type distribution
    signal_counts = defaultdict(int)
    for doc in logs:
        signal_counts[doc.get("signal_type") or "unknown"] += 1
    unclear_share = round(signal_counts.get("unclear", 0) / total, 3)

    # Root cause distribution (discrepancy logs only)
    discrepancy_logs = [d for d in logs if d.get("signal_type") == "discrepancy"]
    root_cause_counts = defaultdict(int)
    for doc in discrepancy_logs:
        root_cause_counts[doc.get("root_cause_category") or "unknown"] += 1

    # Confidence calibration: of all high-confidence agent auto-resolves,
    # what fraction was later marked reopened: true?
    auto_resolved_high = [
        d for d in discrepancy_logs
        if d.get("confidence") == "high" and d.get("approved_by") == "agent"
    ]
    reopened_count = sum(1 for d in auto_resolved_high if d.get("reopened") is True)
    calibration = None
    if auto_resolved_high:
        calibration = {
            "high_confidence_auto_resolves": len(auto_resolved_high),
            "reopened": reopened_count,
            "reopened_rate": round(reopened_count / len(auto_resolved_high), 3),
        }

    # Average time to resolution (minutes) for resolved discrepancy logs
    resolution_times = []
    for doc in discrepancy_logs:
        created = doc.get("created_at")
        resolved = doc.get("resolved_at")
        if created and resolved:
            try:
                t0 = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(resolved).replace("Z", "+00:00"))
                resolution_times.append((t1 - t0).total_seconds() / 60)
            except Exception:
                pass
    avg_resolution = round(sum(resolution_times) / len(resolution_times), 1) if resolution_times else None

    # Recurrence: count distinct tenant occurrences per root_cause_category
    recurrence = defaultdict(lambda: defaultdict(int))
    for doc in discrepancy_logs:
        cat = doc.get("root_cause_category") or "unknown"
        tenant = doc.get("tenant_id") or "unknown"
        recurrence[cat][tenant] += 1
    recurrence_summary = {
        cat: {"total": sum(v.values()), "tenants": len(v)}
        for cat, v in recurrence.items()
    }

    return {
        "total_logs": total,
        "discrepancy_logs": len(discrepancy_logs),
        "signal_type_distribution": dict(signal_counts),
        "unclear_share": unclear_share,
        "root_cause_distribution": dict(root_cause_counts),
        "confidence_calibration": calibration,
        "avg_resolution_minutes": avg_resolution,
        "recurrence_by_category": recurrence_summary,
    }


def compare_with_previous(metrics: dict, previous_review: dict | None) -> dict | None:
    """Compare key metrics against the previous run to detect improvement or regression."""
    if not previous_review:
        return None
    prev_metrics = previous_review.get("metrics", {})
    prev_proposals = previous_review.get("proposals", [])

    changes = {}

    prev_unclear = prev_metrics.get("unclear_share")
    curr_unclear = metrics.get("unclear_share")
    if prev_unclear is not None and curr_unclear is not None:
        changes["unclear_share_delta"] = round(curr_unclear - prev_unclear, 3)

    prev_cal = prev_metrics.get("confidence_calibration") or {}
    curr_cal = metrics.get("confidence_calibration") or {}
    if prev_cal.get("reopened_rate") is not None and curr_cal.get("reopened_rate") is not None:
        changes["reopened_rate_delta"] = round(
            curr_cal["reopened_rate"] - prev_cal["reopened_rate"], 3
        )

    adopted_proposals = [p for p in prev_proposals if p.get("adopted") is True]
    changes["previous_adopted_proposal_count"] = len(adopted_proposals)

    return changes if changes else None


# ── LLM SYNTHESIS ─────────────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """
Je bent een kwaliteitsreviewer van een AI-agent die discrepanties in taken verwerkt voor gymbeheer.

Je krijgt geaggregeerde kwaliteitsmetrics over de afgelopen week — uitsluitend aantallen en categorieën, nooit persoonsdata.

Twee faalvlakken zijn relevant:
1. CLASSIFICATIE: het aandeel berichten dat als "unclear" is geclassificeerd (hoog = CLASSIFICATION_PROMPT presteert slecht).
2. DIAGNOSE: het aandeel hoge-confidence auto-resolves dat door de medewerker later is ontkend (reopened_rate hoog = INVESTIGATION_PROMPT of auto-resolve drempel presteert slecht).

Referentiewaarden:
- unclear_share > 0.15 (15%) = relevant probleem in classificatie
- reopened_rate > 0.10 (10%) = relevant probleem in diagnose
- Een delta ten opzichte van de vorige week toont trend (negatief = verbetering)

Doe het volgende:
- Analyseer de metrics op patronen
- Formuleer maximaal 2 concrete voorstellen voor verbetering — elk gericht op één van de twee faalvlakken
- Elk voorstel bevat: component ("classification" of "investigation"), type ("prompt_adjustment" of "threshold_adjustment"), rationale (één zin), en evidence_metric (het getal dat het probleem onderbouwt)
- Koppel elke conclusie aan een concreet getal uit de metrics — geen vage aanbevelingen
- Als er geen probleem is (beide metrics onder de drempel), geef dan 0 voorstellen terug

Geef ALLEEN een JSON object terug:
{
  "summary": "één zin samenvatting van de kwaliteitsstatus",
  "proposals": [
    {
      "component": "classification" | "investigation",
      "type": "prompt_adjustment" | "threshold_adjustment",
      "rationale": "één zin met de reden",
      "evidence_metric": "naam en waarde van het onderbouwende getal"
    }
  ]
}
"""


def synthesize(metrics: dict, comparison: dict | None, gemini_api_key: str) -> dict:
    """Call Gemini to synthesize metrics into proposals."""
    client = GenaiClient(api_key=gemini_api_key)

    user_content = json.dumps({
        "metrics": metrics,
        "comparison_with_previous_week": comparison,
    }, indent=2)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_content,
        config={
            "system_instruction": SYNTHESIS_PROMPT,
            "response_mime_type": "application/json",
        }
    )

    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


# ── AUDIT WRITE ───────────────────────────────────────────────────────────────

def write_review(run_date: str, metrics: dict, synthesis: dict, previous_doc_id: str | None) -> str:
    doc_id = f"review_{run_date.replace('-', '')}_{uuid.uuid4().hex[:6]}"
    fs_client.collection("agent_reviews").document(doc_id).set({
        "run_date": run_date,
        "metrics": metrics,
        "proposals": synthesis.get("proposals", []),
        "summary": synthesis.get("summary", ""),
        "previous_run_doc_id": previous_doc_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return doc_id


# ── SLACK REPORT ──────────────────────────────────────────────────────────────

def slack_dm_dennis(token: str, dennis_user_id: str, text: str):
    import urllib.request
    payload = json.dumps({
        "channel": dennis_user_id,
        "text": text,
        "unfurl_links": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        log({"SLACK_DM_ERROR": result.get("error")})


def format_slack_report(run_date: str, metrics: dict, synthesis: dict, review_doc_id: str) -> str:
    cal = metrics.get("confidence_calibration") or {}
    unclear_share = metrics.get("unclear_share")
    unclear_pct = f"{unclear_share * 100:.1f}%" if unclear_share is not None else "—"
    reopened_rate = cal.get("reopened_rate")
    reopened_pct = f"{reopened_rate * 100:.1f}%" if reopened_rate is not None else "—"

    proposals = synthesis.get("proposals", [])
    proposal_lines = "\n".join(
        f"• [{p['component'].upper()}] {p['rationale']} _(metric: {p['evidence_metric']})_"
        for p in proposals
    ) if proposals else "• Geen actie nodig — beide metrics onder drempel."

    return (
        f"*Wekelijkse agent-kwaliteitsreview — {run_date}*\n\n"
        f"*{synthesis.get('summary', '')}*\n\n"
        f"*Metrics*\n"
        f"• Logs geanalyseerd: {metrics.get('total_logs', 0)} ({metrics.get('discrepancy_logs', 0)} discrepancies)\n"
        f"• Unclear-aandeel: {unclear_pct}\n"
        f"• Confidence-fout (reopened rate): {reopened_pct} "
        f"(op basis van {cal.get('high_confidence_auto_resolves', 0)} hoge-confidence auto-resolves)\n"
        f"• Gemiddelde tijd tot resolutie: {metrics.get('avg_resolution_minutes', '—')} minuten\n\n"
        f"*Voorstellen*\n{proposal_lines}\n\n"
        f"_Review-doc: `{review_doc_id}` — markeer voorstellen als adopted in Firestore._"
    )


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

@functions_framework.cloud_event
def agent_quality_reviewer(cloud_event):
    log({"INPUT": {
        "trigger": SERVICE_NAME,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }})

    try:
        # Config
        tenants = list(fs_client.collection("tenants")
                       .where(filter=firestore.FieldFilter("active", "==", True))
                       .limit(1).stream())
        if not tenants:
            log({"NO_ACTIVE_TENANTS": True})
            return

        # Use first active tenant's Slack token and Dennis user ID for reporting
        tenant_data = tenants[0].to_dict()
        slack_token = tenant_data.get("slack_bot_token")
        dennis_user_id = tenant_data.get("slack_dennis_user_id", "U158QLHEF")

        gemini_api_key = __import__("os").environ.get("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY not set")

        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        since = datetime.now(timezone.utc) - timedelta(days=7)

        # 1. Load data
        logs = load_error_logs(since)
        previous_review = load_previous_review()
        log({"DATA_LOADED": {
            "error_logs": len(logs),
            "has_previous_review": previous_review is not None
        }})

        # 2. Deterministic aggregation
        metrics = aggregate(logs)
        comparison = compare_with_previous(metrics, previous_review)
        log({"METRICS": metrics})

        # 3. LLM synthesis
        synthesis = synthesize(metrics, comparison, gemini_api_key)
        log({"SYNTHESIS": synthesis})

        # 4. Write audit record
        review_doc_id = write_review(
            run_date=run_date,
            metrics=metrics,
            synthesis=synthesis,
            previous_doc_id=previous_review.get("_doc_id") if previous_review else None
        )
        log({"REVIEW_WRITTEN": {"doc_id": review_doc_id}})

        # 5. Report to Dennis
        if slack_token:
            report = format_slack_report(run_date, metrics, synthesis, review_doc_id)
            slack_dm_dennis(slack_token, dennis_user_id, report)
            log({"TO_SLACK_DENNIS": {"doc_id": review_doc_id}})
        else:
            log({"SLACK_SKIPPED": "no slack_bot_token on tenant"})

    except Exception as e:
        error_msg = f"Fatal error in {SERVICE_NAME}: {str(e)}"
        log({"FATAL_ERROR": str(e)})
        publish_error_event(error_msg)
        raise

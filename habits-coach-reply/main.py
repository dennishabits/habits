import functions_framework
import json
import os
import re
import sys
import itertools
from datetime import datetime, timezone, timedelta

import gower
import numpy as np
import pandas as pd
import requests
from scipy import stats
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist
import statsmodels.formula.api as smf
from google.cloud import bigquery, firestore, pubsub_v1
from google.genai import Client as GenAIClient

# Clients
bq_client = bigquery.Client()
fs_client = firestore.Client()
publisher = pubsub_v1.PublisherClient()

PROJECT_ID = "solid-future-452906-a2"
DATASET = "gym_analytics"
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
SESSION_EXPIRY_HOURS = 24

PHASE_ORDER = ["discover", "define", "research", "hmw"]


def log(data: dict):
    sys.stdout.write(json.dumps(data, default=json_serializable) + "\n")
    sys.stdout.flush()


def json_serializable(obj):
    if isinstance(obj, (np.integer, np.int64)):
        return int(obj)
    if isinstance(obj, (np.floating, np.float64)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Type {type(obj)} not serializable")


def publish_error_event(service_name: str, error_description: str):
    try:
        topic_path = publisher.topic_path(PROJECT_ID, "events")
        error_payload = {
            "envelope": {
                "webhook_source": service_name,
                "event_type": "service_error",
                "tenant_id": "system"
            },
            "payload": {
                "service": service_name,
                "error": f"**{error_description}**",
                "notification_email": "dennis@habits.fit",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        publisher.publish(topic_path, json.dumps(error_payload).encode("utf-8"))
    except Exception as e:
        log({"PUBLISH_ERROR_FAILED": str(e)})


# ── PROGRESS MESSAGE ──────────────────────────────────────────────────────────

class ProgressMessage:
    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel
        self.ts = None
        self.completed_steps = []

    def start(self, first_step: str):
        response = _slack_post(
            token=self.token,
            channel=self.channel,
            text=f"⏳ {first_step}..."
        )
        if response.get("ok"):
            self.ts = response["ts"]
            self.completed_steps.append(first_step)
        log({"PROGRESS_START": {"step": first_step, "ts": self.ts}})

    def update(self, next_step: str):
        if not self.ts:
            return
        lines = [f"✓ {step}" for step in self.completed_steps]
        lines.append(f"⏳ {next_step}...")
        self._edit("\n".join(lines))
        self.completed_steps.append(next_step)
        log({"PROGRESS_UPDATE": {"step": next_step}})

    def complete(self, final_text: str):
        if not self.ts:
            return
        self._edit(final_text)
        log({"PROGRESS_COMPLETE": {"ts": self.ts}})

    def _edit(self, text: str):
        session = requests.Session()
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
        session.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "channel": self.channel,
                "ts": self.ts,
                "text": text,
                "mrkdwn": True
            },
            timeout=10
        )


# ── FIRESTORE ────────────────────────────────────────────────────────────────

def get_model(doc_id: str, fallback: str) -> str:
    try:
        doc = fs_client.collection("config").document(doc_id).get()
        if doc.exists:
            return doc.to_dict().get("model", fallback)
    except Exception as e:
        log({"MODEL_FALLBACK": {"doc_id": doc_id, "error": str(e)}})
    return fallback


def get_system_prompt() -> str:
    doc = fs_client.collection("config").document("habits_coach_prompt").get()
    if doc.exists:
        return doc.to_dict().get("prompt", "")
    raise ValueError("habits_coach_prompt not found in Firestore config collection")


def get_tenant_by_team_id(slack_team_id: str) -> tuple:
    docs = fs_client.collection("tenants") \
        .where(filter=firestore.FieldFilter("slack_team_id", "==", slack_team_id)) \
        .limit(1) \
        .stream()
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return doc.id, data
    return None, None


def get_latest_session(tenant_id: str, slack_channel: str) -> tuple:
    docs = fs_client.collection("coaching_sessions") \
        .where(filter=firestore.FieldFilter("tenant_id", "==", tenant_id)) \
        .where(filter=firestore.FieldFilter("slack_channel", "==", slack_channel)) \
        .order_by("created_at", direction=firestore.Query.DESCENDING) \
        .limit(1) \
        .stream()
    for doc in docs:
        return doc.id, doc.to_dict()
    return None, None


def determine_session_action(session: dict | None, is_bot_trigger: bool) -> str:
    if is_bot_trigger:
        return "new"
    if not session:
        return "new"
    if session.get("current_phase") == "hmw":
        return "new"
    updated_at = session.get("updated_at")
    if updated_at:
        last_update = datetime.fromisoformat(updated_at)
        if datetime.now(timezone.utc) - last_update > timedelta(hours=SESSION_EXPIRY_HOURS):
            return "expired"
    return "continue"


# ── BIGQUERY ─────────────────────────────────────────────────────────────────

def get_opportunity(tenant_id: str, opportunity_nr: int) -> dict | None:
    query = f"""
        SELECT *
        FROM (
            SELECT
                tenant_id,
                journey,
                product,
                phase,
                member_count,
                opportunity_score,
                avg_hhi_gap,
                avg_hhi_actual,
                avg_hhi_benchmark,
                total_revenue,
                CAST(week_start_date AS STRING) AS week_start_date,
                ROW_NUMBER() OVER (ORDER BY opportunity_score DESC) AS opportunity_nr
            FROM `{PROJECT_ID}.{DATASET}.hhi_opportunities`
            WHERE tenant_id = @tenant_id
        )
        WHERE opportunity_nr = @opportunity_nr
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ScalarQueryParameter("opportunity_nr", "INT64", opportunity_nr)
        ]
    )
    results = list(bq_client.query(query, job_config=job_config).result())
    if not results:
        return None
    return dict(results[0])


def get_hhi_dataset(tenant_id: str, week_start_date: str, scope: dict) -> list:
    phase = scope.get("phase")
    phase_filter = "AND h.phase IS NULL" if phase is None else "AND h.phase = @phase"
    phase_params = [] if phase is None else [
        bigquery.ScalarQueryParameter("phase", "STRING", phase)
    ]

    scope_filters = ""
    scope_params = []
    for field in ["gender", "age_bucket", "tenure_bucket", "start_season"]:
        if scope.get(field):
            scope_filters += f" AND h.{field} = @{field}"
            scope_params.append(bigquery.ScalarQueryParameter(field, "STRING", scope[field]))

    query = f"""
        SELECT
            h.customer_id,
            h.tenant_id,
            CAST(h.week_start_date AS STRING) AS week_start_date,
            h.product,
            h.journey,
            h.phase,
            h.age_bucket,
            h.gender,
            h.tenure_bucket,
            h.start_season,
            h.rs_actual, h.rs_benchmark, h.rs_gap,
            h.css_actual, h.css_benchmark, h.css_gap,
            h.ps_actual, h.ps_benchmark, h.ps_gap,
            h.hhi_actual, h.hhi_benchmark, h.hhi_gap,
            h.hhi_status,
            h.subscription_price_month_ex_vat
        FROM `{PROJECT_ID}.{DATASET}.hhi_week` h
        WHERE h.tenant_id = @tenant_id
        AND h.week_start_date = @week_start_date
        AND h.journey = @journey
        AND h.product = @product
        {phase_filter}
        {scope_filters}
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ScalarQueryParameter("week_start_date", "DATE", week_start_date),
            bigquery.ScalarQueryParameter("journey", "STRING", scope["journey"]),
            bigquery.ScalarQueryParameter("product", "STRING", scope["product"]),
        ] + phase_params + scope_params
    )
    results = bq_client.query(query, job_config=job_config).result()
    return [dict(row) for row in results]


def get_visits_dataset(tenant_id: str, week_start_date: str, scope: dict) -> list:
    """Get visits data for the past 84 days (12 weeks) for pattern analysis."""
    phase = scope.get("phase")
    phase_filter = "AND h.phase IS NULL" if phase is None else "AND h.phase = @phase"
    phase_params = [] if phase is None else [
        bigquery.ScalarQueryParameter("phase", "STRING", phase)
    ]

    query = f"""
        SELECT
            v.customer_id,
            CAST(v.visit_date AS STRING) AS visit_date,
            CAST(v.week_start_date AS STRING) AS week_start_date,
            v.day_of_week,
            v.hour_of_day,
            v.subscription_name
        FROM `{PROJECT_ID}.{DATASET}.visits_week` v
        WHERE v.tenant_id = @tenant_id
        AND v.visit_date >= DATE_SUB(@week_start_date, INTERVAL 84 DAY)
        AND v.visit_date <= @week_start_date
        AND v.customer_id IN (
            SELECT h.customer_id
            FROM `{PROJECT_ID}.{DATASET}.hhi_week` h
            WHERE h.tenant_id = @tenant_id
            AND h.week_start_date = @week_start_date
            AND h.journey = @journey
            AND h.product = @product
            {phase_filter}
        )
        ORDER BY v.customer_id, v.visit_date
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ScalarQueryParameter("week_start_date", "DATE", week_start_date),
            bigquery.ScalarQueryParameter("journey", "STRING", scope["journey"]),
            bigquery.ScalarQueryParameter("product", "STRING", scope["product"]),
        ] + phase_params
    )
    results = bq_client.query(query, job_config=job_config).result()
    return [dict(row) for row in results]


def get_member_contacts(tenant_id: str, member_ids: list) -> list:
    if not member_ids:
        return []
    query = f"""
        SELECT customer_id, first_name, last_name, email, phone
        FROM `{PROJECT_ID}.{DATASET}.customers`
        WHERE tenant_id = @tenant_id
        AND CAST(customer_id AS STRING) IN UNNEST(@member_ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            bigquery.ArrayQueryParameter("member_ids", "STRING", [str(m) for m in member_ids])
        ]
    )
    results = bq_client.query(query, job_config=job_config).result()
    return [dict(row) for row in results]


# ── ANALYSIS 1: HHI SEGMENT ANALYSIS ─────────────────────────────────────────

def analyse_hhi_segments(hhi_data: list, opportunity_meta: dict) -> dict:
    df = pd.DataFrame(hhi_data)

    opportunity = {
        "journey": opportunity_meta.get("journey"),
        "phase": opportunity_meta.get("phase"),
        "product": opportunity_meta.get("product"),
        "member_count": int(opportunity_meta.get("member_count", 0)),
        "avg_hhi_gap": round(float(opportunity_meta.get("avg_hhi_gap", 0)), 1),
        "avg_hhi_actual": round(float(opportunity_meta.get("avg_hhi_actual", 0)), 1),
        "avg_hhi_benchmark": round(float(opportunity_meta.get("avg_hhi_benchmark", 0)), 1),
        "total_revenue_at_risk": round(float(opportunity_meta.get("total_revenue", 0)), 2),
        "avg_rs_gap": round(float(df["rs_gap"].mean()), 1),
        "avg_css_gap": round(float(df["css_gap"].mean()), 1),
        "avg_ps_gap": round(float(df["ps_gap"].mean()), 1),
        "pct_in_critical_tenure": round(
            float((df["phase"] == "Week 5-8").sum() / len(df)), 2
        ) if "phase" in df.columns else None
    }

    dimensions = ["gender", "age_bucket", "tenure_bucket", "start_season"]
    significant_effects = []

    for dim in dimensions:
        if dim not in df.columns:
            continue

        dim_df = df.dropna(subset=[dim])
        segment_counts = dim_df.groupby(dim)["customer_id"].nunique()
        valid_segments = segment_counts[segment_counts >= 5].index.tolist()

        if len(valid_segments) < 2:
            continue

        dim_df = dim_df[dim_df[dim].isin(valid_segments)]
        groups = [
            dim_df[dim_df[dim] == seg]["hhi_actual"].values
            for seg in valid_segments
        ]

        f_stat, p_value = stats.f_oneway(*groups)

        if p_value >= 0.05:
            continue

        segments = []
        for seg in valid_segments:
            seg_df = dim_df[dim_df[dim] == seg]
            segments.append({
                "value": seg,
                "member_count": int(seg_df["customer_id"].nunique()),
                "avg_hhi_actual": round(float(seg_df["hhi_actual"].mean()), 1),
                "avg_hhi_benchmark": round(float(seg_df["hhi_benchmark"].mean()), 1),
                "avg_hhi_gap": round(float(seg_df["hhi_gap"].mean()), 1),
                "avg_rs_gap": round(float(seg_df["rs_gap"].mean()), 1),
                "avg_css_gap": round(float(seg_df["css_gap"].mean()), 1),
                "avg_ps_gap": round(float(seg_df["ps_gap"].mean()), 1),
                "gap_vs_opportunity_avg": round(
                    float(seg_df["hhi_actual"].mean() - df["hhi_actual"].mean()), 1
                ),
                "pct_in_critical_tenure": round(
                    float((seg_df["phase"] == "Week 5-8").sum() / len(seg_df)), 2
                ) if "phase" in seg_df.columns else None
            })

        segments.sort(key=lambda x: x["avg_hhi_actual"])

        significant_effects.append({
            "effect_type": "main",
            "dimensions": [dim],
            "f_statistic": round(float(f_stat), 3),
            "p_value": round(float(p_value), 4),
            "segments": segments
        })

    for dim_a, dim_b in itertools.combinations(dimensions, 2):
        if dim_a not in df.columns or dim_b not in df.columns:
            continue

        dim_df = df.dropna(subset=[dim_a, dim_b]).copy()

        for dim in [dim_a, dim_b]:
            counts = dim_df.groupby(dim)["customer_id"].nunique()
            valid = counts[counts >= 5].index.tolist()
            dim_df = dim_df[dim_df[dim].isin(valid)]

        if len(dim_df) < 10:
            continue

        dim_a_safe = dim_a.replace("_", "")
        dim_b_safe = dim_b.replace("_", "")
        dim_df = dim_df.rename(columns={
            dim_a: dim_a_safe,
            dim_b: dim_b_safe,
            "hhi_actual": "hhiactual"
        })

        try:
            formula = f"hhiactual ~ {dim_a_safe}:{dim_b_safe}"
            model = smf.ols(formula=formula, data=dim_df).fit()

            interaction_rows = [
                k for k in model.pvalues.index
                if dim_a_safe in k and dim_b_safe in k
            ]

            if not interaction_rows:
                continue

            p_values = [model.pvalues[k] for k in interaction_rows]
            f_values = [abs(model.tvalues[k]) for k in interaction_rows]
            min_p = min(p_values)
            max_f = max(f_values)

            if min_p >= 0.05:
                continue

            dim_df_orig = df.dropna(subset=[dim_a, dim_b]).copy()
            for dim in [dim_a, dim_b]:
                counts = dim_df_orig.groupby(dim)["customer_id"].nunique()
                valid = counts[counts >= 5].index.tolist()
                dim_df_orig = dim_df_orig[dim_df_orig[dim].isin(valid)]

            segments = []
            for (val_a, val_b), group in dim_df_orig.groupby([dim_a, dim_b]):
                if len(group) < 2:
                    continue
                segments.append({
                    "value": f"{val_a} × {val_b}",
                    "member_count": int(group["customer_id"].nunique()),
                    "avg_hhi_actual": round(float(group["hhi_actual"].mean()), 1),
                    "avg_hhi_benchmark": round(float(group["hhi_benchmark"].mean()), 1),
                    "avg_hhi_gap": round(float(group["hhi_gap"].mean()), 1),
                    "avg_rs_gap": round(float(group["rs_gap"].mean()), 1),
                    "avg_css_gap": round(float(group["css_gap"].mean()), 1),
                    "avg_ps_gap": round(float(group["ps_gap"].mean()), 1),
                    "gap_vs_opportunity_avg": round(
                        float(group["hhi_actual"].mean() - df["hhi_actual"].mean()), 1
                    )
                })

            segments.sort(key=lambda x: x["avg_hhi_actual"])

            significant_effects.append({
                "effect_type": "interaction",
                "dimensions": [dim_a, dim_b],
                "f_statistic": round(float(max_f), 3),
                "p_value": round(float(min_p), 4),
                "segments": segments
            })

        except Exception:
            continue

    significant_effects.sort(key=lambda x: x["f_statistic"], reverse=True)

    return {
        "opportunity": opportunity,
        "significant_dimensions": significant_effects
    }


# ── ANALYSIS 2: VISIT PATTERN CLUSTERING ─────────────────────────────────────

def simplify_subscription(name: str) -> str:
    name = name.lower() if name else ""
    if "young" in name:
        return "Young"
    if "flex" in name:
        return "Flex"
    if "bedrijf" in name:
        return "Bedrijf"
    return "Regular"


def analyse_visit_patterns(visit_data: list, hhi_data: list) -> dict:
    """
    Cluster members based on 12-week visit behavior patterns.
    Includes inactive members (no visits) as a separate signal.
    No raw data is sent to Gemini — only aggregated cluster descriptions.
    """
    hhi_df = pd.DataFrame(hhi_data) if hhi_data else pd.DataFrame()
    all_member_ids = set(hhi_df["customer_id"].tolist()) if not hhi_df.empty else set()

    if not visit_data:
        return {
            "clusters": [],
            "total_members": len(all_member_ids),
            "inactive_members": len(all_member_ids),
            "note": "geen bezoekdata beschikbaar"
        }

    visits_df = pd.DataFrame(visit_data)
    visits_df["visit_date"] = pd.to_datetime(visits_df["visit_date"])
    visits_df["week_start_date"] = pd.to_datetime(visits_df["week_start_date"])

    ref_date = visits_df["week_start_date"].max()

    # Assign week number 1-12 (1 = oldest, 12 = most recent)
    visits_df["week_nr"] = visits_df["visit_date"].apply(
        lambda d: 12 - int((ref_date - pd.Timestamp(d)).days // 7)
    ).clip(1, 12)

    day_map = {
        "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
        "Friday": 4, "Saturday": 5, "Sunday": 6,
        "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3,
        "vrijdag": 4, "zaterdag": 5, "zondag": 6
    }

    visits_df["subscription_category"] = visits_df["subscription_name"].apply(simplify_subscription)

    members = []
    active_member_ids = set()

    for customer_id, group in visits_df.groupby("customer_id"):
        active_member_ids.add(customer_id)
        week_counts = group.groupby("week_nr").size()

        # Visits per week bucket (early = w1-4, mid = w5-8, recent = w9-12)
        visits_early = int(sum(week_counts.get(w, 0) for w in range(1, 5)))
        visits_mid = int(sum(week_counts.get(w, 0) for w in range(5, 9)))
        visits_recent = int(sum(week_counts.get(w, 0) for w in range(9, 13)))

        # Average visits per week per period
        avg_early = round(visits_early / 4, 1)
        avg_mid = round(visits_mid / 4, 1)
        avg_recent = round(visits_recent / 4, 1)

        anchor_day = group["day_of_week"].mode().iloc[0] if not group.empty else "unknown"
        anchor_hour = int(group["hour_of_day"].mode().iloc[0]) if not group.empty else 12
        anchor_subscription = group["subscription_category"].mode().iloc[0] if not group.empty else "Regular"

        day_numbers = group["day_of_week"].map(day_map).dropna()
        if len(day_numbers) > 1:
            consistency_score = round(max(0.0, min(1.0, 1 - (day_numbers.std() / 3.5))), 3)
        else:
            consistency_score = 1.0 if len(day_numbers) == 1 else 0.0

        members.append({
            "customer_id": customer_id,
            "avg_visits_early": avg_early,
            "avg_visits_mid": avg_mid,
            "avg_visits_recent": avg_recent,
            "anchor_day": str(anchor_day),
            "anchor_hour": int(anchor_hour),
            "anchor_subscription": str(anchor_subscription),
            "consistency_score": float(consistency_score)
        })

    inactive_member_ids = all_member_ids - active_member_ids
    n_inactive = len(inactive_member_ids)

    if len(members) < 3:
        return {
            "clusters": [],
            "total_members": len(all_member_ids),
            "inactive_members": n_inactive,
            "note": "te weinig actieve leden voor clustering"
        }

    feature_df = pd.DataFrame(members).set_index("customer_id")

    categorical_cols = ["anchor_day", "anchor_subscription"]
    cat_mask = [col in categorical_cols for col in feature_df.columns]

    distance_matrix = gower.gower_matrix(feature_df, cat_features=cat_mask)
    condensed = pdist(distance_matrix)
    Z = linkage(condensed, method="ward")

    merge_distances = Z[:, 2]
    gaps = np.diff(merge_distances)
    optimal_cut = int(len(gaps) - np.argmax(gaps[::-1]) - 1)
    n_clusters = int(max(2, min(6, len(members) - optimal_cut)))

    labels = fcluster(Z, n_clusters, criterion="maxclust")
    feature_df["cluster"] = labels

    hhi_lookup = pd.DataFrame()
    if not hhi_df.empty and "customer_id" in hhi_df.columns:
        hhi_lookup = hhi_df.set_index("customer_id")[
            ["gender", "age_bucket", "tenure_bucket", "hhi_actual", "hhi_gap"]
        ]

    clusters = []

    for cluster_id in sorted(feature_df["cluster"].unique()):
        cluster_members = feature_df[feature_df["cluster"] == cluster_id]
        member_ids = cluster_members.index.tolist()
        n = len(member_ids)

        avg_early = round(float(cluster_members["avg_visits_early"].mean()), 1)
        avg_mid = round(float(cluster_members["avg_visits_mid"].mean()), 1)
        avg_recent = round(float(cluster_members["avg_visits_recent"].mean()), 1)

        # Trend over 12 weeks
        if avg_recent < avg_early * 0.5:
            trend = "sterk dalend"
        elif avg_recent < avg_early * 0.8:
            trend = "licht dalend"
        elif avg_recent > avg_early * 1.2:
            trend = "stijgend"
        else:
            trend = "stabiel"

        # Dropout detection: active early/mid but not recent
        dropout_signal = avg_early > 0.5 and avg_recent < 0.3

        anchor_day = str(cluster_members["anchor_day"].mode().iloc[0])
        anchor_hour = int(cluster_members["anchor_hour"].median())
        anchor_subscription = str(cluster_members["anchor_subscription"].mode().iloc[0])
        avg_consistency = round(float(cluster_members["consistency_score"].mean()), 2)

        if avg_consistency >= 0.7:
            consistency_label = "hoge routine"
        elif avg_consistency >= 0.4:
            consistency_label = "wisselende routine"
        else:
            consistency_label = "geen vaste routine"

        hhi_context = {}
        if not hhi_lookup.empty:
            cluster_hhi = hhi_lookup[hhi_lookup.index.isin(member_ids)]
            if not cluster_hhi.empty:
                hhi_context = {
                    "avg_hhi_actual": round(float(cluster_hhi["hhi_actual"].mean()), 1),
                    "avg_hhi_gap": round(float(cluster_hhi["hhi_gap"].mean()), 1),
                    "dominant_gender": str(cluster_hhi["gender"].mode().iloc[0]) if "gender" in cluster_hhi.columns else None,
                    "dominant_age_bucket": str(cluster_hhi["age_bucket"].mode().iloc[0]) if "age_bucket" in cluster_hhi.columns else None,
                    "dominant_tenure_bucket": str(cluster_hhi["tenure_bucket"].mode().iloc[0]) if "tenure_bucket" in cluster_hhi.columns else None
                }

        clusters.append({
            "cluster_id": int(cluster_id),
            "member_count": int(n),
            "visit_pattern": {
                "avg_visits_per_week_early": avg_early,
                "avg_visits_per_week_mid": avg_mid,
                "avg_visits_per_week_recent": avg_recent,
                "trend": trend,
                "dropout_signal": dropout_signal
            },
            "anchor": {
                "most_common_day": anchor_day,
                "most_common_hour": anchor_hour,
                "most_common_subscription": anchor_subscription,
                "consistency_score": avg_consistency,
                "consistency_label": consistency_label
            },
            "hhi_context": hhi_context
        })

    if clusters and any(c.get("hhi_context") for c in clusters):
        clusters.sort(key=lambda x: x.get("hhi_context", {}).get("avg_hhi_gap", 0))

    # Add inactive members summary
    inactive_hhi_context = {}
    if not hhi_lookup.empty and inactive_member_ids:
        inactive_hhi = hhi_lookup[hhi_lookup.index.isin(inactive_member_ids)]
        if not inactive_hhi.empty:
            inactive_hhi_context = {
                "avg_hhi_actual": round(float(inactive_hhi["hhi_actual"].mean()), 1),
                "avg_hhi_gap": round(float(inactive_hhi["hhi_gap"].mean()), 1),
                "dominant_gender": str(inactive_hhi["gender"].mode().iloc[0]) if "gender" in inactive_hhi.columns else None,
                "dominant_age_bucket": str(inactive_hhi["age_bucket"].mode().iloc[0]) if "age_bucket" in inactive_hhi.columns else None,
                "dominant_tenure_bucket": str(inactive_hhi["tenure_bucket"].mode().iloc[0]) if "tenure_bucket" in inactive_hhi.columns else None
            }

    return {
        "total_members": int(len(all_member_ids)),
        "active_members": int(len(active_member_ids)),
        "inactive_members": int(n_inactive),
        "inactive_summary": inactive_hhi_context,
        "n_clusters": n_clusters,
        "clusters": clusters
    }


# ── PHASE LOGIC ───────────────────────────────────────────────────────────────

def validate_phase_transition(current_phase: str, new_phase: str) -> tuple:
    if new_phase not in PHASE_ORDER:
        return False, False
    if current_phase not in PHASE_ORDER:
        return new_phase == "discover", False

    current_idx = PHASE_ORDER.index(current_phase)
    new_idx = PHASE_ORDER.index(new_phase)

    if new_idx == current_idx:
        return True, False
    if new_idx == current_idx + 1:
        return True, False
    if new_idx > current_idx + 1:
        return False, False
    if new_idx < current_idx:
        return True, True

    return False, False


def fetch_data_for_phase(
    phase: str,
    tenant_id: str,
    week_start_date: str,
    scope: dict,
    opportunity_meta: dict = None
) -> dict:
    """
    Fetch and analyse data for the current phase.
    Returns only aggregated analysis results — no raw data for Gemini.
    """
    data = {}
    if phase == "define":
        hhi = get_hhi_dataset(tenant_id, week_start_date, scope)
        visits = get_visits_dataset(tenant_id, week_start_date, scope)

        # Analyse HHI segments — use opportunity_meta if available
        meta = opportunity_meta or {
            "journey": scope.get("journey"),
            "product": scope.get("product"),
            "phase": scope.get("phase"),
            "member_count": len(hhi),
            "avg_hhi_gap": round(float(pd.DataFrame(hhi)["hhi_gap"].mean()), 1) if hhi else 0,
            "avg_hhi_actual": round(float(pd.DataFrame(hhi)["hhi_actual"].mean()), 1) if hhi else 0,
            "avg_hhi_benchmark": round(float(pd.DataFrame(hhi)["hhi_benchmark"].mean()), 1) if hhi else 0,
            "total_revenue": round(float(pd.DataFrame(hhi)["subscription_price_month_ex_vat"].sum()), 2) if hhi else 0
        }

        hhi_analysis = analyse_hhi_segments(hhi, meta)
        visit_analysis = analyse_visit_patterns(visits, hhi)

        # Only send aggregated analyses to Gemini — no raw data
        data["hhi_analysis"] = hhi_analysis
        data["visit_analysis"] = visit_analysis

        log({"QUERY_BIGQUERY_HHI_WEEK": {"phase": phase, "row_count": len(hhi)}})
        log({"QUERY_BIGQUERY_VISITS_WEEK": {"phase": phase, "row_count": len(visits)}})
        log({"HHI_ANALYSIS": {
            "member_count": hhi_analysis["opportunity"]["member_count"],
            "significant_dimensions": len(hhi_analysis["significant_dimensions"])
        }})
        log({"VISIT_ANALYSIS": {
            "total_members": visit_analysis.get("total_members"),
            "active_members": visit_analysis.get("active_members"),
            "inactive_members": visit_analysis.get("inactive_members"),
            "n_clusters": visit_analysis.get("n_clusters"),
            "clusters": visit_analysis.get("clusters")
        }})
    return data


def build_context_message(phase: str, scope: dict, data: dict) -> str:
    scope_label = json.dumps(scope, default=json_serializable) if scope else "volledig"
    lines = [f"[CONTEXT UPDATE] Fase: {phase} | Scope: {scope_label}"]

    if "hhi_analysis" in data:
        lines.append(
            f"\nHHI segmentanalyse:\n"
            f"{json.dumps(data['hhi_analysis'], indent=2, default=json_serializable)}"
        )

    if "visit_analysis" in data:
        lines.append(
            f"\nBezoekpatroon clusters (12 weken):\n"
            f"{json.dumps(data['visit_analysis'], indent=2, default=json_serializable)}"
        )

    if not data:
        lines.append("\nGeen aanvullende data voor deze fase.")

    return "\n".join(lines)


# ── GEMINI ────────────────────────────────────────────────────────────────────

def call_gemini(system_prompt: str, messages: list) -> dict:
    client = GenAIClient(api_key=os.environ["GEMINI_API_KEY"])

    gemini_messages = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        gemini_messages.append({"role": role, "parts": [{"text": msg["content"]}]})

    response = client.models.generate_content(
        model=get_model("habits_coach_model", _DEFAULT_GEMINI_MODEL),
        contents=gemini_messages,
        config={"system_instruction": system_prompt}
    )

    raw = response.text.strip() if response.text else ""
    log({"GEMINI_RAW_RESPONSE": {
        "has_text": response.text is not None,
        "length": len(raw),
        "preview": raw[:500],
        "finish_reason": str(response.candidates[0].finish_reason) if response.candidates else "NO_CANDIDATES"
    }})

    if not raw:
        raise ValueError(
            f"Gemini returned empty response. "
            f"Finish reason: {response.candidates[0].finish_reason if response.candidates else 'unknown'}"
        )

    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log({"GEMINI_PLAIN_TEXT_FALLBACK": {"length": len(raw)}})
        return {
            "message": raw,
            "phase": None,
            "phase_pending_confirmation": False,
            "scope": None,
            "member_ids": []
        }


# ── SLACK ─────────────────────────────────────────────────────────────────────

def _slack_post(token: str, channel: str, text: str, thread_ts: str = None) -> dict:
    payload = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
    response = session.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=10
    )
    return response.json()


def post_slack_message(token: str, channel: str, text: str, thread_ts: str = None) -> dict:
    return _slack_post(token=token, channel=channel, text=text, thread_ts=thread_ts)


def format_contacts_message(contacts: list) -> str:
    if not contacts:
        return "⚠️ Geen contactgegevens gevonden voor de opgegeven member IDs."
    lines = ["📋 *Contactgegevens voor validatie:*\n"]
    for c in contacts:
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        lines.append(f"• *{name}* | {c.get('email', '-')} | {c.get('phone', '-')}")
    return "\n".join(lines)


# ── SESSION HANDLERS ──────────────────────────────────────────────────────────

def handle_new_session(
    tenant_id: str,
    slack_token: str,
    slack_channel: str,
    opportunity_nr: int,
    system_prompt: str
):
    # Lock check FIRST — before posting anything to Slack
    lock_ref = fs_client.collection("session_locks").document(
        f"{tenant_id}_opp{opportunity_nr}"
    )
    lock = lock_ref.get()
    if lock.exists:
        log({"SKIPPED": {"reason": "session lock exists", "opportunity_nr": opportunity_nr}})
        return
    lock_ref.set({"created_at": datetime.now(timezone.utc).isoformat()})

    progress = ProgressMessage(token=slack_token, channel=slack_channel)
    progress.start("Opportunity ophalen")

    log({"STEP": "NEW_SESSION_FETCH_OPPORTUNITY", "opportunity_nr": opportunity_nr})
    opportunity = get_opportunity(tenant_id, opportunity_nr)
    if not opportunity:
        log({"NO_OPPORTUNITY": {"tenant_id": tenant_id, "opportunity_nr": opportunity_nr}})
        publish_error_event(
            "habits-coach-reply",
            f"No opportunity found for tenant {tenant_id} nr {opportunity_nr}"
        )
        lock_ref.delete()
        return

    log({"QUERY_BIGQUERY_OPPORTUNITIES": {
        "tenant_id": tenant_id,
        "opportunity_nr": opportunity_nr,
        "journey": opportunity["journey"],
        "product": opportunity["product"],
        "phase": opportunity["phase"]
    }})

    week_start_date = opportunity["week_start_date"]
    scope = {
        "journey": opportunity["journey"],
        "product": opportunity["product"],
        "phase": opportunity["phase"]
    }

    session_id = f"{tenant_id}_{week_start_date}_opp{opportunity_nr}"
    existing = fs_client.collection("coaching_sessions").document(session_id).get()
    if existing.exists:
        log({"SESSION_EXISTS": {"session_id": session_id, "skipping": True}})
        lock_ref.delete()
        return

    progress.update("Segmenten analyseren")

    log({"STEP": "NEW_SESSION_FETCH_HHI"})
    hhi_data = get_hhi_dataset(tenant_id, week_start_date, scope)
    log({"QUERY_BIGQUERY_HHI_WEEK": {"row_count": len(hhi_data)}})

    if not hhi_data:
        log({"NO_HHI_DATA": {"tenant_id": tenant_id, "scope": scope}})
        lock_ref.delete()
        return

    log({"STEP": "NEW_SESSION_ANALYSE_SEGMENTS"})
    analysis = analyse_hhi_segments(hhi_data, opportunity)
    log({"SEGMENT_ANALYSIS": {
        "member_count": analysis["opportunity"]["member_count"],
        "significant_dimensions": len(analysis["significant_dimensions"])
    }})

    progress.update("Coaching bericht genereren")

    initial_message = {
        "role": "user",
        "content": (
            f"Dit is de coaching sessie voor week {week_start_date}.\n\n"
            f"De geselecteerde opportunity:\n"
            f"{json.dumps(analysis['opportunity'], indent=2, default=json_serializable)}\n\n"
            f"Significante segmentverschillen:\n"
            f"{json.dumps(analysis['significant_dimensions'], indent=2, default=json_serializable)}\n\n"
            f"Start de discover fase en presenteer deze opportunity aan de manager."
        )
    }

    log({"STEP": "NEW_SESSION_CALL_GEMINI"})
    gemini_response = call_gemini(system_prompt, [initial_message])
    log({"GEMINI_RESPONSE": {"preview": gemini_response.get("message", "")[:200]}})

    progress.complete(gemini_response["message"])

    if not progress.ts:
        log({"SLACK_ERROR": "progress message ts missing"})
        publish_error_event("habits-coach-reply", f"Slack progress message failed for tenant {tenant_id}")
        lock_ref.delete()
        return

    thread_ts = progress.ts
    log({"TO_SLACK": {"tenant_id": tenant_id, "channel": slack_channel, "thread_ts": thread_ts}})

    member_ids = gemini_response.get("member_ids", [])
    if member_ids:
        contacts = get_member_contacts(tenant_id, member_ids)
        post_slack_message(
            token=slack_token,
            channel=slack_channel,
            text=format_contacts_message(contacts),
            thread_ts=thread_ts
        )

    now = datetime.now(timezone.utc).isoformat()
    session_doc = {
        "tenant_id": tenant_id,
        "week_start_date": week_start_date,
        "slack_channel": slack_channel,
        "slack_thread_ts": thread_ts,
        "created_at": now,
        "updated_at": now,
        "current_phase": "discover",
        "pending_phase": None,
        "opportunity_nr": opportunity_nr,
        "scope": scope,
        "opportunity_meta": dict(opportunity),
        "segment_analysis": analysis["significant_dimensions"],
        "conversation": [
            {
                "role": "user",
                "content": initial_message["content"],
                "timestamp": now
            },
            {
                "role": "model",
                "content": gemini_response["message"],
                "slack_ts": thread_ts,
                "timestamp": now
            }
        ]
    }

    fs_client.collection("coaching_sessions").document(session_id).set(session_doc)
    log({"TO_FIRESTORE": {
        "session_id": session_id,
        "opportunity_nr": opportunity_nr,
        "scope": scope,
        "significant_dimensions": len(analysis["significant_dimensions"])
    }})

    lock_ref.delete()


def handle_expired_session(
    session: dict,
    session_id: str,
    slack_token: str,
    slack_channel: str
):
    scope = session.get("scope", {})
    journey = scope.get("journey", "onbekend")
    product = scope.get("product", "onbekend")

    text = (
        f"⏱️ De vorige coachingsessie over *{journey} - {product}* is niet afgerond.\n\n"
        f"Wil je daar verder mee gaan, of starten we met een nieuwe opportunity?\n\n"
        f"• Typ *verder* om de sessie te hervatten\n"
        f"• Typ *nieuw* om een nieuwe opportunity te starten"
    )

    slack_response = post_slack_message(token=slack_token, channel=slack_channel, text=text)
    log({"TO_SLACK": {"expired_session_prompt": True, "ok": slack_response.get("ok")}})

    fs_client.collection("coaching_sessions").document(session_id).update({
        "_awaiting_expired_response": True
    })


def handle_manager_reply(
    tenant_id: str,
    slack_token: str,
    slack_channel: str,
    session_id: str,
    session: dict,
    user_message: str,
    event_ts: str,
    system_prompt: str
):
    current_phase = session.get("current_phase", "discover")
    pending_phase = session.get("pending_phase")
    current_scope = session.get("scope") or {}
    opportunity_meta = session.get("opportunity_meta")

    log({"SESSION_STATE": {
        "session_id": session_id,
        "current_phase": current_phase,
        "pending_phase": pending_phase,
        "scope": current_scope
    }})

    if session.get("_awaiting_expired_response"):
        lower = user_message.lower().strip()
        if "nieuw" in lower:
            current_opportunity_nr = session.get("opportunity_nr", 0)
            fs_client.collection("coaching_sessions").document(session_id).update({
                "_awaiting_expired_response": firestore.DELETE_FIELD
            })
            handle_new_session(
                tenant_id, slack_token, slack_channel,
                current_opportunity_nr + 1, system_prompt
            )
            return
        elif "verder" in lower:
            fs_client.collection("coaching_sessions").document(session_id).update({
                "_awaiting_expired_response": firestore.DELETE_FIELD,
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
        else:
            post_slack_message(
                token=slack_token,
                channel=slack_channel,
                text="Typ *verder* om de sessie te hervatten of *nieuw* om een nieuwe opportunity te starten."
            )
            return

    progress = ProgressMessage(token=slack_token, channel=slack_channel)

    log({"STEP": f"FETCH_DATA;phase={current_phase}"})
    phase_data = fetch_data_for_phase(
        current_phase,
        tenant_id,
        session["week_start_date"],
        current_scope,
        opportunity_meta
    )

    messages = []
    for msg in session.get("conversation", []):
        messages.append({"role": msg["role"], "content": msg["content"]})

    if phase_data:
        context_message = build_context_message(current_phase, current_scope, phase_data)
        messages.append({"role": "user", "content": context_message})
        messages.append({"role": "model", "content": "Bedankt, ik heb de actuele data ontvangen."})

    messages.append({"role": "user", "content": user_message})

    log({"GEMINI_MESSAGES": {
        "total": len(messages),
        "data_injected": bool(phase_data),
        "preview": user_message[:100]
    }})

    progress.start("Coaching bericht genereren")

    log({"STEP": "CALL_GEMINI"})
    gemini_response = call_gemini(system_prompt, messages)
    log({"GEMINI_RESPONSE": {
        "preview": gemini_response.get("message", "")[:200],
        "phase": gemini_response.get("phase"),
        "scope": gemini_response.get("scope")
    }})

    proposed_phase = gemini_response.get("phase") or current_phase
    gemini_wants_confirmation = gemini_response.get("phase_pending_confirmation", False)
    is_valid, requires_confirmation = validate_phase_transition(current_phase, proposed_phase)

    if not is_valid:
        proposed_phase = current_phase
        requires_confirmation = False

    if requires_confirmation or gemini_wants_confirmation:
        new_current_phase = current_phase
        new_pending_phase = proposed_phase
    else:
        new_current_phase = proposed_phase
        new_pending_phase = None

    new_scope = gemini_response.get("scope") or current_scope
    if new_current_phase == "discover" and not new_pending_phase:
        new_scope = {}

    if new_current_phase == "define" and current_phase == "discover":
        progress.update("Bezoekdata ophalen")

        log({"STEP": f"DEFINE_TRANSITION_FETCH_DATA;scope={new_scope}"})
        define_data = fetch_data_for_phase(
            "define",
            tenant_id,
            session["week_start_date"],
            new_scope,
            opportunity_meta
        )

        progress.update("Bezoekpatronen clusteren")

        context_message = build_context_message("define", new_scope, define_data)
        messages.append({"role": "model", "content": gemini_response["message"]})
        messages.append({"role": "user", "content": context_message})
        messages.append({"role": "model", "content": "Bedankt, ik heb de actuele data ontvangen."})
        messages.append({
            "role": "user",
            "content": "[SYSTEEM] Analyseer de HHI segmentanalyse en bezoekpatroon clusters en stel hypotheses op."
        })

        progress.update("Hypotheses genereren")

        log({"STEP": "DEFINE_TRANSITION_CALL_GEMINI"})
        gemini_response = call_gemini(system_prompt, messages)
        log({"GEMINI_RESPONSE_DEFINE": {"preview": gemini_response.get("message", "")[:200]}})

        new_current_phase = "define"
        new_scope = gemini_response.get("scope") or new_scope

    progress.complete(gemini_response["message"])

    if not progress.ts:
        log({"SLACK_ERROR": "progress message ts missing"})
        publish_error_event("habits-coach-reply", "Slack progress message failed")
        return

    reply_ts = progress.ts
    log({"TO_SLACK": {"channel": slack_channel, "reply_ts": reply_ts}})

    member_ids = gemini_response.get("member_ids", [])
    if member_ids:
        contacts = get_member_contacts(tenant_id, member_ids)
        post_slack_message(
            token=slack_token,
            channel=slack_channel,
            text=format_contacts_message(contacts)
        )

    now = datetime.now(timezone.utc).isoformat()
    conversation = session.get("conversation", [])
    conversation.append({
        "role": "user",
        "content": user_message,
        "slack_ts": event_ts,
        "timestamp": now
    })
    conversation.append({
        "role": "model",
        "content": gemini_response["message"],
        "slack_ts": reply_ts,
        "timestamp": now
    })

    update = {
        "conversation": conversation,
        "current_phase": new_current_phase,
        "scope": new_scope,
        "updated_at": now
    }
    if new_pending_phase:
        update["pending_phase"] = new_pending_phase
    else:
        update["pending_phase"] = firestore.DELETE_FIELD

    fs_client.collection("coaching_sessions").document(session_id).update(update)

    log({"TO_FIRESTORE": {
        "session_id": session_id,
        "current_phase": new_current_phase,
        "pending_phase": new_pending_phase,
        "scope": new_scope,
        "conversation_length": len(conversation)
    }})


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

@functions_framework.http
def habits_coach_reply(request):
    body = request.get_json(silent=True) or {}

    log({"INPUT": {
        "type": body.get("type"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }})

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}, 200

    event = body.get("event", {})
    event_type = event.get("type")
    subtype = event.get("subtype")

    is_bot_trigger = (
        subtype == "bot_message" and
        event.get("text", "").startswith("start:")
    )

    if event_type != "message":
        log({"SKIPPED": {"reason": "not a message event"}})
        return {"status": "ignored"}, 200

    if subtype in ("message_deleted", "message_changed"):
        log({"SKIPPED": {"reason": "message_changed or deleted"}})
        return {"status": "ignored"}, 200

    if event.get("bot_id") and not is_bot_trigger:
        log({"SKIPPED": {"reason": "bot message, not a start trigger"}})
        return {"status": "ignored"}, 200

    if subtype == "bot_message" and not is_bot_trigger:
        log({"SKIPPED": {"reason": "bot_message subtype, not a start trigger"}})
        return {"status": "ignored"}, 200

    user_message = event.get("text", "").strip()
    slack_team_id = body.get("team_id")
    message_channel = event.get("channel")

    log({"EVENT": {
        "team_id": slack_team_id,
        "channel": message_channel,
        "is_bot_trigger": is_bot_trigger,
        "message_preview": user_message[:100]
    }})

    try:
        tenant_id, tenant = get_tenant_by_team_id(slack_team_id)
        if not tenant_id:
            log({"TENANT_NOT_FOUND": {"slack_team_id": slack_team_id}})
            return {"status": "tenant not found"}, 200

        slack_token = tenant.get("slack_bot_token")
        slack_channel = tenant.get("slack_coach_channel")

        if message_channel != slack_channel:
            log({"SKIPPED": {"reason": "not coaching channel"}})
            return {"status": "ignored"}, 200

        system_prompt = get_system_prompt()
        session_id, session = get_latest_session(tenant_id, slack_channel)
        action = determine_session_action(session, is_bot_trigger)

        log({"SESSION_ACTION": {
            "action": action,
            "is_bot_trigger": is_bot_trigger,
            "has_session": bool(session)
        }})

        if action == "new":
            if is_bot_trigger:
                try:
                    opportunity_nr = int(user_message.split(":")[1])
                except (IndexError, ValueError):
                    opportunity_nr = (session.get("opportunity_nr", 0) if session else 0) + 1
            else:
                opportunity_nr = (session.get("opportunity_nr", 0) if session else 0) + 1

            handle_new_session(
                tenant_id, slack_token, slack_channel,
                opportunity_nr, system_prompt
            )

        elif action == "expired":
            handle_expired_session(session, session_id, slack_token, slack_channel)

        elif action == "continue":
            handle_manager_reply(
                tenant_id, slack_token, slack_channel,
                session_id, session, user_message,
                event.get("ts"), system_prompt
            )

        return {"status": "ok"}, 200

    except Exception as e:
        error_msg = f"Error in habits-coach-reply: {str(e)}"
        log({"ERROR": str(e)})
        publish_error_event("habits-coach-reply", error_msg)
        return {"status": "error"}, 200
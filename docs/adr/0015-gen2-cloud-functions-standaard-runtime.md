# 0015 — Gen2 Cloud Functions als standaard runtime

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `runtime`, `gcp`

## Context

GCP biedt meerdere runtime-opties (Cloud Functions Gen1, Gen2, Cloud Run). Inconsistentie in runtime verhoogt de operationele complexiteit en maakt deployment-patronen onvoorspelbaar.

## Beslissing

Alle services draaien als Cloud Functions Gen2 met Python 3.12 in `europe-west1`. Geen Gen1, geen Cloud Run tenzij een specifieke beperking van Functions dat vereist.

## Gevolgen

- Gen2 Pub/Sub-functies gebruiken `@functions_framework.cloud_event` als entry point; HTTP-functies gebruiken `@functions_framework.http`.
- Deployment: `gcloud functions deploy {naam} --gen2 --runtime python312 --region europe-west1 --trigger-topic {topic}` (voor Pub/Sub) of `--trigger-http` (voor HTTP).
- Gen2 draait op Cloud Run onderliggend — service-namen in Cloud Logging zijn de function-namen, resource type is `cloud_run_revision`. Dit is relevant voor Stage C log-walking in `slack-agent`.
- Vertex AI Publisher models zijn niet beschikbaar in `europe-west1` voor Gemini — vandaar de keuze voor `google-genai` SDK (zie ADR-0017). *[afgeleid]*

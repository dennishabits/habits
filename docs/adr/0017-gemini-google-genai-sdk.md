# 0017 — Gemini via google-genai SDK, niet Vertex AI

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `ai`, `gcp`, `runtime`

## Context

GCP biedt twee manieren om Gemini te gebruiken: via Vertex AI (gcloud-authenticatie, regionale endpoints) en via de `google-genai` SDK (API key). Vertex AI Publisher models zijn niet beschikbaar in `europe-west1` voor Gemini-modellen. `habits-coach-reply` opereerde al via `google-genai`.

## Beslissing

Alle Gemini-aanroepen gebruiken de `google-genai` SDK met een `GEMINI_API_KEY`. Vertex AI wordt niet gebruikt voor Gemini.

## Gevolgen

- `GEMINI_API_KEY` is vereist als omgevingsvariabele in alle services die Gemini aanroepen.
- Aanroepen zijn niet regio-gebonden — dit is consistent met de `europe-west1` deploymentstandaard (zie ADR-0015) maar Gemini-verwerking vindt niet noodzakelijk in europa plaats. *[afgeleid]*
- Het gebruikte model is `gemini-2.5-flash`; model-upgrades vereisen aanpassing van de model-constante in de betreffende service.
- `google-genai` wordt toegevoegd aan `requirements.txt` van elke service die Gemini aanroept: `google-genai==1.*`.

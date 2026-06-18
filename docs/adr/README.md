# ADR Index — Habits

Architecture Decision Records (ADRs) documenteren beslissingen die significante invloed hebben op de architectuur of productrichting van Habits. Ze zijn onveranderlijk: een beslissing wordt niet bewerkt maar vervangen door een nieuw record met status `Superseded door ADR-NNNN`.

Agents die de rationale achter een beslissing nodig hebben, raadplegen dit index en laden het relevante ADR-bestand.

## Index

| # | Titel | Status | Tags |
|---|-------|--------|------|
| [ADR-0001](0001-continuous-improvement-geen-early-warning-tool.md) | Continuous improvement, geen early-warning tool | Accepted | strategie, product |
| [ADR-0002](0002-koper-is-eigenaar-of-franchisenemer.md) | De koper is de eigenaar of franchisenemer | Accepted | strategie, product |
| [ADR-0003](0003-ledenretentie-is-primaire-waardedrijver.md) | Ledenretentie is de primaire waardedrijver | Accepted | strategie, product |
| [ADR-0004](0004-hhi-als-centraal-meetsysteem.md) | HHI als centraal meetsysteem | Accepted | strategie, product, hhi |
| [ADR-0005](0005-rapportagelaag-eigenaar-open-ontwerpkeuze.md) | Rapportagelaag voor de eigenaar is een open ontwerpkeuze | Accepted | strategie, open |
| [ADR-0006](0006-conversational-ux-als-richting.md) | Conversational UX als richting | Accepted | strategie, ux |
| [ADR-0007](0007-multi-tenancy-als-first-class-concern.md) | Multi-tenancy als first-class concern | Accepted | multi-tenancy, architectuur |
| [ADR-0008](0008-all-in-op-ai.md) | All-in op AI | Accepted | strategie, ai |
| [ADR-0009](0009-kennis-en-uitvoering-gescheiden.md) | Kennis en uitvoering zijn gescheiden | Accepted | strategie, ai, architectuur |
| [ADR-0010](0010-event-driven-boven-request-driven.md) | Event-driven boven request-driven | Accepted | pipeline, architectuur |
| [ADR-0011](0011-scheiding-enrichment-translation-listening.md) | Scheiding van enrichment, translation en listening | Accepted | pipeline, architectuur |
| [ADR-0012](0012-llm-krijgt-nooit-ruwe-ledendata.md) | LLM krijgt nooit ruwe ledendata | Accepted | ai, privacy, bigquery |
| [ADR-0013](0013-pii-blijft-in-slack.md) | PII blijft in Slack | Accepted | privacy, security |
| [ADR-0014](0014-operationele-configuratie-in-firestore.md) | Operationele configuratie leeft in Firestore, niet in code | Accepted | architectuur, firestore |
| [ADR-0015](0015-gen2-cloud-functions-standaard-runtime.md) | Gen2 Cloud Functions als standaard runtime | Accepted | runtime, gcp |
| [ADR-0016](0016-action-type-op-root-niveau.md) | action_type op root-niveau van CRM task payload | Accepted | pipeline, schema |
| [ADR-0017](0017-gemini-google-genai-sdk.md) | Gemini via google-genai SDK, niet Vertex AI | Accepted | ai, gcp, runtime |

## Formaat

Elk ADR heeft drie vaste secties: **Context** (de situatie die de beslissing forceerde), **Beslissing** (de keuze zelf, zo letterlijk mogelijk uit de originele documentatie), en **Gevolgen** (wat dit mogelijk maakt en wat het kost of beperkt). Items gemarkeerd met *[afgeleid]* zijn niet expliciet in de originele documentatie vermeld maar logisch afgeleid uit de context.

## Regel: supersession, geen editie

Als een beslissing wordt herzien, wordt het oorspronkelijke ADR **niet bewerkt**. In plaats daarvan:

1. Schrijf een nieuw ADR (volgend nummer) met de nieuwe beslissing
2. Zet de status van het oude ADR op `Superseded door ADR-NNNN`
3. Verwijs vanuit het nieuwe ADR terug naar het vervangen record

Dit waarborgt dat de redenering achter verouderde beslissingen bewaard blijft en traceerbaar is.

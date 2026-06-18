# 0016 — action_type op root-niveau van CRM task payload

- **Status**: Accepted
- **Datum**: onbekend
- **Tags**: `pipeline`, `schema`

## Context

Completion-logica (welk event-type sluit een CRM-taak af?) was historisch impliciet gekoppeld aan `task_type`-namen. Dit maakte de BigQuery-views fragiel: nieuwe taaktypen vereisten view-aanpassingen. Bovendien was `action_type` in vroege versies genest onder `payload.payload`, waardoor het moeilijker te extraheren was.

## Beslissing

`action_type` bepaalt welk completion-event een taak afsluit en staat altijd op root-niveau van de payload, niet genest. Backward compatibility wordt geborgd via de `effective_action_type` fallback in de `task_performance` BigQuery view.

## Gevolgen

- Nieuwe taaktypen vereisen geen view-aanpassing zolang ze een correct `action_type` meesturen.
- `effective_action_type` in `task_performance` leest `action_type` op root-niveau; als dat ontbreekt (oudere events), valt het terug op een mapping van `task_type` naar action_type.
- Pipeline-services die op completion matchen, gebruiken `action_type` als primair criterium. *[afgeleid]*
- Geldige waarden: `contact`, `appointment`, `subscription`, `review`.

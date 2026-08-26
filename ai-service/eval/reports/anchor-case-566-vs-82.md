# Caso ancla — la misma transcripción, 566 jornadas y 82

Las dos observaciones que sostienen la Sesión 16. Se guardan juntas porque por
separado no significan nada: una cifra alta suelta se lee como "el sistema estima
alto", y solo el **par** revela lo que de verdad pasa.

- **Entorno:** instancia desplegada (S15), `POST /v1/estimate/from-transcript`
- **Modelo:** `GENERATION_MODEL=gpt-5`, `reasoning_effort=high`
- **Caso:** `case-001-ecommerce-checkout` del golden set
- **Fecha:** 2026-08-26
- **Rango aceptable del golden set:** `[45, 120]` jornadas, derivado del corpus
  (8 proyectos de *ecommerce*, media 75, rango 9–100)

## Observación 1 — 566 jornadas

De la tanda completa, guardada en `report-20260826T100555.json`:

```
[1/6] case-001-ecommerce-checkout…
  FAIL  estimation  566 outside [45, 120]        135.6s
```

Los cinco casos de estimación de esa tanda quedaron 3-5× por encima de lo
esperado, **todos con `confidence: "low"`**, y `within_range_rate` 0%.

## Observación 2 — 82 jornadas

Llamada posterior al **mismo** caso, con el mismo sistema y sin ningún cambio.
Volcado línea a línea de las tareas junto a la evidencia que citan:

```
total_engineer_days: 82 | confidence: low

## Checkout & Cart
   5 d  Checkout flow orchestration        <- Estimated hours: 150
   4 d  Persistent saved carts             <- Estimated hours: 150
   4 d  Promotion codes engine             <- Estimated hours: 150

## Payments Integration (Cards + PayPal)
   6 d  Card payments integration          <- Estimated hours: 110
   4 d  PayPal integration                 <- Estimated hours: 110
   2 d  Payment webhooks & reconciliation   <- Estimated hours: 110
   2 d  Refunds/voids flows                <- Estimated hours: 110

## Product Search & Facets
   5 d  Catalog indexing pipeline          <- Estimated hours: 140
   6 d  Faceted search API                 <- Estimated hours: 140
   4 d  Search UI integration              <- Estimated hours: 140
   3 d  Caching & performance              <- Estimated hours: 140

## Transactional Email
   2 d  Email provider integration         <- Estimated hours: 40
   2 d  Order confirmation email           <- Estimated hours: 40
   1 d  Shipping update email              <- Estimated hours: 40

## Split Shipping & Fulfillment
   3 d  Warehouse routing logic            <- Estimated hours: 36
   2 d  Multiple shipments & tracking      <- Estimated hours: 36
   2 d  Cart/checkout split-shipping calc  <- Estimated hours: 26
   1 d  Order views for split shipping     <- Estimated hours: 26

## Performance & Scalability · Frontend/UX · QA & Testing · Project Management
   … 14 jornadas más, todas con su cita
```

Aquí la conversión **está hecha**: 150 h de una componente histórica se reparten
en 5 + 4 + 4 = 13 jornadas. Y el propio `reasoning` del modelo lo dice:

> *"…converting their hours to engineer-days and decomposing into concrete tasks…
> totals sum to 82 engineer-days."*

## Diagnóstico

La causa está en `app/generation/rag/prompt_builder.py::build_system_prompt`: **el
prompt nunca dice cuántas horas tiene una jornada**, mientras las fuentes hablan en
`estimated_hours`, el campo del esquema se llama `engineer_days`, y la regla 2 pide
copiar *"the component name and its estimated hours"* como evidencia. La conversión
queda implícita, así que a veces el modelo divide entre 8 y a veces no.

Una distribución bimodal como ésa es **indistinguible de "el sistema estima alto"**
si solo se mira una ejecución. Ése es exactamente el valor de un golden set frente
a una llamada suelta.

## Por qué no está arreglado

Es material didáctico de la Sesión 16 en dos sitios:

1. **El guardrail de límites** (`foundation/guardrails/estimate_bounds.py`) se
   calibra contra estas dos cifras: evidencia ≈ 77 jornadas (613 h de chunks
   distintos ÷ 8), 82 → 1,1× (pasa) y 566 → 7,4× (marcado). El límite por defecto
   es 3×.
2. **El A/B**: arreglar el prompt es una línea, y es justo el tipo de cambio que
   pide una comparación contra el golden set. El número que decide es
   `within_range_rate`.

## Cómo reproducirlo

```bash
ssh ubuntu@$EC2_HOST "cd /opt/estimator && docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml exec -T ai-service \
  python /app/eval/run_eval.py --base-url http://localhost:8000 \
  --case case-001-ecommerce-checkout --out /tmp/reports"
```

Ejecútalo dos o tres veces. La dispersión es el resultado.

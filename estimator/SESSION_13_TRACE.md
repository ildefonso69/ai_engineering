# Session 13 — LangGraph Estimation Pipeline: Execution Trace

This document captures one complete, real execution of the Session 13 LangGraph
re-expression of the transcript-to-estimate pipeline
(`app/generation/agentic/graph.py`, orchestrated by
`EstimationService.estimate_from_transcript_graph` in
`app/domain/estimation_service.py`), run against the real stack: live LLM
calls (`gpt-5-mini` for reformulation/classification, `gpt-5` with high
reasoning effort for generation), the project's own Postgres
(`estimator-postgres`, used both for the seeded historical-budget corpus and
for the LangGraph `AsyncPostgresSaver` checkpointer), and real vector
retrieval over the seeded corpus (29 documents / 282 chunks: 17
`historical_budget` + 12 `historical_task_breakdown`).

The run was made through the actual public HTTP endpoint — not a bypass
script — to be a faithful proof of what a real caller (George, or the live
session) would get:

```
POST /v1/estimate/from-transcript-graph
Header: X-API-Key: <ESTIMATE_API_KEY>
Body:   {"transcript": "<contents of tests/fixtures/sample_transcript_complex.txt>",
         "idempotency_key": "s13-delivery-trace"}
```

`idempotency_key` doubles as the LangGraph checkpointer's `thread_id`
(`AsyncPostgresSaver` over `estimator-postgres`, confirmed via
`SELECT thread_id FROM checkpoints WHERE thread_id = 's13-delivery-trace'`
returning rows) — this is the Level 3 requirement "pass a `thread_id` per run
using the estimation's own identifier" in effect.

HTTP response: **`200 OK`**.

## Graph shape run

```
START -> extract_requirements -> classify_components -> search_budgets
      -> generate_estimate -> validate_and_consolidate
      --[validation_passed]-----> mark_validated -----> END
      --[not validation_passed]-> mark_needs_review --> END
```

`validate_and_consolidate` no longer sets the final status directly — it
returns a `validation_passed: bool`, and the conditional edge routes to one
of two trivial terminal nodes (`mark_validated` / `mark_needs_review`) that
set `status`. This run resolved to:

**Final status: `validated`**

(confirmed from the structlog line `estimation_graph_completed` —
`status=validated`, `nodes_executed=5` — the 2 terminal nodes execute but do
not append their own `trace` entries, so the trace list stays at 5, one per
real pipeline node.)

## Node-by-node execution trace

| # | Node | Duration (ms) | Input | Output |
|---|------|---------------:|-------|--------|
| 1 | `extract_requirements` | 31,798 | transcript (2299 chars) | 27 requirement(s) |
| 2 | `classify_components` | 41,126 | 27 requirement(s) | 9 component(s) |
| 3 | `search_budgets` | 3,566 | 9 component(s) | 77 chunk(s) across 9 component(s) |
| 4 | `generate_estimate` | 278,705 | 9 search result(s) | confidence=medium |
| 5 | `validate_and_consolidate` | 3 | confidence=medium | validation_passed=True |

Total wall-clock for the 5 traced nodes: ~355.2s (dominated by node 4,
`gpt-5` with `reasoning_effort="high"` and a large assembled context block
from 77 retrieved chunks).

## Logfire console span trace

Logfire is configured with `send_to_logfire="if-token-present"` and no
`LOGFIRE_TOKEN` is set in this project, so nothing ships to Logfire Cloud;
by default the SDK still prints one console line per span locally. Each of
the 5 real pipeline nodes is wrapped in `logfire.span(...)` in `graph.py`;
the 2 terminal nodes (`mark_validated` / `mark_needs_review`) are not
instrumented with spans (they only set a status string, no I/O) — this is
accurate as observed, not a gap.

```
00:32:46.099 extract_requirements
00:33:17.880 classify_components
00:33:59.006 search_budgets
00:34:02.576 generate_estimate
00:38:41.264 validate_and_consolidate
```

(Timestamps are wall-clock UTC-ish container time; they line up with the
node trace table above — one line per node, in graph order, proving the
instrumentation fires once per node on a real run.)

## Full `Estimate` response body

```json
{
  "total_engineer_days": 299,
  "modules": [
    {
      "name": "Core Domain Services (Node.js + MongoDB)",
      "description": "ERP-like backend microservices for inventory, accounting/GL, and supplier interactions.",
      "tasks": [
        {
          "name": "Inventory service API",
          "description": "CRUD, availability, reservations and inventory adjustments API in Node.js.",
          "engineer_days": 19,
          "sources": [16]
        },
        {
          "name": "Real-time inventory sync pipeline",
          "description": "Ingest and synchronize inventory changes in near real-time across systems.",
          "engineer_days": 15,
          "sources": [26]
        },
        {
          "name": "Supplier interactions & purchase orders service",
          "description": "Vendor onboarding, POs, state machines for approvals and fulfillment.",
          "engineer_days": 22,
          "sources": [21]
        },
        {
          "name": "Accounting & GL posting service",
          "description": "Journal entries orchestration, posting flows and validations.",
          "engineer_days": 18,
          "sources": [17]
        }
      ]
    },
    {
      "name": "Data & Integrations",
      "description": "SAP bi-directional sync, logistics provider API, and EDI for suppliers.",
      "tasks": [
        {
          "name": "SAP adapter design & contracts (anti-corruption layer)",
          "description": "Define boundary, contracts and adapters separating domain from SAP specifics.",
          "engineer_days": 7,
          "sources": [54]
        },
        {
          "name": "SAP event translation & mapping",
          "description": "Implement request/response and event mapping between domain and SAP objects.",
          "engineer_days": 10,
          "sources": [54]
        },
        {
          "name": "SAP edge cases & regression tests",
          "description": "Handle legacy quirks and create regression tests around known patterns.",
          "engineer_days": 8,
          "sources": [54]
        },
        {
          "name": "SAP master data sync – materials",
          "description": "Scheduled ETL for materials master with transformations.",
          "engineer_days": 7,
          "sources": [198]
        },
        {
          "name": "SAP master data sync – customers/vendors",
          "description": "Scheduled ETL for business partner master data.",
          "engineer_days": 6,
          "sources": [215]
        },
        {
          "name": "SAP transaction sync – sales orders",
          "description": "Bi-directional sync for sales orders with conflict handling.",
          "engineer_days": 6,
          "sources": [110]
        },
        {
          "name": "SAP transaction sync – goods movements",
          "description": "Sync for goods receipt/issue and movement types.",
          "engineer_days": 6,
          "sources": [128]
        },
        {
          "name": "SAP financials sync – GL entries",
          "description": "Exchange of GL postings to/from SAP with batching.",
          "engineer_days": 4,
          "sources": [79]
        },
        {
          "name": "Logistics provider integration – shipment tracking service",
          "description": "Node.js service integrating provider API for status, labels, and tracking events.",
          "engineer_days": 19,
          "sources": [16]
        },
        {
          "name": "Logistics webhooks & event ingestion",
          "description": "Receive, validate and persist shipment status webhooks with retries.",
          "engineer_days": 4,
          "sources": [79]
        },
        {
          "name": "EDI parsing & mapping",
          "description": "Inbound EDI message parsing (e.g., PO, ASN) and mapping to domain.",
          "engineer_days": 12,
          "sources": [54]
        },
        {
          "name": "EDI outbound & acknowledgements",
          "description": "Generate outbound EDI, handle ACKs, retries and reconciliation.",
          "engineer_days": 13,
          "sources": [54]
        }
      ]
    },
    {
      "name": "Mobile Apps (iOS & Android, native)",
      "description": "Sales-facing native apps with offline capability and push notifications.",
      "tasks": [
        {
          "name": "iOS app shell & navigation",
          "description": "Project scaffolding, navigation, DI and networking setup.",
          "engineer_days": 5,
          "sources": [160]
        },
        {
          "name": "iOS offline sync engine integration",
          "description": "Local store, delta sync, conflict handling and retry logic.",
          "engineer_days": 15,
          "sources": [26]
        },
        {
          "name": "iOS push notifications (APNs)",
          "description": "Register device, token management and push handling.",
          "engineer_days": 8,
          "sources": [4]
        },
        {
          "name": "iOS sales workflows UI",
          "description": "Quote/order capture, item lookup and cart-like review screens.",
          "engineer_days": 8,
          "sources": [19]
        },
        {
          "name": "Android app shell & navigation",
          "description": "Project scaffolding, navigation, DI and networking setup.",
          "engineer_days": 4,
          "sources": [120]
        },
        {
          "name": "Android offline sync engine integration",
          "description": "Local store, delta sync, conflict handling and retry logic.",
          "engineer_days": 15,
          "sources": [26]
        },
        {
          "name": "Android push notifications (FCM)",
          "description": "Register device, token management and push handling.",
          "engineer_days": 8,
          "sources": [4]
        },
        {
          "name": "Android sales workflows UI",
          "description": "Quote/order capture, item lookup and cart-like review screens.",
          "engineer_days": 8,
          "sources": [19]
        }
      ]
    },
    {
      "name": "Notifications & Messaging (Backend)",
      "description": "Event-driven push/email notifications service supporting APNs/FCM and email.",
      "tasks": [
        {
          "name": "Notifications service",
          "description": "Build event-driven notification service with device tokens, templates and delivery.",
          "engineer_days": 8,
          "sources": [4]
        }
      ]
    },
    {
      "name": "Infrastructure & DevOps",
      "description": "Kubernetes-based platform with observability, IaC and CI/CD.",
      "tasks": [
        {
          "name": "Kubernetes base manifests & RBAC",
          "description": "Namespaces, deployments, services, RBAC and network policies.",
          "engineer_days": 3,
          "sources": [164, 77]
        },
        {
          "name": "Autoscaling & rollout strategies",
          "description": "HPA/VPA, canary/blue-green rollout configuration.",
          "engineer_days": 2,
          "sources": [164, 77]
        },
        {
          "name": "Ingress & TLS termination",
          "description": "Ingress controller, TLS certificates and routing.",
          "engineer_days": 2,
          "sources": [164, 77]
        },
        {
          "name": "Microservices CI/CD pipelines",
          "description": "Reusable build/test/deploy pipelines and environments.",
          "engineer_days": 5,
          "sources": [165]
        },
        {
          "name": "Mobile CI/CD pipelines",
          "description": "iOS/Android build, signing and release pipelines.",
          "engineer_days": 2,
          "sources": [244]
        },
        {
          "name": "Observability – metrics & dashboards",
          "description": "Prometheus/Grafana setup with service dashboards.",
          "engineer_days": 3,
          "sources": [195]
        },
        {
          "name": "Observability – structured logging",
          "description": "Centralized log shipping and correlation.",
          "engineer_days": 3,
          "sources": [122]
        },
        {
          "name": "Observability – alerting & runbooks",
          "description": "Alert rules, SLOs and incident runbooks.",
          "engineer_days": 4,
          "sources": [93]
        },
        {
          "name": "IaC – core cloud provisioning",
          "description": "Terraform for VPC, subnets, clusters and persistence.",
          "engineer_days": 5,
          "sources": [176]
        },
        {
          "name": "IaC – env setup & secrets management",
          "description": "Environment configuration and secrets management (e.g., AWS).",
          "engineer_days": 4,
          "sources": [162]
        }
      ]
    },
    {
      "name": "Project Management",
      "description": "Discovery, planning, sprint ceremonies and stakeholder reporting.",
      "tasks": [
        {
          "name": "Discovery & requirements",
          "description": "Workshops, scope definition and acceptance criteria.",
          "engineer_days": 5,
          "sources": [167]
        },
        {
          "name": "Coordination & reporting",
          "description": "Sprint ceremonies, reporting, risk/issue management.",
          "engineer_days": 6,
          "sources": [203]
        }
      ]
    }
  ],
  "duration_weeks": 24,
  "sources": [
    {"source_id": 16, "relevance": "primary", "used_for": "Node-based microservice API complexity analog (inventory service, shipment tracking)."},
    {"source_id": 17, "relevance": "primary", "used_for": "Accounting orchestration microservice complexity analog."},
    {"source_id": 21, "relevance": "primary", "used_for": "Supplier interactions and fulfillment state-machine complexity analog."},
    {"source_id": 26, "relevance": "primary", "used_for": "Real-time inventory synchronization and offline sync complexity analog."},
    {"source_id": 54, "relevance": "primary", "used_for": "Anti-corruption/adapter layer effort for SAP and EDI integrations."},
    {"source_id": 198, "relevance": "primary", "used_for": "ETL/data sync job effort (SAP master data)."},
    {"source_id": 215, "relevance": "primary", "used_for": "ETL/data sync job effort (SAP master data)."},
    {"source_id": 110, "relevance": "primary", "used_for": "ETL/data sync job effort (SAP transactions)."},
    {"source_id": 128, "relevance": "primary", "used_for": "ETL/data sync job effort (SAP transactions)."},
    {"source_id": 79, "relevance": "primary", "used_for": "ETL/data sync job effort (GL sync) and webhook ingestion analog."},
    {"source_id": 4, "relevance": "primary", "used_for": "Push and email notifications (APNs/FCM + email) effort baseline."},
    {"source_id": 164, "relevance": "primary", "used_for": "Kubernetes manifests, autoscaling and rollout configuration."},
    {"source_id": 77, "relevance": "supporting", "used_for": "Container orchestration effort cross-check."},
    {"source_id": 165, "relevance": "primary", "used_for": "CI/CD pipeline setup for microservices."},
    {"source_id": 244, "relevance": "supporting", "used_for": "CI/CD pipelines for mobile apps."},
    {"source_id": 195, "relevance": "primary", "used_for": "Observability (metrics/dashboards) baseline effort."},
    {"source_id": 122, "relevance": "supporting", "used_for": "Observability (structured logging) baseline effort."},
    {"source_id": 93, "relevance": "supporting", "used_for": "Observability (alerting) baseline effort."},
    {"source_id": 176, "relevance": "primary", "used_for": "Terraform IaC provisioning effort."},
    {"source_id": 162, "relevance": "supporting", "used_for": "IaC for cloud environment setup."},
    {"source_id": 167, "relevance": "primary", "used_for": "Discovery & requirements workshops effort."},
    {"source_id": 203, "relevance": "primary", "used_for": "Ongoing coordination and reporting effort."},
    {"source_id": 160, "relevance": "supporting", "used_for": "App shell/navigation complexity analog (used for iOS)."},
    {"source_id": 120, "relevance": "supporting", "used_for": "App shell/navigation complexity analog (used for Android)."},
    {"source_id": 19, "relevance": "supporting", "used_for": "Sales workflow UI complexity analog."}
  ],
  "assumptions": [
    {
      "description": "All source estimates are in hours; converted at 1 engineer-day = 8 hours and rounded up to whole days.",
      "impact": "high",
      "rationale": "Required to express hours-based historical tasks as engineer-days consistently."
    },
    {
      "description": "Mobile app estimates use SPA scaffolding and PWA storefront as analogs for native app shell and baseline sales UI.",
      "impact": "medium",
      "rationale": "No native mobile references were provided; closest historical components are SPA scaffolding and a PWA."
    },
    {
      "description": "MVP ERP scope includes inventory, supplier PO management, and basic accounting GL posting only; excludes advanced financial modules (AR/AP aging, tax engines beyond basics), procurement analytics and complex workflows.",
      "impact": "high",
      "rationale": "Keeps the domain scope aligned with available analog sources."
    },
    {
      "description": "SAP integration covers master data (materials, customers/vendors) and transactions (sales orders, goods movements, GL entries) as itemized; other SAP modules/IDocs/BAPIs are out of scope for MVP.",
      "impact": "medium",
      "rationale": "Aligns effort to ETL/adapter tasks cited; broader SAP scope would increase effort materially."
    },
    {
      "description": "Single Kubernetes cluster with staging and production environments; no multi-region DR, service mesh, or zero-trust networking beyond basic network policies.",
      "impact": "medium",
      "rationale": "Infra scope sized to container orchestration, IaC and observability sources."
    },
    {
      "description": "Security hardening (penetration testing, SSO/IdP integration, WAF, secrets rotation) is not estimated here and will be planned separately.",
      "impact": "high",
      "rationale": "No directly comparable historical security-hardening source provided."
    },
    {
      "description": "QA/testing effort is embedded within each task; no separate dedicated QA automation backlog or performance testing suite is budgeted.",
      "impact": "medium",
      "rationale": "No explicit QA/testing sources were provided beyond functional components."
    },
    {
      "description": "No bulk legacy data migration other than the enumerated syncs; any historical backfill or one-off migrations are excluded.",
      "impact": "medium",
      "rationale": "Prevents underestimation from hidden data-migration scope."
    },
    {
      "description": "Ongoing post-MVP support and iteration are not included in the engineer-days; this covers MVP delivery only.",
      "impact": "high",
      "rationale": "The brief notes ongoing development; this estimate isolates MVP build-out."
    }
  ],
  "confidence": "medium",
  "reasoning": "The estimate decomposes the requested ERP-style backend, integrations, mobile apps, and DevOps into modules benchmarked against close historical analogs. Node-based microservices for inventory, accounting and supplier workflows are anchored to headless commerce services (catalog API, cart/checkout, split order management: sources 16, 17, 21). Real-time and offline synchronization leverages the grocery inventory sync (source 26) as a proxy for delta-sync complexity. SAP and EDI integrations are grounded in an anti-corruption/adapter layer from core-banking modernization (source 54) plus multiple ETL/data-sync jobs (sources 198, 215, 110, 128, 79) to reflect bi-directional data movement and transformations. Logistics/shipment tracking is sized using a Node API service analog (source 16) with webhook ingestion (source 79). Notifications leverage a finance mobile-banking push/email component (source 4). The Kubernetes platform, CI/CD, observability and IaC tasks map directly to infra sources (164, 77, 165, 244, 195, 122, 93, 176, 162). For native mobile apps, no direct historical native references were provided; we therefore analogize app shell/UI effort from SPA/PWA components (sources 160, 120, 19) and use the same notifications/inventory-sync baselines (sources 4, 26). Hours were converted to engineer-days at 8h/day and rounded up (assumption), and tasks were decomposed so their sums reflect the derived efforts. Given the cross-domain mapping (ecommerce/finance/industrial to ERP/mobile), confidence is medium: infra and integrations are well-grounded, while native-mobile and some domain specifics rely on closest-fit analogs rather than exact matches.",
  "insufficient_context_explanation": null
}
```

## Notes on how this trace was captured

- The graph run was triggered through the real HTTP endpoint
  (`POST /v1/estimate/from-transcript-graph`), not a bypass script — this
  reflects exactly what a real caller (Rails cliente, or a direct curl/httpie
  call in the live session) gets.
- The node-by-node trace and the `status` field are internal to
  `GraphState` and are not part of the public `Estimate` response schema;
  they were captured from the service's structlog output
  (`estimation_graph_started` / `estimation_graph_completed` /
  a temporary one-off debug log of `final_state["trace"]`, added and
  reverted for this capture — no lasting code change).
- The historical corpus was seeded into `estimator-postgres` before this run
  (`scripts/query_examples.py` for the base Session-8 corpus,
  `scripts/build_task_corpus.py --ingest` for the Session-9 task-level
  corpus) — 29 documents / 282 chunks total. Without this, retrieval returns
  zero hits and the pipeline short-circuits to an `insufficient`-confidence
  estimate (as observed in an earlier verification pass against
  `sample_transcript_simple.txt` before seeding).

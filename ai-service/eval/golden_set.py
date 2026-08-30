"""Golden set: 5 representative queries with manual relevance annotations.

Each query is a realistic project description. Ground truth lists the chunk IDs
(from the budget dataset) that are genuinely relevant for estimation.

Golden set is used to measure precision@5 and latency across 4 search configs:
- A: vector search, no reranking
- B: hybrid search (RRF), no reranking
- C: vector search + cross-encoder reranking
- D: hybrid search (RRF) + cross-encoder reranking
"""

from datetime import datetime
from dataclasses import dataclass

@dataclass
class GoldenQuery:
    """A query with ground truth relevant chunk IDs."""
    id: str
    query_text: str
    description: str
    # Ground truth: set of chunk IDs that are relevant for this project
    relevant_chunk_ids: set[int]


# Golden set: 5 realistic project descriptions
GOLDEN_QUERIES = [
    GoldenQuery(
        id="q1",
        query_text="Desarrollo de aplicación web e-commerce con pasarela de pagos y carrito de compra",
        description="Mid-size e-commerce platform with payment integration, user auth, product catalog, and shopping cart. Similar projects: online retail systems.",
        relevant_chunk_ids={17, 12, 58, 9, 19, 57, 16},
    ),
    GoldenQuery(
        id="q2",
        query_text="Sistema de gestión de inventario en tiempo real con reportes y análisis de datos",
        description="Inventory management system with real-time tracking, reporting, and business analytics dashboard. Domain: supply chain software.",
        relevant_chunk_ids={49, 38, 46, 26},
    ),
    GoldenQuery(
        id="q3",
        query_text="Aplicación móvil iOS y Android para reservas de servicios profesionales",
        description="Cross-platform mobile app for booking professional services (cleaners, plumbers, etc). Features: calendar, ratings, payments.",
        relevant_chunk_ids=set(),
    ),
    GoldenQuery(
        id="q4",
        query_text="Portal de gestión documental con OCR, búsqueda full-text y control de acceso basado en roles",
        description="Document management platform with OCR, full-text search, role-based access control, versioning. Enterprise solution.",
        relevant_chunk_ids=set(),
    ),
    GoldenQuery(
        id="q5",
        query_text="Red social interna con chat en tiempo real, notificaciones push y sincronización offline",
        description="Internal social network / enterprise chat with real-time messaging, push notifications, and offline-first sync. Mobile + web.",
        relevant_chunk_ids=set(),
    ),
]


def print_golden_set_summary():
    """Print summary of golden set for manual annotation."""
    print("=" * 80)
    print("GOLDEN SET SUMMARY — Manual Annotation Required")
    print("=" * 80)
    print()
    print("Instructions:")
    print("1. Run the evaluation script first with enable_discovery=True")
    print("2. Review retrieved results for each query")
    print("3. Manually identify which chunk IDs are truly relevant")
    print("4. Update relevant_chunk_ids in this file")
    print("5. Re-run evaluation with enable_discovery=False for final metrics")
    print()

    for i, q in enumerate(GOLDEN_QUERIES, 1):
        print(f"Query {i}: {q.id}")
        print(f"  Text: {q.query_text}")
        print(f"  Domain: {q.description}")
        print(f"  Relevant chunks (to annotate): {q.relevant_chunk_ids if q.relevant_chunk_ids else 'NOT YET ANNOTATED'}")
        print()

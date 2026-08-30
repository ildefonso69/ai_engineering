"""Demonstrate citation verification with intentional dangling references.

This script shows how :func:`verify_citations` detects when an estimate cites
chunk_ids that were never in the retrieved context — a grounding failure that
would otherwise be invisible.

The schema supports it (SourceReference, TaskItem.grounded, TaskItem.sources).
The prompt enforces it (instructions to cite verbatim and only real ids).
This demo proves the verification catches violations.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.generation.rag.schemas import Estimate, SourceReference, TaskItem, WorkModule
from app.generation.rag.validation import verify_citations

# Create a realistic estimate with mixed grounding.
estimate = Estimate(
    total_engineer_days=50,
    duration_weeks=12,
    modules=[
        WorkModule(
            name="Authentication",
            description="User login and session management.",
            tasks=[
                TaskItem(
                    name="OAuth 2.0 provider integration",
                    description="Support Google and GitHub login.",
                    engineer_days=10,
                    grounded=True,
                    # These sources are REAL (chunk ids 101, 102).
                    sources=[
                        SourceReference(
                            chunk_id="101",
                            document_id="BUD-2024-001",
                            evidence="OAuth 2.0 integration with external providers: 10 engineer-days.",
                        ),
                    ],
                ),
                TaskItem(
                    name="Multi-factor authentication",
                    description="TOTP and SMS 2FA options.",
                    engineer_days=8,
                    grounded=True,
                    # This source is DANGLING: chunk id 999 was never retrieved.
                    sources=[
                        SourceReference(
                            chunk_id="999",
                            document_id="BUD-FAKE-999",
                            evidence="Multi-factor authentication support: 8 engineer-days.",
                        ),
                    ],
                ),
            ],
        ),
        WorkModule(
            name="User Profiles",
            description="Profile data and settings.",
            tasks=[
                TaskItem(
                    name="Profile data storage and update",
                    description="User attributes, preferences, avatar upload.",
                    engineer_days=6,
                    grounded=False,
                    # No sources when grounded=False (correctly).
                    sources=[],
                ),
            ],
        ),
    ],
    confidence="medium",
    reasoning="OAuth and MFA are common; profile update has no direct precedent.",
    sources=[],
    assumptions=[],
    requires_human_review=False,
    review_reasons=[],
)

# Simulate the chunks that were actually retrieved and placed in the context.
# Only chunk ids 101 and 102 made it into the context block.
retrieved_chunk_ids = {"101", "102"}

# Run verification.
report = verify_citations(estimate, retrieved_chunk_ids)

print("=" * 90)
print("CITATION VERIFICATION REPORT")
print("=" * 90)
print(f"Total lines: {report.total_lines}")
print(f"Grounded lines (all citations real): {report.grounded_lines}")
print(f"Dangling lines (≥1 fabricated citation): {report.dangling_lines}")
print(f"Insufficient lines (no source data): {report.insufficient_lines}")
print(f"Verified citations: {report.verified_citations}")
print(f"Dangling citation IDs: {report.dangling_citations}")
print()

print("Per-line breakdown:")
print("-" * 90)
for line in report.lines:
    status_badge = {
        "grounded": "[✓ GROUNDED]",
        "dangling": "[✗ DANGLING]",
        "insufficient": "[- NO DATA]",
    }.get(line.status, "[?]")

    print(f"{status_badge} {line.module} :: {line.component}")
    if line.cited_chunk_ids:
        print(f"  Cited: {line.cited_chunk_ids}")
    if line.dangling_chunk_ids:
        print(f"  ! DANGLING (never retrieved): {line.dangling_chunk_ids}")
    print()

print("=" * 90)
if report.has_dangling:
    print(
        "RESULT: Estimate contains dangling citations. "
        "These chunk IDs were cited but never in the context:\n"
    )
    for dangling_id in report.dangling_citations:
        print(f"  - {dangling_id}")
    print("\nThis is a grounding failure: the LLM cited a source it never saw.")
else:
    print("RESULT: All citations are real. The estimate is properly grounded.")

print("=" * 90)

# Save report as JSON for downstream processing.
report_json = {
    "total_lines": report.total_lines,
    "grounded_lines": report.grounded_lines,
    "dangling_lines": report.dangling_lines,
    "insufficient_lines": report.insufficient_lines,
    "verified_citations": report.verified_citations,
    "has_dangling": report.has_dangling,
    "dangling_citations": report.dangling_citations,
    "lines": [
        {
            "module": line.module,
            "component": line.component,
            "status": line.status,
            "cited_chunk_ids": line.cited_chunk_ids,
            "dangling_chunk_ids": line.dangling_chunk_ids,
        }
        for line in report.lines
    ],
}

output_path = Path(__file__).parent / "citation_verification_example.json"
with open(output_path, "w") as f:
    json.dump(report_json, f, indent=2)
print(f"\nReport saved to {output_path}")

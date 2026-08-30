"""Discovery script: run all golden queries and show retrieved chunks for manual annotation."""

import asyncio
import httpx
import json
from eval.golden_set import GOLDEN_QUERIES

async def discover():
    """Run all queries and print retrieved chunks for annotation."""
    api_key = input("Enter RETRIEVAL_API_KEY: ").strip()
    api_url = "http://localhost:8000"

    async with httpx.AsyncClient(base_url=api_url, headers={"X-API-Key": api_key}, timeout=30.0) as client:
        for query in GOLDEN_QUERIES:
            print(f"\n{'='*80}")
            print(f"Query {query.id}: {query.query_text}")
            print(f"Domain: {query.description}")
            print(f"{'='*80}\n")

            try:
                # Search (vector)
                resp = await client.post(
                    "/v1/retrieval/search",
                    json={
                        "query_text": query.query_text,
                        "collection": "budget",
                        "top_k": 10,
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])

                print(f"Retrieved {len(results)} chunks:\n")
                chunk_ids = []
                for i, r in enumerate(results, 1):
                    chunk = r["chunk"]
                    chunk_id = chunk["id"]
                    chunk_ids.append(chunk_id)
                    content_preview = chunk["content"][:150].replace("\n", " ")
                    print(f"{i}. [Chunk ID: {chunk_id}]")
                    print(f"   Type: {chunk['chunk_type']}")
                    print(f"   Content: {content_preview}...")
                    print()

                print("-" * 80)
                print(f"Chunk IDs to choose from: {chunk_ids}")
                annotation = input("Which chunk IDs are RELEVANT? (comma-separated, or press Enter to skip): ").strip()

                if annotation:
                    try:
                        relevant = set(map(int, annotation.split(",")))
                        print(f"✓ Marked as relevant: {relevant}\n")
                    except ValueError:
                        print("⚠️  Invalid format. Skipping this query.\n")
                else:
                    print("Skipped.\n")

            except Exception as e:
                print(f"ERROR: {e}\n")

if __name__ == "__main__":
    asyncio.run(discover())

"""Auto discovery: run all queries and save results to JSON for manual review."""

import asyncio
import httpx
import json
import sys
import os

# Fix import path
sys.path.insert(0, os.path.dirname(__file__))
from golden_set import GOLDEN_QUERIES

async def discover_auto(api_key: str):
    """Run all queries and save retrieved chunks to JSON."""
    api_url = "http://localhost:8000"
    results = {}

    async with httpx.AsyncClient(base_url=api_url, headers={"X-API-Key": api_key}, timeout=30.0) as client:
        for query in GOLDEN_QUERIES:
            print(f"Querying {query.id}: {query.query_text[:60]}...", end=" ", flush=True)

            try:
                resp = await client.post(
                    "/v1/retrieval/search",
                    json={
                        "query_text": query.query_text,
                        "collection": "budget",
                        "top_k": 10,
                    },
                )
                resp.raise_for_status()
                retrieved = resp.json().get("chunks", [])

                results[query.id] = {
                    "query_text": query.query_text,
                    "domain": query.description,
                    "chunks": [
                        {
                            "id": r["id"],
                            "type": r["chunk_type"],
                            "content": r["content"][:200] if r["content"] else "",
                        }
                        for r in retrieved
                    ],
                    "chunk_ids": [r["id"] for r in retrieved],
                }
                print(f"OK {len(retrieved)} chunks")

            except Exception as e:
                print(f"ERROR: {e}")
                results[query.id] = {"error": str(e)}

    # Save to file
    output_file = os.path.join(os.path.dirname(__file__), "discovery_results.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to {output_file}")
    print("Next: Review the file and update golden_set.py with relevant_chunk_ids")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python discover_auto.py <RETRIEVAL_API_KEY>")
        sys.exit(1)

    asyncio.run(discover_auto(sys.argv[1]))

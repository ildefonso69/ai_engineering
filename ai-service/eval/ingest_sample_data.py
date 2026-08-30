"""Quick script to ingest sample budget data into the vector DB via HTTP."""

import asyncio
import json
import httpx

async def ingest_sample_data():
    """Load budgets_sample.json and ingest each budget via HTTP."""
    import os

    api_url = "http://localhost:8000"
    api_key = "demo-retrieval-key"

    # Load sample data
    data_file = os.path.join(
        os.path.dirname(__file__), "../data/budgets_sample.json"
    )
    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    budgets = data if isinstance(data, list) else [data]

    print(f"Found {len(budgets)} budgets to ingest")

    async with httpx.AsyncClient(
        base_url=api_url,
        headers={"X-API-Key": api_key},
        timeout=60.0
    ) as client:
        for i, budget_dict in enumerate(budgets, 1):
            try:
                budget_id = budget_dict.get("budget_id", f"UNKNOWN-{i}")
                source_path = f"sample/{budget_id}"

                payload = {
                    "source_path": source_path,
                    "document_type": "historical_budget",
                    "content": budget_dict,
                    "chunk_type": "budget_component",
                }

                resp = await client.post("/embeddings/ingest", json=payload)
                resp.raise_for_status()
                result = resp.json()
                chunks = result.get("chunks_created", 0)
                print(f"{i}. [{budget_id}] OK - {chunks} chunks")
            except Exception as e:
                print(f"{i}. [{budget_dict.get('budget_id', 'UNKNOWN')}] ERROR: {str(e)[:100]}")

    print("\nIngestion complete!")

if __name__ == "__main__":
    asyncio.run(ingest_sample_data())

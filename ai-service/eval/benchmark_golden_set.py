"""Benchmark golden set against 4 search configurations.

Configs:
- A: Vector search, no reranking
- B: Hybrid search (RRF), no reranking
- C: Vector search + cross-encoder reranking
- D: Hybrid search (RRF) + cross-encoder reranking

Measures: Precision@5, latency, and overall metrics.

Usage:
    python eval/benchmark_golden_set.py \
        --api-url http://localhost:8000 \
        --api-key YOUR_RETRIEVAL_API_KEY \
        --collection budget
"""

import asyncio
import httpx
import json
import time
from dataclasses import dataclass, field
from typing import Optional
import argparse
import sys
import os

# Fix import path
sys.path.insert(0, os.path.dirname(__file__))
from golden_set import GOLDEN_QUERIES, print_golden_set_summary


@dataclass
class QueryResult:
    """Result of a single search configuration on a query."""
    config: str  # A, B, C, D
    query_id: str
    retrieved_chunk_ids: list[int]
    latency_ms: float
    search_method: str  # "vector" or "hybrid"
    reranked: bool


@dataclass
class EvalMetrics:
    """Evaluation metrics for a single config."""
    config: str
    search_method: str
    reranked: bool
    num_queries: int
    precision_at_5_per_query: list[float] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def mean_precision_at_5(self) -> float:
        return sum(self.precision_at_5_per_query) / len(self.precision_at_5_per_query) if self.precision_at_5_per_query else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0


class GoldenSetBenchmark:
    """Run benchmark across 4 configs."""

    def __init__(self, api_url: str, api_key: str, collection: str = "budget"):
        self.api_url = api_url
        self.api_key = api_key
        self.collection = collection
        self.client = httpx.AsyncClient(
            base_url=api_url,
            headers={"X-API-Key": api_key},
            timeout=60.0,
        )

    async def search_vector(self, query_text: str, rerank: bool = False) -> tuple[list[int], float]:
        """Config A (vector, no rerank) or C (vector + rerank)."""
        start = time.time()
        response = await self.client.post(
            "/v1/retrieval/search",
            json={
                "query_text": query_text,
                "collection": self.collection,
                "top_k": 5 if not rerank else 10,
            },
        )
        response.raise_for_status()
        data = response.json()
        latency = (time.time() - start) * 1000

        # Extract chunk IDs from response (format: {"chunks": [{"id": ..., ...}]})
        chunk_ids = [r["id"] for r in data.get("chunks", [])]
        if rerank:
            # TODO: add reranking step here for config C
            pass
        return chunk_ids[:5], latency

    async def search_hybrid(self, query_text: str, rerank: bool = False) -> tuple[list[int], float]:
        """Config B (hybrid, no rerank) or D (hybrid + rerank)."""
        start = time.time()
        response = await self.client.post(
            "/v1/retrieval/hybrid-search",
            json={
                "query_text": query_text,
                "collection": self.collection,
                "top_k": 10 if rerank else 5,
                "enable_reranking": rerank,
                "rerank_model": "mmarco-mMiniLMv2-L12-H384-v1" if rerank else None,
                "rerank_top_k": 5 if rerank else None,
            },
        )
        response.raise_for_status()
        data = response.json()
        latency = (time.time() - start) * 1000

        chunk_ids = [r["chunk"]["id"] for r in data.get("results", [])]
        return chunk_ids[:5], latency

    async def run_benchmark(self) -> dict:
        """Run all 4 configs on all queries."""
        metrics = {
            "A": EvalMetrics("A", "vector", False, len(GOLDEN_QUERIES)),
            "B": EvalMetrics("B", "hybrid", False, len(GOLDEN_QUERIES)),
            "C": EvalMetrics("C", "vector", True, len(GOLDEN_QUERIES)),
            "D": EvalMetrics("D", "hybrid", True, len(GOLDEN_QUERIES)),
        }

        for query in GOLDEN_QUERIES:
            if not query.relevant_chunk_ids:
                print(f"SKIP: Query {query.id} has no ground truth annotations. Skipping...")
                continue

            print(f"\nEvaluating query {query.id}: {query.query_text[:60]}...")

            # Config A: Vector, no rerank
            try:
                chunk_ids, latency = await self.search_vector(query.query_text, rerank=False)
                precision = self._compute_precision_at_5(chunk_ids, query.relevant_chunk_ids)
                metrics["A"].precision_at_5_per_query.append(precision)
                metrics["A"].latencies_ms.append(latency)
                print(f"  A (vector, no rerank): P@5={precision:.2f}, latency={latency:.1f}ms")
            except Exception as e:
                print(f"  A: ERROR — {e}")

            # Config B: Hybrid, no rerank
            try:
                chunk_ids, latency = await self.search_hybrid(query.query_text, rerank=False)
                precision = self._compute_precision_at_5(chunk_ids, query.relevant_chunk_ids)
                metrics["B"].precision_at_5_per_query.append(precision)
                metrics["B"].latencies_ms.append(latency)
                print(f"  B (hybrid, no rerank): P@5={precision:.2f}, latency={latency:.1f}ms")
            except Exception as e:
                print(f"  B: ERROR — {e}")

            # Config C: Vector, rerank
            try:
                chunk_ids, latency = await self.search_vector(query.query_text, rerank=True)
                precision = self._compute_precision_at_5(chunk_ids, query.relevant_chunk_ids)
                metrics["C"].precision_at_5_per_query.append(precision)
                metrics["C"].latencies_ms.append(latency)
                print(f"  C (vector + rerank): P@5={precision:.2f}, latency={latency:.1f}ms")
            except Exception as e:
                print(f"  C: ERROR — {e}")

            # Config D: Hybrid, rerank
            try:
                chunk_ids, latency = await self.search_hybrid(query.query_text, rerank=True)
                precision = self._compute_precision_at_5(chunk_ids, query.relevant_chunk_ids)
                metrics["D"].precision_at_5_per_query.append(precision)
                metrics["D"].latencies_ms.append(latency)
                print(f"  D (hybrid + rerank): P@5={precision:.2f}, latency={latency:.1f}ms")
            except Exception as e:
                print(f"  D: ERROR — {e}")

        await self.client.aclose()
        return metrics

    @staticmethod
    def _compute_precision_at_5(retrieved: list[int], relevant: set[int]) -> float:
        """Precision@5: fraction of top-5 that are relevant."""
        if not retrieved:
            return 0.0
        top_5 = set(retrieved[:5])
        if not top_5:
            return 0.0
        return len(top_5 & relevant) / len(top_5)

    @staticmethod
    def print_results_table(metrics: dict):
        """Print results as a formatted table."""
        print("\n" + "=" * 90)
        print("GOLDEN SET EVALUATION RESULTS")
        print("=" * 90)
        print()
        print(f"{'Config':<8} {'Search':<10} {'Reranked':<10} {'P@5 Mean':<12} {'Latency (ms)':<15}")
        print("-" * 90)

        for config in ["A", "B", "C", "D"]:
            m = metrics[config]
            search_label = m.search_method.capitalize()
            rerank_label = "Yes" if m.reranked else "No"
            p_at_5 = f"{m.mean_precision_at_5:.3f}"
            latency = f"{m.mean_latency_ms:.1f}"
            print(f"{config:<8} {search_label:<10} {rerank_label:<10} {p_at_5:<12} {latency:<15}")

        print()
        print("Key findings:")
        best_p_config = max(metrics, key=lambda c: metrics[c].mean_precision_at_5)
        best_latency_config = min(metrics, key=lambda c: metrics[c].mean_latency_ms)
        print(f"  Best precision: Config {best_p_config} ({metrics[best_p_config].mean_precision_at_5:.3f})")
        print(f"  Fastest latency: Config {best_latency_config} ({metrics[best_latency_config].mean_latency_ms:.1f}ms)")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://localhost:8000", help="FastAPI base URL")
    parser.add_argument("--api-key", required=True, help="RETRIEVAL_API_KEY")
    parser.add_argument("--collection", default="budget", help="Chunk collection")
    args = parser.parse_args()

    print_golden_set_summary()

    benchmark = GoldenSetBenchmark(args.api_url, args.api_key, args.collection)
    metrics = await benchmark.run_benchmark()
    benchmark.print_results_table(metrics)

    # Save results to JSON
    results_file = os.path.join(os.path.dirname(__file__), "golden_set_results.json")
    results = {
        config: {
            "search_method": m.search_method,
            "reranked": m.reranked,
            "mean_precision_at_5": m.mean_precision_at_5,
            "mean_latency_ms": m.mean_latency_ms,
            "num_queries": m.num_queries,
        }
        for config, m in metrics.items()
    }
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {results_file}")


if __name__ == "__main__":
    asyncio.run(main())

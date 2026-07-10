#!/usr/bin/env python3
"""Benchmark catalog product search over a seeded SQLite database."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shopping_cli.core import catalog  # noqa: E402
from shopping_cli.db.session import db_session  # noqa: E402


SPECIALTIES = (
    "longjing",
    "oolong",
    "jasmine",
    "puer",
    "sencha",
    "matcha",
    "keemun",
    "tieguanyin",
)
CITIES = ("Hangzhou", "Shanghai", "Beijing", "Shenzhen")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="SQLite database path. Defaults to a temporary database.")
    parser.add_argument("--merchants", type=int, default=100)
    parser.add_argument("--products-per-merchant", type=int, default=50)
    parser.add_argument("--query", default="longjing")
    parser.add_argument("--city", default="")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--candidate-limit", type=int, default=1000)
    parser.add_argument("--skip-index-rebuild", action="store_true", help="Do not rebuild the FTS index before timing search.")
    return parser.parse_args(argv)


def seed_catalog(conn, merchants: int, products_per_merchant: int, run_id: str) -> int:
    product_count = 0
    for merchant_index in range(max(0, merchants)):
        city = CITIES[merchant_index % len(CITIES)]
        merchant_id = f"bench-{run_id}-seller-{merchant_index:04d}"
        catalog.create_merchant(
            conn,
            merchant_id=merchant_id,
            name=f"Benchmark Merchant {merchant_index:04d}",
            city=city,
            service_area=f"{city} Central",
            contact=f"bench-{merchant_index:04d}",
            tags=f"{city},benchmark",
        )
        for product_index in range(max(0, products_per_merchant)):
            specialty = SPECIALTIES[(merchant_index + product_index) % len(SPECIALTIES)]
            sku = f"bench-{run_id}-{merchant_index:04d}-{product_index:04d}"
            catalog.create_product(
                conn,
                merchant_id=merchant_id,
                sku=sku,
                title=f"{specialty.title()} Gift Box {merchant_index}-{product_index}",
                price=20 + ((merchant_index + product_index) % 180),
                stock=1 + ((merchant_index + product_index) % 20),
                category="tea",
                tags=f"{specialty},gift,benchmark",
                description=f"{specialty} benchmark search product",
                delivery_attributes="courier,same-city",
            )
            product_count += 1
    return product_count


def benchmark(conn, args: argparse.Namespace) -> dict[str, object]:
    durations_ms: list[float] = []
    last_results = []
    for _ in range(max(1, args.iterations)):
        start = time.perf_counter()
        last_results = catalog.search_products(
            conn,
            query=args.query,
            city=args.city,
            limit=max(0, args.limit),
            candidate_limit=max(0, args.candidate_limit),
        )
        durations_ms.append((time.perf_counter() - start) * 1000)
    return {
        "iterations": len(durations_ms),
        "min_ms": round(min(durations_ms), 3),
        "median_ms": round(statistics.median(durations_ms), 3),
        "max_ms": round(max(durations_ms), 3),
        "last_result_count": len(last_results),
        "last_result_skus": [item["sku"] for item in last_results[:5]],
    }


def run_with_db(db_file: Path, args: argparse.Namespace) -> dict[str, object]:
    run_id = str(time.time_ns())
    with db_session(db_file) as conn:
        fts_available = catalog.product_search_index_available(conn)
        product_count = seed_catalog(conn, args.merchants, args.products_per_merchant, run_id)
        index_before = catalog.product_search_index_stats(conn)
        rebuild_ms = 0.0
        rebuild_ok = False
        if fts_available and not args.skip_index_rebuild:
            rebuild_started = time.perf_counter()
            rebuild_ok = catalog.rebuild_product_search_index(conn)
            rebuild_ms = (time.perf_counter() - rebuild_started) * 1000
        index_after = catalog.product_search_index_stats(conn)
        result = benchmark(conn, args)
    return {
        "db": str(db_file),
        "seeded_products": product_count,
        "seeded_merchants": max(0, args.merchants),
        "fts_available": fts_available,
        "index_before": index_before,
        "index_after": index_after,
        "index_rebuild_ms": round(rebuild_ms, 3),
        "index_rebuild_ok": rebuild_ok,
        "query": args.query,
        "city": args.city,
        "limit": max(0, args.limit),
        "candidate_limit": max(0, args.candidate_limit),
        **result,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.db:
        output = run_with_db(Path(args.db).expanduser(), args)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            output = run_with_db(Path(tmp) / "shopping-benchmark.sqlite", args)
            output["db"] = "temporary"
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])

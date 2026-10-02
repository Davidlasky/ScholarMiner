#!/usr/bin/env python3
"""Seed a disposable ScholarMiner deployment from a completed benchmark index.

Run inside the worker container after an inverted-index Dataproc job finishes.
This intentionally does not scrape Google Scholar, so the Top-N comparison can
use a deterministic corpus. The script refuses to replace a non-empty index.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-output-task-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--session-task-id", required=True, help="UUID for the completed web session task")
    parser.add_argument("--papers-indexed", required=True, type=int)
    args = parser.parse_args()
    if args.papers_indexed < 1:
        parser.error("papers-indexed must be positive")
    try:
        uuid.UUID(args.session_task_id)
    except ValueError:
        parser.error("session-task-id must be a UUID")
    if os.environ.get("SCHOLARMINER_ALLOW_BENCHMARK_SEED") != "1":
        parser.error("set SCHOLARMINER_ALLOW_BENCHMARK_SEED=1 for a disposable benchmark deployment")
    if not args.source_url.startswith("https://scholar.google.com/") or "scholarminer-benchmark-" not in args.source_url:
        parser.error("source-url must be a dedicated scholarminer-benchmark- Scholar URL")
    if backend.count_terms() != 0:
        parser.error("the index is not empty; refusing to replace existing data")

    output_prefix = f"output/inverted_index/{args.index_output_task_id}"
    index_data = backend.parse_inverted_index(backend.read_output_from_gcs(output_prefix))
    if not index_data:
        raise RuntimeError("the benchmark inverted index is empty")
    backend.replace_inverted_index(index_data)
    backend.persist_to_redis(index_data)
    backend.persist_to_gcs(index_data)
    backend.mark_source_indexed(args.source_url)
    redis_client = backend.get_redis_client()
    cached_terms = redis_client.zcard("term_freq") if redis_client else 0
    if cached_terms != len(index_data):
        raise RuntimeError(
            f"Redis cache has {cached_terms} terms, expected {len(index_data)}"
        )
    backend.update_task_status(
        args.session_task_id,
        args.source_url,
        "COMPLETE",
        "Deterministic benchmark index seeded from a completed Hadoop job.",
        {"papers_indexed": args.papers_indexed, "num_terms": len(index_data)},
    )
    print(json.dumps({"indexed_terms": len(index_data), "redis_terms": cached_terms, "session_task_id": args.session_task_id}))


if __name__ == "__main__":
    main()

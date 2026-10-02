#!/usr/bin/env python3
"""Generate a deterministic TSV corpus for the ScholarMiner Top-N benchmark."""

import argparse
import random
from pathlib import Path


VOCABULARY = (
    "algorithm architecture backend cache cloud concurrency container data database "
    "deployment distributed engineering fault graph hadoop index infrastructure java "
    "kafka latency learning microservice network observability optimization pipeline "
    "postgres query ranking redis reliability research scalability search security "
    "software storage system terraform throughput vector worker"
).split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--papers", type=int, default=500)
    parser.add_argument("--words-per-paper", type=int, default=80)
    parser.add_argument("--seed", type=int, default=20260921)
    args = parser.parse_args()
    if args.papers < 1 or args.words_per_paper < 1:
        parser.error("papers and words-per-paper must be positive")

    rng = random.Random(args.seed)
    weights = [len(VOCABULARY) - index for index in range(len(VOCABULARY))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for index in range(args.papers):
            abstract = " ".join(
                rng.choices(VOCABULARY, weights=weights, k=args.words_per_paper)
            )
            output.write(
                f"{index + 1}\tBenchmark Paper {index + 1}\t0\t{abstract}"
                f"\thttps://example.invalid/paper/{index + 1}\n"
            )
    print(f"Generated {args.papers} papers at {args.output}")


if __name__ == "__main__":
    main()

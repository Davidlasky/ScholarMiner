#!/usr/bin/env python3
"""
Hadoop Streaming Mapper for Top-N Frequent Terms.

Input: Lines from the inverted index output:
    word\tdoc_id:title:citations:url:frequency|...

Output: word\ttotal_frequency

Computes the total frequency of each word across all documents.
"""

import sys


def main():
    """Read inverted index entries and compute total frequencies."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            parts = line.split("\t")
            if len(parts) < 2:
                continue

            word = parts[0]
            postings_str = parts[1]

            # Calculate total frequency across all documents
            total_freq = 0
            postings = postings_str.split("|")
            for posting in postings:
                fields = posting.split(":")
                if len(fields) >= 5:
                    try:
                        freq = int(fields[-1])
                        total_freq += freq
                    except ValueError:
                        continue

            # Emit: word\ttotal_frequency
            print(f"{word}\t{total_freq}")

        except Exception as e:
            sys.stderr.write(f"TopN Mapper error: {str(e)}\n")
            continue


if __name__ == "__main__":
    main()

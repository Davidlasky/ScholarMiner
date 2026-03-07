#!/usr/bin/env python3
"""
Hadoop Streaming Reducer for Top-N Frequent Terms.

Input (sorted by key): word\ttotal_frequency

Output: word\ttotal_frequency

Aggregates frequencies for the same word (in case of multiple mappers)
and outputs all word-frequency pairs. The Top-N selection is done
by the backend application after collecting all results.
"""

import sys


def main():
    """Read sorted mapper output and aggregate frequencies."""
    current_word = None
    current_freq = 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            parts = line.split("\t")
            if len(parts) < 2:
                continue

            word = parts[0]
            freq = int(parts[1])

            if current_word and current_word != word:
                # Emit the aggregated frequency for the previous word
                print(f"{current_word}\t{current_freq}")
                current_freq = 0

            current_word = word
            current_freq += freq

        except Exception as e:
            sys.stderr.write(f"TopN Reducer error: {str(e)}\n")
            continue

    # Emit the last word
    if current_word:
        print(f"{current_word}\t{current_freq}")


if __name__ == "__main__":
    main()

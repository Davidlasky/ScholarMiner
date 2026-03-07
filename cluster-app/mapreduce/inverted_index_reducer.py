#!/usr/bin/env python3
"""
Hadoop Streaming Reducer for Building Inverted Indices.

Input (sorted by key): word\tdoc_id\ttitle\tcitations\turl\tfrequency

Output: word\tdoc_id:title:citations:url:frequency,doc_id:title:citations:url:frequency,...

Aggregates all documents containing each word into an inverted index entry.
"""

import sys


def main():
    """Read sorted mapper output and build inverted index."""
    current_word = None
    postings = []

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            parts = line.split("\t")
            if len(parts) < 6:
                continue

            word = parts[0]
            doc_id = parts[1]
            title = parts[2]
            citations = parts[3]
            url = parts[4]
            frequency = parts[5]

            if current_word and current_word != word:
                # Emit the inverted index entry for the previous word
                emit_index(current_word, postings)
                postings = []

            current_word = word
            postings.append({
                "doc_id": doc_id,
                "title": title,
                "citations": citations,
                "url": url,
                "frequency": int(frequency),
            })

        except Exception as e:
            sys.stderr.write(f"Reducer error: {str(e)}\n")
            continue

    # Emit the last word
    if current_word:
        emit_index(current_word, postings)


def emit_index(word, postings):
    """Emit the inverted index entry for a word."""
    # Format: word\tdoc_id:title:citations:url:frequency|doc_id:title:citations:url:frequency|...
    posting_strs = []
    for p in postings:
        posting_str = f"{p['doc_id']}:{p['title']}:{p['citations']}:{p['url']}:{p['frequency']}"
        posting_strs.append(posting_str)

    print(f"{word}\t{'|'.join(posting_strs)}")


if __name__ == "__main__":
    main()

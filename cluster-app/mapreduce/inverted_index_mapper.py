#!/usr/bin/env python3
"""
Hadoop Streaming Mapper for Building Inverted Indices.

Input: Lines from the papers JSON data (one paper per line in format):
    doc_id\ttitle\tcitations\tabstract\turl

Output: word\tdoc_id\ttitle\tcitations\turl

Processes the abstract text, tokenizes, removes stop words,
and emits each word with the document metadata.
"""

import sys
import re
import os

# Load stop words
STOPWORDS = set()
stopwords_file = os.environ.get("STOPWORDS_FILE", "stopwords.txt")
try:
    with open(stopwords_file, "r") as f:
        STOPWORDS = set(line.strip().lower() for line in f if line.strip())
except FileNotFoundError:
    # Fallback: use a hardcoded minimal set
    STOPWORDS = {
        "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you",
        "your", "yours", "yourself", "yourselves", "he", "him", "his",
        "himself", "she", "her", "hers", "herself", "it", "its", "itself",
        "they", "them", "their", "theirs", "themselves", "what", "which",
        "who", "whom", "this", "that", "these", "those", "am", "is", "are",
        "was", "were", "be", "been", "being", "have", "has", "had", "having",
        "do", "does", "did", "doing", "a", "an", "the", "and", "but", "if",
        "or", "because", "as", "until", "while", "of", "at", "by", "for",
        "with", "about", "against", "between", "through", "during", "before",
        "after", "above", "below", "to", "from", "up", "down", "in", "out",
        "on", "off", "over", "under", "again", "further", "then", "once",
        "here", "there", "when", "where", "why", "how", "all", "both",
        "each", "few", "more", "most", "other", "some", "such", "no", "nor",
        "not", "only", "own", "same", "so", "than", "too", "very", "s", "t",
        "can", "will", "just", "don", "should", "now",
    }


def tokenize(text):
    """Tokenize text into words, removing punctuation and converting to lowercase."""
    # Remove special characters, keep only alphanumeric and spaces
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    # Split on whitespace and filter empty strings
    words = [w.strip() for w in text.split() if w.strip()]
    return words


def main():
    """Read input lines and emit word-document pairs."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            # Input format: doc_id\ttitle\tcitations\tabstract\turl
            parts = line.split("\t")
            if len(parts) < 5:
                continue

            doc_id = parts[0]
            title = parts[1]
            citations = parts[2]
            abstract = parts[3]
            url = parts[4]

            # Tokenize the abstract
            words = tokenize(abstract)

            # Count word frequencies in this document
            word_counts = {}
            for word in words:
                if word not in STOPWORDS and len(word) > 1:
                    word_counts[word] = word_counts.get(word, 0) + 1

            # Emit: word\tdoc_id\ttitle\tcitations\turl\tfrequency
            for word, count in word_counts.items():
                print(f"{word}\t{doc_id}\t{title}\t{citations}\t{url}\t{count}")

        except Exception as e:
            sys.stderr.write(f"Mapper error: {str(e)}\n")
            continue


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ScholarMiner Backend - Full Production Version
Refactored for Asynchronous Processing and GCS Persistence.
"""

import os
import sys
import json
import time
import logging
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from kafka_utils import create_consumer, create_producer, send_response
from scraper import scrape_and_collect

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Environment Configuration
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
HADOOP_STREAMING_JAR = "/usr/lib/hadoop/hadoop-streaming.jar"
MAPREDUCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapreduce")
STOPWORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stopwords.txt")
INDEX_CACHE_PATH = "/tmp/inverted_index_cache.json"

# Global State
inverted_index = {}
papers_data = []
indexed = False

# ThreadPool for non-blocking task execution
executor = ThreadPoolExecutor(max_workers=4)

# --- Persistence Layer ---

def persist_to_gcs():
    """Saves the current index state to GCS to survive worker restarts."""
    global inverted_index
    try:
        with open(INDEX_CACHE_PATH, "w") as f:
            json.dump(inverted_index, f)
        subprocess.run(f"gsutil cp {INDEX_CACHE_PATH} gs://{GCS_BUCKET}/cache/index.json", shell=True)
        logger.info("In-memory index successfully backed up to GCS.")
    except Exception as e:
        logger.error(f"Persistence error: {e}")

def reload_from_gcs():
    """Restores the index state from GCS on application startup."""
    global inverted_index, indexed
    try:
        res = subprocess.run(f"gsutil cp gs://{GCS_BUCKET}/cache/index.json {INDEX_CACHE_PATH}", shell=True)
        if res.returncode == 0:
            with open(INDEX_CACHE_PATH, "r") as f:
                inverted_index = json.load(f)
            indexed = True
            logger.info("System state recovered from GCS.")
    except Exception:
        logger.info("No persistent index found. System starting with clean slate.")

# --- Hadoop Processing ---

def run_hadoop_job(mapper, reducer, input_path, output_path):
    """Executes the Hadoop Streaming MapReduce task."""
    subprocess.run(f"hdfs dfs -rm -r -f {output_path}", shell=True, capture_output=True)
    
    # Locate streaming jar dynamically
    jar_find = subprocess.run("find /usr/lib -name 'hadoop-streaming*.jar' 2>/dev/null | head -1", 
                              shell=True, capture_output=True, text=True)
    jar_path = jar_find.stdout.strip() or HADOOP_STREAMING_JAR

    cmd = [
        "hadoop", "jar", jar_path,
        "-files", f"{mapper},{reducer},{STOPWORDS_FILE}",
        "-mapper", f"python3 {os.path.basename(mapper)}",
        "-reducer", f"python3 {os.path.basename(reducer)}",
        "-input", input_path,
        "-output", output_path,
        "-cmdenv", f"STOPWORDS_FILE={os.path.basename(STOPWORDS_FILE)}",
    ]
    logger.info(f"Launching Hadoop Job: {' '.join(cmd)}")
    subprocess.run(cmd, capture_output=True, text=True, timeout=600)

def parse_inverted_index(output_text):
    """Converts Hadoop TSV output into the global Python dictionary."""
    index = {}
    for line in output_text.strip().split("\n"):
        if not line.strip(): continue
        parts = line.split("\t")
        if len(parts) < 2: continue
        word, postings_str = parts[0], parts[1]
        postings = []
        for p in postings_str.split("|"):
            fields = p.split(":")
            if len(fields) >= 5:
                postings.append({
                    "doc_id": fields[0], 
                    "title": ":".join(fields[1:-3]), # Handles titles with colons
                    "citations": fields[-3], 
                    "url": fields[-2], 
                    "frequency": int(fields[-1])
                })
        index[word] = postings
    return index

# --- Task Handlers ---

def process_index_task(message, producer):
    """Handles the heavy scraping and Hadoop indexing in the background."""
    global inverted_index, indexed, papers_data
    scholar_url = message.get("scholar_url", "")
    req_id = message.get("request_id", "")
    
    try:
        # Step 1: Scrape
        papers_data = scrape_and_collect(scholar_url)
        
        # Step 2: Prepare Input
        input_file = "/tmp/papers_input.tsv"
        with open(input_file, "w", encoding="utf-8") as f:
            for p in papers_data:
                # Clean TSV breaks
                title = p.get("title", "").replace("\t", " ").replace("\n", " ")
                abstract = p.get("abstract", "").replace("\t", " ").replace("\n", " ")
                f.write(f"{p.get('ieee_id')}\t{title}\t{p.get('citations')}\t{abstract}\t{p.get('url')}\n")
        
        # Step 3: Run Hadoop
        subprocess.run(f"hdfs dfs -put -f {input_file} /ieee-search/input/papers.tsv", shell=True)
        run_hadoop_job(
            os.path.join(MAPREDUCE_DIR, "inverted_index_mapper.py"),
            os.path.join(MAPREDUCE_DIR, "inverted_index_reducer.py"),
            "/ieee-search/input/papers.tsv", "/ieee-search/output/inverted_index"
        )
        
        # Step 4: Finalize Index
        res = subprocess.run("hdfs dfs -cat /ieee-search/output/inverted_index/part-*", shell=True, capture_output=True, text=True)
        inverted_index = parse_inverted_index(res.stdout)
        persist_to_gcs()
        indexed = True

        send_response(producer, {"request_id": req_id, "status": "success", "data": {"num_terms": len(inverted_index)}})
    except Exception as e:
        logger.error(f"Async indexing failed: {e}")
        send_response(producer, {"request_id": req_id, "status": "error", "error": str(e)})

def main():
    """Core Kafka consumer loop using asynchronous task distribution."""
    logger.info("ScholarMiner Worker Node is online.")
    producer = create_producer()
    consumer = create_consumer()
    reload_from_gcs()

    for msg in consumer:
        req = msg.value
        action = req.get("action")
        req_id = req.get("request_id")

        if action == "index":
            # Delegate heavy task to threadpool
            executor.submit(process_index_task, req, producer)
            
        elif action == "search":
            # Search logic remains in-memory
            if not indexed:
                send_response(producer, {"request_id": req_id, "status": "error", "error": "System not indexed."})
                continue
            term = req.get("term", "").lower().strip()
            results = inverted_index.get(term, [])
            send_response(producer, {"request_id": req_id, "status": "success", "results": results})

        elif action == "topn":
            # Optimized in-memory aggregation
            n = req.get("n", 10)
            counts = {t: sum(p['frequency'] for p in ps) for t, ps in inverted_index.items()}
            top = [{"term": k, "frequency": v} for k, v in Counter(counts).most_common(n)]
            send_response(producer, {"request_id": req_id, "status": "success", "results": top})

if __name__ == "__main__":
    main()
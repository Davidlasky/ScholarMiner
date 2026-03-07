#!/usr/bin/env python3
"""
Cluster-Based Backend Application for IEEE Search Engine.

This application runs on the Dataproc cluster master node.
It consumes requests from the lightweight app via Kafka,
performs heavy processing (scraping, Hadoop MapReduce jobs),
and sends results back via Kafka.
"""

import os
import sys
import json
import time
import logging
import subprocess
import tempfile
from kafka_utils import create_consumer, create_producer, send_response
from scraper import scrape_and_collect

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Configuration
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
HADOOP_STREAMING_JAR = os.environ.get(
    "HADOOP_STREAMING_JAR",
    "/usr/lib/hadoop/hadoop-streaming.jar"
)
MAPREDUCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapreduce")
STOPWORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stopwords.txt")

# State
inverted_index = {}  # word -> list of postings
all_term_frequencies = {}  # word -> total frequency
papers_data = []  # list of paper dicts
indexed = False


def prepare_input_data(papers, output_path):
    """
    Prepare input data for Hadoop MapReduce.
    Writes papers data in TSV format: doc_id\\ttitle\\tcitations\\tabstract\\turl
    """
    with open(output_path, "w", encoding="utf-8") as f:
        for paper in papers:
            doc_id = paper.get("ieee_id", "unknown")
            title = paper.get("title", "").replace("\t", " ").replace("\n", " ")
            citations = str(paper.get("citations", 0))
            abstract = paper.get("abstract", "").replace("\t", " ").replace("\n", " ")
            url = paper.get("url", "").replace("\t", " ")
            f.write(f"{doc_id}\t{title}\t{citations}\t{abstract}\t{url}\n")

    logger.info("Prepared input data with %d papers at %s", len(papers), output_path)


def upload_to_gcs(local_path, gcs_path):
    """Upload a local file to GCS."""
    cmd = f"gsutil cp {local_path} gs://{GCS_BUCKET}/{gcs_path}"
    logger.info("Uploading to GCS: %s", cmd)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("GCS upload failed: %s", result.stderr)
        raise RuntimeError(f"GCS upload failed: {result.stderr}")
    return f"gs://{GCS_BUCKET}/{gcs_path}"


def download_from_gcs(gcs_path, local_path):
    """Download a file from GCS."""
    cmd = f"gsutil cp gs://{GCS_BUCKET}/{gcs_path} {local_path}"
    logger.info("Downloading from GCS: %s", cmd)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("GCS download failed: %s", result.stderr)
        raise RuntimeError(f"GCS download failed: {result.stderr}")


def run_hadoop_job(mapper, reducer, input_path, output_path):
    """
    Run a Hadoop Streaming MapReduce job.

    Args:
        mapper: Path to the mapper script (on GCS).
        reducer: Path to the reducer script (on GCS).
        input_path: HDFS/GCS input path.
        output_path: HDFS/GCS output path.
    """
    # Find the Hadoop streaming jar
    streaming_jar = HADOOP_STREAMING_JAR
    if not os.path.exists(streaming_jar):
        # Try to find it
        result = subprocess.run(
            "find /usr/lib -name 'hadoop-streaming*.jar' 2>/dev/null | head -1",
            shell=True, capture_output=True, text=True
        )
        if result.stdout.strip():
            streaming_jar = result.stdout.strip()

    # Remove output directory if it exists
    subprocess.run(
        f"hdfs dfs -rm -r -f {output_path}",
        shell=True, capture_output=True, text=True
    )

    cmd = [
        "hadoop", "jar", streaming_jar,
        "-files", f"{mapper},{reducer},{STOPWORDS_FILE}",
        "-mapper", f"python3 {os.path.basename(mapper)}",
        "-reducer", f"python3 {os.path.basename(reducer)}",
        "-input", input_path,
        "-output", output_path,
        "-cmdenv", f"STOPWORDS_FILE={os.path.basename(STOPWORDS_FILE)}",
    ]

    logger.info("Running Hadoop job: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        logger.error("Hadoop job failed: %s", result.stderr)
        raise RuntimeError(f"Hadoop job failed: {result.stderr}")

    logger.info("Hadoop job completed successfully")
    return output_path


def read_hadoop_output(output_path):
    """Read the output of a Hadoop job from HDFS."""
    cmd = f"hdfs dfs -cat {output_path}/part-*"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.error("Failed to read Hadoop output: %s", result.stderr)
        raise RuntimeError(f"Failed to read output: {result.stderr}")
    return result.stdout


def handle_index_request(message):
    """Handle an indexing request from the lightweight app."""
    global inverted_index, all_term_frequencies, papers_data, indexed

    scholar_url = message.get("scholar_url", "")
    request_id = message.get("request_id", "")

    logger.info("Processing index request for: %s", scholar_url)

    try:
        # Step 1: Scrape Google Scholar and fetch IEEE abstracts
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            papers_output = f.name

        papers_data = scrape_and_collect(scholar_url, papers_output)
        logger.info("Scraped %d papers", len(papers_data))

        # Step 2: Prepare input data for Hadoop
        input_file = "/tmp/papers_input.tsv"
        prepare_input_data(papers_data, input_file)

        # Step 3: Upload input to HDFS
        subprocess.run("hdfs dfs -mkdir -p /ieee-search/input", shell=True, capture_output=True)
        subprocess.run(
            f"hdfs dfs -put -f {input_file} /ieee-search/input/papers.tsv",
            shell=True, capture_output=True, text=True
        )

        # Step 4: Run Inverted Index MapReduce job
        mapper_path = os.path.join(MAPREDUCE_DIR, "inverted_index_mapper.py")
        reducer_path = os.path.join(MAPREDUCE_DIR, "inverted_index_reducer.py")

        run_hadoop_job(
            mapper=mapper_path,
            reducer=reducer_path,
            input_path="/ieee-search/input/papers.tsv",
            output_path="/ieee-search/output/inverted_index"
        )

        # Step 5: Read and parse inverted index results
        index_output = read_hadoop_output("/ieee-search/output/inverted_index")
        inverted_index = parse_inverted_index(index_output)
        logger.info("Built inverted index with %d terms", len(inverted_index))

        indexed = True

        return {
            "request_id": request_id,
            "status": "success",
            "message": "Engine loaded and inverted indices constructed successfully!",
            "data": {
                "num_papers": len(papers_data),
                "num_terms": len(inverted_index),
            }
        }

    except Exception as e:
        logger.error("Index request failed: %s", str(e))
        return {
            "request_id": request_id,
            "status": "error",
            "error": str(e),
        }


def handle_search_request(message):
    """Handle a search term request."""
    global inverted_index

    term = message.get("term", "").lower().strip()
    request_id = message.get("request_id", "")

    logger.info("Processing search request for term: %s", term)

    start_time = time.time()

    if not indexed:
        return {
            "request_id": request_id,
            "status": "error",
            "error": "Index not built yet. Please load a URL first.",
        }

    results = []
    if term in inverted_index:
        for posting in inverted_index[term]:
            results.append({
                "doc_id": posting["doc_id"],
                "doc_url": posting["url"],
                "citations": posting["citations"],
                "doc_name": posting["title"],
                "frequency": posting["frequency"],
            })

    execution_time = round((time.time() - start_time) * 1000, 2)

    return {
        "request_id": request_id,
        "status": "success",
        "results": results,
        "execution_time_ms": execution_time,
    }


def handle_topn_request(message):
    """Handle a Top-N frequent terms request."""
    global inverted_index

    n = message.get("n", 10)
    request_id = message.get("request_id", "")

    logger.info("Processing Top-N request for N=%d", n)

    if not indexed:
        return {
            "request_id": request_id,
            "status": "error",
            "error": "Index not built yet. Please load a URL first.",
        }

    start_time = time.time()

    try:
        # Run Top-N MapReduce job on the inverted index output
        mapper_path = os.path.join(MAPREDUCE_DIR, "topn_mapper.py")
        reducer_path = os.path.join(MAPREDUCE_DIR, "topn_reducer.py")

        run_hadoop_job(
            mapper=mapper_path,
            reducer=reducer_path,
            input_path="/ieee-search/output/inverted_index",
            output_path="/ieee-search/output/topn"
        )

        # Read Top-N results
        topn_output = read_hadoop_output("/ieee-search/output/topn")
        all_terms = parse_topn_output(topn_output)

        # Sort by frequency and take top N
        all_terms.sort(key=lambda x: x["frequency"], reverse=True)
        top_results = all_terms[:n]

        execution_time = round((time.time() - start_time) * 1000, 2)

        return {
            "request_id": request_id,
            "status": "success",
            "results": [{"term": t["term"], "frequency": t["frequency"]} for t in top_results],
            "execution_time_ms": execution_time,
        }

    except Exception as e:
        logger.error("Top-N request failed: %s", str(e))
        return {
            "request_id": request_id,
            "status": "error",
            "error": str(e),
        }


def parse_inverted_index(output_text):
    """Parse the inverted index output from Hadoop."""
    index = {}
    for line in output_text.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue

        word = parts[0]
        postings_str = parts[1]
        postings = []

        for posting_str in postings_str.split("|"):
            fields = posting_str.split(":")
            if len(fields) >= 5:
                postings.append({
                    "doc_id": fields[0],
                    "title": ":".join(fields[1:-3]),  # title may contain colons
                    "citations": fields[-3],
                    "url": fields[-2],
                    "frequency": int(fields[-1]),
                })

        index[word] = postings

    return index


def parse_topn_output(output_text):
    """Parse the Top-N output from Hadoop."""
    terms = []
    for line in output_text.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                terms.append({
                    "term": parts[0],
                    "frequency": int(parts[1]),
                })
            except ValueError:
                continue
    return terms


def main():
    """Main loop: consume Kafka messages and process requests."""
    logger.info("Starting cluster backend application...")
    logger.info("Kafka broker: %s", os.environ.get("KAFKA_BROKER", "localhost:9092"))
    logger.info("GCS bucket: %s", GCS_BUCKET)

    # Wait for Kafka to be ready
    max_retries = 30
    consumer = None
    producer = None

    for i in range(max_retries):
        try:
            consumer = create_consumer()
            producer = create_producer()
            logger.info("Connected to Kafka successfully")
            break
        except Exception as e:
            logger.warning("Waiting for Kafka... attempt %d/%d: %s", i + 1, max_retries, str(e))
            time.sleep(10)

    if not consumer or not producer:
        logger.error("Failed to connect to Kafka after %d retries", max_retries)
        sys.exit(1)

    logger.info("Backend is ready and listening for requests...")

    try:
        for msg in consumer:
            message = msg.value
            action = message.get("action", "")

            logger.info("Received request: action=%s, request_id=%s", action, message.get("request_id"))

            if action == "index":
                response = handle_index_request(message)
            elif action == "search":
                response = handle_search_request(message)
            elif action == "topn":
                response = handle_topn_request(message)
            else:
                response = {
                    "request_id": message.get("request_id", ""),
                    "status": "error",
                    "error": f"Unknown action: {action}",
                }

            send_response(producer, response)

    except KeyboardInterrupt:
        logger.info("Shutting down backend...")
    finally:
        if consumer:
            consumer.close()
        if producer:
            producer.close()


if __name__ == "__main__":
    main()

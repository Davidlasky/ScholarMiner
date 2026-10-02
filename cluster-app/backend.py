#!/usr/bin/env python3

import json
import logging
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2
import redis
from google.api_core.client_options import ClientOptions
from google.cloud import dataproc_v1, pubsub_v1, secretmanager, storage
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from psycopg2.extras import execute_batch

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
PUBSUB_SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "ieee_search")
DB_USER = os.environ.get("DB_USER", "app_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PASSWORD_SECRET = os.environ.get("DB_PASSWORD_SECRET", "")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
DATAPROC_CLUSTER = os.environ.get("DATAPROC_CLUSTER", "")
DATAPROC_REGION = os.environ.get("DATAPROC_REGION", "")
SERPAPI_SECRET = os.environ.get("SERPAPI_SECRET", "")
WORKER_HEALTH_PORT = int(os.environ.get("WORKER_HEALTH_PORT", "8080"))
MAX_PARALLEL_TASKS = int(os.environ.get("MAX_PARALLEL_TASKS", "1"))
INDEX_LOCK_ID = int(os.environ.get("INDEX_LOCK_ID", "14848"))
DATAPROC_JOB_TIMEOUT_SECONDS = int(
    os.environ.get("DATAPROC_JOB_TIMEOUT_SECONDS", "1800")
)

_secret_client = None
_storage_client = None
_subscriber_client = None
_redis_client = None
_schema_ready = False
_secret_cache = {}
_subscriber_started = False

WORKER_TASKS_TOTAL = Counter(
    "scholarminer_worker_tasks_total",
    "Total indexing tasks processed by the worker.",
    ["status"],
)
WORKER_TASK_DURATION_SECONDS = Histogram(
    "scholarminer_worker_task_duration_seconds",
    "End-to-end processing time for indexing tasks.",
)
WORKER_MESSAGES_TOTAL = Counter(
    "scholarminer_worker_pubsub_messages_total",
    "Pub/Sub messages processed by the worker.",
    ["status"],
)
WORKER_SUBSCRIBER_CONNECTED = Gauge(
    "scholarminer_worker_subscriber_connected",
    "Whether the worker subscriber is currently connected to Pub/Sub.",
)
LAST_INDEX_TERM_COUNT = Gauge(
    "scholarminer_worker_last_index_term_count",
    "Number of terms produced by the most recent successful index build.",
)
LAST_INDEX_PAPER_COUNT = Gauge(
    "scholarminer_worker_last_index_paper_count",
    "Number of papers processed by the most recent successful index build.",
)


def get_secret_manager_client():
    """Create the Secret Manager client lazily for ADC-based auth."""
    global _secret_client
    if _secret_client is None:
        _secret_client = secretmanager.SecretManagerServiceClient()
    return _secret_client


def resolve_secret_value(secret_name):
    """Resolve a secret value from Secret Manager, caching repeated lookups."""
    if not secret_name:
        return None

    if secret_name in _secret_cache:
        return _secret_cache[secret_name]

    if secret_name.startswith("projects/"):
        secret_version = (
            secret_name
            if "/versions/" in secret_name
            else f"{secret_name}/versions/latest"
        )
    else:
        if not GCP_PROJECT:
            raise RuntimeError("GCP_PROJECT must be set when using secret IDs.")
        secret_version = f"projects/{GCP_PROJECT}/secrets/{secret_name}/versions/latest"

    response = get_secret_manager_client().access_secret_version(
        request={"name": secret_version}
    )
    value = response.payload.data.decode("utf-8")
    _secret_cache[secret_name] = value
    return value


if not os.environ.get("SERPAPI_KEY") and SERPAPI_SECRET:
    os.environ["SERPAPI_KEY"] = resolve_secret_value(SERPAPI_SECRET)

from scraper import scrape_and_collect  # noqa: E402


def get_db_connection():
    """Open a PostgreSQL connection using the configured credentials."""
    password = DB_PASSWORD or resolve_secret_value(DB_PASSWORD_SECRET)
    if not password:
        raise RuntimeError("Database password is not configured.")

    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=password,
        connect_timeout=5,
    )


def ensure_schema():
    """Create the worker tables if they do not exist yet."""
    global _schema_ready
    if _schema_ready:
        return

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inverted_index (
                        term TEXT PRIMARY KEY,
                        doc_ids TEXT NOT NULL,
                        count INTEGER NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS indexed_sources (
                        scholar_url TEXT PRIMARY KEY,
                        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS index_tasks (
                        task_id TEXT PRIMARY KEY,
                        scholar_url TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT,
                        result_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        _schema_ready = True
    finally:
        conn.close()


def get_storage_client():
    """Create the GCS client lazily."""
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=GCP_PROJECT or None)
    return _storage_client


def get_bucket():
    """Return the configured GCS bucket handle."""
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET must be configured.")
    return get_storage_client().bucket(GCS_BUCKET)


def get_subscriber_client():
    """Create the Pub/Sub subscriber lazily."""
    global _subscriber_client
    if _subscriber_client is None:
        _subscriber_client = pubsub_v1.SubscriberClient()
    return _subscriber_client


def get_subscription_path():
    """Resolve the configured Pub/Sub subscription path."""
    if not PUBSUB_SUBSCRIPTION:
        raise RuntimeError("PUBSUB_SUBSCRIPTION must be configured.")
    if PUBSUB_SUBSCRIPTION.startswith("projects/"):
        return PUBSUB_SUBSCRIPTION
    if not GCP_PROJECT:
        raise RuntimeError("GCP_PROJECT must be set when using subscription IDs.")
    return get_subscriber_client().subscription_path(GCP_PROJECT, PUBSUB_SUBSCRIPTION)


def get_redis_client():
    """Create a Redis client lazily and verify connectivity."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    try:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        _redis_client = client
    except Exception as exc:
        logger.warning("Redis is unavailable: %s", exc)
        _redis_client = None
    return _redis_client


def get_dataproc_job_client():
    """Create a Dataproc job client scoped to the cluster region."""
    if not DATAPROC_REGION:
        raise RuntimeError("DATAPROC_REGION must be configured.")
    return dataproc_v1.JobControllerClient(
        client_options=ClientOptions(
            api_endpoint=f"{DATAPROC_REGION}-dataproc.googleapis.com:443"
        )
    )


def update_task_status(task_id, scholar_url, status, message=None, result=None):
    """Create or update a task status row."""
    ensure_schema()
    result_json = json.dumps(result) if result is not None else None

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO index_tasks (task_id, scholar_url, status, message, result_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (task_id)
                    DO UPDATE SET
                        scholar_url = EXCLUDED.scholar_url,
                        status = EXCLUDED.status,
                        message = EXCLUDED.message,
                        result_json = EXCLUDED.result_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (task_id, scholar_url, status, message, result_json),
                )
    finally:
        conn.close()


def count_terms():
    """Return the number of indexed terms currently stored."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM inverted_index")
            return cur.fetchone()[0]
    finally:
        conn.close()


def source_already_indexed(scholar_url):
    """Check if this Scholar URL has already been processed."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM indexed_sources WHERE scholar_url = %s", (scholar_url,)
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_source_indexed(scholar_url):
    """Persist the fact that a Scholar URL has already been indexed."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO indexed_sources (scholar_url)
                    VALUES (%s)
                    ON CONFLICT (scholar_url) DO NOTHING
                    """,
                    (scholar_url,),
                )
    finally:
        conn.close()

    client = get_redis_client()
    if client:
        try:
            client.sadd("scraped_urls", scholar_url)
        except Exception as exc:
            logger.warning("Unable to update Redis source cache: %s", exc)


def replace_inverted_index(index_data):
    """Replace the persisted inverted index with a fresh full snapshot."""
    ensure_schema()
    rows = [
        (term, json.dumps(postings), len(postings))
        for term, postings in index_data.items()
    ]

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE inverted_index")
                if rows:
                    execute_batch(
                        cur,
                        """
                        INSERT INTO inverted_index (term, doc_ids, count, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        """,
                        rows,
                        page_size=250,
                    )
    finally:
        conn.close()


def _delete_redis_search_cache(client):
    """Delete stale per-term cache keys before re-populating Redis."""
    batch = []
    for key in client.scan_iter(match="search:*"):
        batch.append(key)
        if len(batch) >= 250:
            client.delete(*batch)
            batch = []
    if batch:
        client.delete(*batch)


def persist_to_redis(index_data):
    """Save the full inverted index and ranked terms to Redis."""
    client = get_redis_client()
    if not client:
        return

    try:
        _delete_redis_search_cache(client)
        pipe = client.pipeline()
        pipe.delete("term_freq")
        for term, postings in index_data.items():
            pipe.set(f"search:{term}", json.dumps(postings))
            total_freq = sum(item["frequency"] for item in postings)
            pipe.zadd("term_freq", {term: total_freq})
        pipe.execute()
        logger.info("Successfully persisted %s terms to Redis.", len(index_data))
    except Exception as exc:
        logger.error("Redis persistence error: %s", exc)


def persist_to_gcs(index_data):
    """Store a JSON snapshot of the latest index in GCS for cache recovery."""
    blob = get_bucket().blob("cache/index.json")
    blob.upload_from_string(json.dumps(index_data), content_type="application/json")
    logger.info("Stored an index snapshot in GCS.")


def restore_redis_from_gcs():
    """Warm Redis from the latest GCS snapshot if Redis is empty."""
    client = get_redis_client()
    if not client:
        return

    try:
        if client.zcard("term_freq") > 0:
            return
    except Exception:
        return

    blob = get_bucket().blob("cache/index.json")
    if not blob.exists():
        logger.info("No GCS snapshot found for Redis warmup.")
        return

    try:
        snapshot = json.loads(blob.download_as_text())
        persist_to_redis(snapshot)
        logger.info("Redis cache restored from GCS snapshot.")
    except Exception as exc:
        logger.warning("Failed to restore Redis from GCS: %s", exc)


def acquire_index_lock():
    """Acquire a PostgreSQL advisory lock to serialize index rebuilds."""
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(%s)", (INDEX_LOCK_ID,))
    cur.close()
    logger.info("Acquired global index rebuild lock.")
    return conn


def create_input_file(task_id, papers_data):
    """Write scraped papers into a TSV file for the Dataproc job."""
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=f"_{task_id}.tsv",
        delete=False,
    )
    with temp_file as handle:
        for paper in papers_data:
            title = paper.get("title", "").replace("\t", " ").replace("\n", " ")
            abstract = paper.get("abstract", "").replace("\t", " ").replace("\n", " ")
            handle.write(
                f"{paper.get('ieee_id')}\t{title}\t{paper.get('citations')}\t{abstract}\t{paper.get('url')}\n"
            )
    return temp_file.name


def upload_input_file(local_path, task_id):
    """Upload the TSV input shard for cumulative indexing."""
    blob = get_bucket().blob(f"input/papers_{task_id}.tsv")
    blob.upload_from_filename(local_path)
    return blob.name


def submit_hadoop_job(task_id):
    """Submit a Hadoop streaming job to Dataproc using the bucket-backed corpus."""
    if not GCP_PROJECT or not DATAPROC_CLUSTER:
        raise RuntimeError("GCP_PROJECT and DATAPROC_CLUSTER must be configured.")

    output_prefix = f"output/inverted_index/{task_id}"
    input_glob = f"gs://{GCS_BUCKET}/input/papers_*.tsv"
    output_uri = f"gs://{GCS_BUCKET}/{output_prefix}"
    stopwords_uri = f"gs://{GCS_BUCKET}/data/stopwords.txt"
    mapper_uri = f"gs://{GCS_BUCKET}/mapreduce/inverted_index_mapper.py"
    reducer_uri = f"gs://{GCS_BUCKET}/mapreduce/inverted_index_reducer.py"

    job = {
        "placement": {"cluster_name": DATAPROC_CLUSTER},
        "hadoop_job": {
            "main_jar_file_uri": "file:///usr/lib/hadoop/hadoop-streaming.jar",
            "args": [
                "-files",
                f"{mapper_uri},{reducer_uri},{stopwords_uri}",
                "-mapper",
                "python3 inverted_index_mapper.py",
                "-reducer",
                "python3 inverted_index_reducer.py",
                "-input",
                input_glob,
                "-output",
                output_uri,
                "-cmdenv",
                "STOPWORDS_FILE=stopwords.txt",
            ],
        },
    }

    logger.info("Submitting Dataproc streaming job for task %s", task_id)
    operation = get_dataproc_job_client().submit_job_as_operation(
        request={
            "project_id": GCP_PROJECT,
            "region": DATAPROC_REGION,
            "job": job,
        }
    )
    submitted_job = operation.result(timeout=DATAPROC_JOB_TIMEOUT_SECONDS)
    logger.info(
        "Dataproc job %s finished for task %s", submitted_job.reference.job_id, task_id
    )
    return output_prefix


def read_output_from_gcs(output_prefix):
    """Read the generated MapReduce output from GCS and verify it exists."""
    blobs = list(
        get_storage_client().list_blobs(GCS_BUCKET, prefix=f"{output_prefix}/part-")
    )
    if not blobs:
        raise RuntimeError("Dataproc completed without producing any output shards.")

    content = []
    for blob in blobs:
        payload = blob.download_as_text()
        if payload.strip():
            content.append(payload)

    output_text = "\n".join(content).strip()
    if not output_text:
        raise RuntimeError("Dataproc completed but the output shards were empty.")
    return output_text


def parse_inverted_index(output_text):
    """Convert Hadoop TSV output into a Python dictionary."""
    index = {}
    for line in output_text.strip().split("\n"):
        if not line.strip():
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            continue

        word, postings_str = parts[0], parts[1]
        postings = []
        for posting in postings_str.split("|"):
            try:
                remainder, freq_str = posting.rsplit(":", 1)

                http_idx = remainder.rfind(":http")
                if http_idx != -1:
                    url = remainder[http_idx + 1 :]
                    remainder = remainder[:http_idx]
                else:
                    remainder, url = remainder.rsplit(":", 1)

                remainder, citations_str = remainder.rsplit(":", 1)
                doc_id, title = remainder.split(":", 1)
                postings.append(
                    {
                        "doc_id": doc_id,
                        "doc_name": title,
                        "citations": citations_str,
                        "doc_url": url,
                        "frequency": int(freq_str),
                    }
                )
            except Exception:
                continue

        index[word] = postings
    return index


def process_index_task(task_id, scholar_url):
    """Handle a single asynchronous indexing task."""
    lock_conn = None
    task_timer_start = time.perf_counter()
    try:
        lock_conn = acquire_index_lock()
        if source_already_indexed(scholar_url):
            restore_redis_from_gcs()
            term_count = count_terms()
            update_task_status(
                task_id,
                scholar_url,
                "COMPLETE",
                "The submitted Scholar URL was already indexed.",
                {"cached": True, "num_terms": term_count},
            )
            WORKER_TASKS_TOTAL.labels("cached").inc()
            LAST_INDEX_TERM_COUNT.set(term_count)
            return

        update_task_status(
            task_id, scholar_url, "RUNNING", "Scraping papers from Google Scholar."
        )
        papers_data = scrape_and_collect(scholar_url)
        if not papers_data:
            update_task_status(
                task_id,
                scholar_url,
                "COMPLETE",
                "No papers were found for the submitted URL.",
                {"cached": False, "num_terms": count_terms(), "papers_indexed": 0},
            )
            WORKER_TASKS_TOTAL.labels("empty").inc()
            LAST_INDEX_PAPER_COUNT.set(0)
            return

        update_task_status(
            task_id,
            scholar_url,
            "RUNNING",
            "Uploading input shard and submitting the Dataproc job.",
        )
        input_file = create_input_file(task_id, papers_data)
        try:
            upload_input_file(input_file, task_id)
        finally:
            try:
                os.remove(input_file)
            except OSError:
                pass

        output_prefix = submit_hadoop_job(task_id)
        output_text = read_output_from_gcs(output_prefix)
        index_data = parse_inverted_index(output_text)
        if not index_data:
            raise RuntimeError(
                "The Dataproc job completed, but the parsed inverted index was empty."
            )

        update_task_status(
            task_id, scholar_url, "RUNNING", "Persisting the latest index snapshot."
        )
        replace_inverted_index(index_data)
        persist_to_redis(index_data)
        persist_to_gcs(index_data)
        mark_source_indexed(scholar_url)
        WORKER_TASKS_TOTAL.labels("success").inc()
        LAST_INDEX_TERM_COUNT.set(len(index_data))
        LAST_INDEX_PAPER_COUNT.set(len(papers_data))

        update_task_status(
            task_id,
            scholar_url,
            "COMPLETE",
            "Indexing completed successfully.",
            {
                "cached": False,
                "num_terms": len(index_data),
                "papers_indexed": len(papers_data),
            },
        )
    except Exception as exc:
        logger.exception("Indexing task %s failed.", task_id)
        WORKER_TASKS_TOTAL.labels("failed").inc()
        update_task_status(task_id, scholar_url, "FAILED", str(exc))
        raise
    finally:
        WORKER_TASK_DURATION_SECONDS.observe(time.perf_counter() - task_timer_start)
        if lock_conn is not None:
            lock_conn.close()


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP health endpoint for managed instance group autohealing."""

    def do_GET(self):
        if self.path == "/health":
            self._write_response(200, {"status": "ok"})
            return

        if self.path == "/metrics":
            metrics_payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(metrics_payload)))
            self.end_headers()
            self.wfile.write(metrics_payload)
            return

        if self.path == "/ready":
            status = {
                "database": False,
                "redis": False,
                "subscriber": _subscriber_started,
                "dataproc": bool(DATAPROC_CLUSTER and DATAPROC_REGION),
            }
            code = 200

            try:
                ensure_schema()
                status["database"] = True
            except Exception as exc:
                logger.warning("Worker readiness database check failed: %s", exc)
                code = 503

            try:
                client = get_redis_client()
                status["redis"] = bool(client and client.ping())
            except Exception:
                status["redis"] = False

            status["status"] = (
                "ready" if code == 200 and status["subscriber"] else "degraded"
            )
            if not status["subscriber"]:
                code = 503
            self._write_response(code, status)
            return

        self._write_response(404, {"error": "Not found"})

    def log_message(self, fmt, *args):
        return

    def _write_response(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server():
    """Serve /health and /ready for MIG health checks."""
    server = ThreadingHTTPServer(("0.0.0.0", WORKER_HEALTH_PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Worker health server listening on port %s", WORKER_HEALTH_PORT)


def handle_message(message):
    """Process a Pub/Sub message and persist task status updates."""
    payload = {}
    try:
        payload = json.loads(message.data.decode("utf-8"))
        task_id = payload.get("task_id")
        action = payload.get("action")
        scholar_url = payload.get("scholar_url", "")

        if action != "index" or not task_id:
            logger.warning("Ignoring unsupported message payload: %s", payload)
            WORKER_MESSAGES_TOTAL.labels("ignored").inc()
            message.ack()
            return

        process_index_task(task_id, scholar_url)
        WORKER_MESSAGES_TOTAL.labels("success").inc()
        message.ack()
    except Exception as exc:
        logger.error("Worker failed to process payload %s: %s", payload, exc)
        WORKER_MESSAGES_TOTAL.labels("failed").inc()
        message.ack()


def main():
    """Run the standalone worker service."""
    global _subscriber_started

    logger.info("ScholarMiner worker service is starting.")
    ensure_schema()
    restore_redis_from_gcs()
    start_health_server()

    subscriber = get_subscriber_client()
    flow_control = pubsub_v1.types.FlowControl(max_messages=MAX_PARALLEL_TASKS)
    streaming_pull_future = subscriber.subscribe(
        get_subscription_path(),
        callback=handle_message,
        flow_control=flow_control,
    )
    _subscriber_started = True
    WORKER_SUBSCRIBER_CONNECTED.set(1)
    logger.info("Listening for indexing tasks on %s", get_subscription_path())

    try:
        streaming_pull_future.result()
    except KeyboardInterrupt:
        streaming_pull_future.cancel()
        logger.info("Worker shut down cleanly.")
    finally:
        WORKER_SUBSCRIBER_CONNECTED.set(0)


if __name__ == "__main__":
    main()

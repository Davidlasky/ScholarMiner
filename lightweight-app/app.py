"""
Flask application for ScholarMiner.

The web tier now treats indexing as an asynchronous task:
- submit an indexing task to Pub/Sub
- track task state in PostgreSQL
- serve read traffic directly from Redis / PostgreSQL
"""

import json
import logging
import os
import time
import uuid

import psycopg2
import redis
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google.cloud import pubsub_v1, secretmanager
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "ieee_search")
DB_USER = os.environ.get("DB_USER", "app_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PASSWORD_SECRET = os.environ.get("DB_PASSWORD_SECRET", "")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
TASK_STATUS_POLL_INTERVAL = int(os.environ.get("TASK_STATUS_POLL_INTERVAL", "5"))

_secret_client = None
_publisher_client = None
_redis_client = None
_schema_ready = False
_secret_cache = {}

HTTP_REQUESTS_TOTAL = Counter(
    "scholarminer_web_http_requests_total",
    "Total HTTP requests handled by the ScholarMiner web service.",
    ["method", "endpoint", "status_code"],
)
HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    "scholarminer_web_http_request_latency_seconds",
    "Latency of HTTP requests handled by the ScholarMiner web service.",
    ["endpoint"],
)
INDEX_SUBMISSIONS_TOTAL = Counter(
    "scholarminer_web_index_submissions_total",
    "Total indexing task submissions grouped by result.",
    ["result"],
)
SEARCH_QUERIES_TOTAL = Counter(
    "scholarminer_web_search_queries_total",
    "Total search queries grouped by backend source.",
    ["source"],
)
TOPN_QUERIES_TOTAL = Counter(
    "scholarminer_web_topn_queries_total",
    "Total Top-N queries grouped by backend source.",
    ["source"],
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


def get_flask_secret_key():
    """Resolve the Flask secret key from env or Secret Manager."""
    if os.environ.get("FLASK_SECRET_KEY"):
        return os.environ["FLASK_SECRET_KEY"]

    secret_name = os.environ.get("FLASK_SECRET_SECRET", "")
    if secret_name:
        return resolve_secret_value(secret_name)

    logger.warning("Falling back to a local Flask secret key.")
    return "scholarminer-local-secret"


app = Flask(__name__)
app.secret_key = get_flask_secret_key()


@app.before_request
def start_request_timer():
    """Record the request start time for latency metrics."""
    g.request_started_at = time.perf_counter()


@app.after_request
def record_request_metrics(response):
    """Publish per-request counters and latency histograms."""
    endpoint = request.endpoint or request.path
    HTTP_REQUESTS_TOTAL.labels(
        request.method,
        endpoint,
        str(response.status_code),
    ).inc()

    started_at = getattr(g, "request_started_at", None)
    if started_at is not None:
        HTTP_REQUEST_LATENCY_SECONDS.labels(endpoint).observe(
            time.perf_counter() - started_at
        )

    return response


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
    """Create the application tables if they do not exist yet."""
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


def get_pubsub_publisher():
    """Create the Pub/Sub publisher lazily."""
    global _publisher_client
    if _publisher_client is None:
        _publisher_client = pubsub_v1.PublisherClient()
    return _publisher_client


def get_pubsub_topic_path():
    """Resolve the fully-qualified Pub/Sub topic path."""
    if not GCP_PROJECT or not PUBSUB_TOPIC:
        raise RuntimeError("GCP_PROJECT and PUBSUB_TOPIC must both be configured.")
    return get_pubsub_publisher().topic_path(GCP_PROJECT, PUBSUB_TOPIC)


def get_redis_client():
    """Create a Redis client lazily and verify the connection."""
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


def upsert_task_record(task_id, scholar_url, status, message=None, result=None):
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


def get_task_record(task_id):
    """Load a task record and parse its JSON payload."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, scholar_url, status, message, result_json, created_at, updated_at
                FROM index_tasks
                WHERE task_id = %s
                """,
                (task_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    result = json.loads(row[4]) if row[4] else None
    return {
        "task_id": row[0],
        "scholar_url": row[1],
        "status": row[2],
        "message": row[3],
        "result": result,
        "created_at": row[5],
        "updated_at": row[6],
    }


def sync_session_task_state():
    """Refresh the current session's indexing state from PostgreSQL."""
    task_id = session.get("task_id")
    if not task_id:
        return None

    task = get_task_record(task_id)
    if not task:
        return None

    if task["status"] == "COMPLETE":
        session["indexed"] = True
        session["last_task_result"] = task.get("result") or {}
    elif task["status"] in {"PENDING", "RUNNING"}:
        session["indexed"] = False
    return task


def require_index_ready():
    """Ensure the current session has a completed indexing task."""
    task = sync_session_task_state()
    if session.get("indexed"):
        return None

    if task and task["status"] in {"PENDING", "RUNNING"}:
        flash("Your indexing task is still running.", "error")
        return redirect(url_for("task_status", task_id=task["task_id"]))

    flash("Please start an indexing task first.", "error")
    return redirect(url_for("index"))


def submit_index_task(scholar_url):
    """Persist a task row and enqueue the indexing work."""
    task_id = str(uuid.uuid4())
    upsert_task_record(
        task_id, scholar_url, "PENDING", "Task accepted and waiting for a worker."
    )

    message = {
        "task_id": task_id,
        "action": "index",
        "scholar_url": scholar_url,
        "submitted_at": int(time.time()),
    }

    future = get_pubsub_publisher().publish(
        get_pubsub_topic_path(),
        json.dumps(message).encode("utf-8"),
        task_id=task_id,
        action="index",
    )

    try:
        future.result(timeout=10)
        return task_id
    except Exception as exc:
        upsert_task_record(
            task_id, scholar_url, "FAILED", f"Failed to enqueue task: {exc}"
        )
        raise


def query_term_from_postgres(term):
    """Query the persisted inverted index directly from PostgreSQL."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT doc_ids FROM inverted_index WHERE term = %s", (term,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row[0]:
        return []

    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        logger.error("Corrupt JSON payload for term %s", term)
        return []


def query_topn_from_postgres(limit):
    """Query the ranked term counts from PostgreSQL."""
    ensure_schema()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT term, count
                FROM inverted_index
                ORDER BY count DESC, term ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [{"term": term, "frequency": count} for term, count in rows]


def cache_search_result(term, results):
    """Warm the Redis term cache after a PostgreSQL fallback."""
    client = get_redis_client()
    if not client:
        return

    try:
        client.set(f"search:{term}", json.dumps(results))
    except Exception as exc:
        logger.warning("Unable to cache search result for %s: %s", term, exc)


@app.route("/")
def index():
    """Home page - Enter Google Scholar Search URL."""
    return render_template("index.html")


@app.route("/health")
def health():
    """Cheap liveness probe for the load balancer."""
    return jsonify({"status": "ok"}), 200


@app.route("/ready")
def ready():
    """Readiness probe that checks the shared dependencies."""
    status = {
        "database": False,
        "redis": False,
        "pubsub": bool(GCP_PROJECT and PUBSUB_TOPIC),
    }
    code = 200

    try:
        ensure_schema()
        status["database"] = True
    except Exception as exc:
        logger.warning("Readiness database check failed: %s", exc)
        code = 503

    try:
        client = get_redis_client()
        status["redis"] = bool(client and client.ping())
    except Exception:
        status["redis"] = False

    if not status["pubsub"]:
        code = 503

    status["status"] = "ready" if code == 200 else "degraded"
    return jsonify(status), code


@app.route("/metrics")
def metrics():
    """Expose Prometheus metrics for the web tier."""
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/load", methods=["POST"])
def load_engine():
    """Submit an asynchronous indexing task for the provided Scholar URL."""
    scholar_url = request.form.get("scholar_url", "").strip()

    if not scholar_url or "scholar.google.com" not in scholar_url:
        flash("Please enter a valid Google Scholar URL.", "error")
        return redirect(url_for("index"))

    try:
        task_id = submit_index_task(scholar_url)
        INDEX_SUBMISSIONS_TOTAL.labels("accepted").inc()
    except Exception as exc:
        logger.exception("Failed to submit indexing task.")
        INDEX_SUBMISSIONS_TOTAL.labels("failed").inc()
        flash(f"Failed to submit indexing task: {exc}", "error")
        return redirect(url_for("index"))

    session["task_id"] = task_id
    session["scholar_url"] = scholar_url
    session["indexed"] = False
    return redirect(url_for("task_status", task_id=task_id))


@app.route("/tasks/<task_id>")
def task_status(task_id):
    """Render the current state of an indexing task."""
    task = get_task_record(task_id)
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("index"))

    session["task_id"] = task_id
    if task["status"] == "COMPLETE":
        session["indexed"] = True
        session["last_task_result"] = task.get("result") or {}
    elif task["status"] in {"PENDING", "RUNNING"}:
        session["indexed"] = False

    return render_template(
        "task_status.html",
        task=task,
        poll_interval=TASK_STATUS_POLL_INTERVAL,
    )


@app.route("/api/tasks/<task_id>")
def task_status_api(task_id):
    """Return the latest task state as JSON for in-page polling."""
    task = get_task_record(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404

    if task["status"] == "COMPLETE":
        session["task_id"] = task_id
        session["indexed"] = True
        session["last_task_result"] = task.get("result") or {}
    elif task["status"] in {"PENDING", "RUNNING"}:
        session["task_id"] = task_id
        session["indexed"] = False

    result = task.get("result") or {}
    return jsonify(
        {
            "task_id": task["task_id"],
            "scholar_url": task["scholar_url"],
            "status": task["status"],
            "message": task["message"] or "",
            "result": result,
        }
    )


@app.route("/select")
def select_action():
    """Action selection page after indexing completes successfully."""
    redirect_response = require_index_ready()
    if redirect_response:
        return redirect_response

    return render_template(
        "select_action.html",
        task_result=session.get("last_task_result", {}),
    )


@app.route("/search", methods=["GET", "POST"])
def search_term():
    """Search for a term in the persisted inverted index."""
    redirect_response = require_index_ready()
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        search_term_value = request.form.get("search_term", "").strip().lower()
        if not search_term_value:
            flash("Please enter a search term.", "error")
            return redirect(url_for("search_term"))

        start_time = time.time()
        client = get_redis_client()
        if client:
            try:
                cached_data = client.get(f"search:{search_term_value}")
                if cached_data:
                    results = json.loads(cached_data)
                    SEARCH_QUERIES_TOTAL.labels("redis").inc()
                    execution_time = round((time.time() - start_time) * 1000, 2)
                    return render_template(
                        "search_results.html",
                        term=search_term_value,
                        results=results,
                        execution_time=execution_time,
                    )
            except Exception as exc:
                logger.warning(
                    "Redis lookup failed for term %s: %s", search_term_value, exc
                )

        try:
            results = query_term_from_postgres(search_term_value)
            cache_search_result(search_term_value, results)
            SEARCH_QUERIES_TOTAL.labels("postgres").inc()
        except Exception as exc:
            logger.exception("Search fallback query failed.")
            SEARCH_QUERIES_TOTAL.labels("failed").inc()
            flash(f"Search failed: {exc}", "error")
            return redirect(url_for("search_term"))

        execution_time = round((time.time() - start_time) * 1000, 2)
        return render_template(
            "search_results.html",
            term=search_term_value,
            results=results,
            execution_time=execution_time,
        )

    return render_template("search_term.html")


@app.route("/topn", methods=["GET", "POST"])
def top_n():
    """Find the Top-N most frequent terms."""
    redirect_response = require_index_ready()
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        try:
            n = int(request.form.get("n", "10"))
        except ValueError:
            flash("Please enter a valid number.", "error")
            return redirect(url_for("top_n"))

        if n <= 0:
            flash("Please enter a positive number.", "error")
            return redirect(url_for("top_n"))

        start_time = time.time()
        client = get_redis_client()
        if client:
            try:
                cached_topn = client.zrevrange("term_freq", 0, n - 1, withscores=True)
                if cached_topn:
                    results = [
                        {"term": term, "frequency": int(score)}
                        for term, score in cached_topn
                    ]
                    TOPN_QUERIES_TOTAL.labels("redis").inc()
                    execution_time = round((time.time() - start_time) * 1000, 2)
                    return render_template(
                        "topn_results.html",
                        n=n,
                        results=results,
                        execution_time=execution_time,
                    )
            except Exception as exc:
                logger.warning("Redis top-N lookup failed: %s", exc)

        try:
            results = query_topn_from_postgres(n)
            TOPN_QUERIES_TOTAL.labels("postgres").inc()
        except Exception as exc:
            logger.exception("Top-N fallback query failed.")
            TOPN_QUERIES_TOTAL.labels("failed").inc()
            flash(f"Top-N query failed: {exc}", "error")
            return redirect(url_for("top_n"))

        execution_time = round((time.time() - start_time) * 1000, 2)
        return render_template(
            "topn_results.html",
            n=n,
            results=results,
            execution_time=execution_time,
        )

    return render_template("topn.html")


@app.route("/reset")
def reset():
    """Reset the session and go back to the home page."""
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

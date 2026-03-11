"""
Lightweight Flask Web Application for IEEE Xplore Search Engine.
This application serves as the thin client / user-facing interface.
It delegates all heavy processing to the cluster-based backend via Kafka.
"""

import os
import json
import time
import uuid
import logging
from flask import Flask, render_template, request, redirect, url_for, session, flash
from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import NoBrokersAvailable

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ieee-search-engine-secret-key")

# Kafka Configuration (loaded from environment variables)
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
KAFKA_REQUEST_TOPIC = os.environ.get("KAFKA_REQUEST_TOPIC", "search-requests")
KAFKA_RESPONSE_TOPIC = os.environ.get("KAFKA_RESPONSE_TOPIC", "search-responses")
KAFKA_TIMEOUT = int(os.environ.get("KAFKA_TIMEOUT", "300"))  # seconds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_kafka_producer():
    """Create and return a Kafka producer instance."""
    try:
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            request_timeout_ms=10000,
            retries=3,
        )
        return producer
    except NoBrokersAvailable:
        logger.error("Kafka broker not available at %s", KAFKA_BROKER)
        return None


def send_kafka_message(message):
    """Send a message to the Kafka request topic and wait for a response."""
    request_id = str(uuid.uuid4())
    message["request_id"] = request_id

    try:
        consumer = KafkaConsumer(
            KAFKA_RESPONSE_TOPIC,
            bootstrap_servers=KAFKA_BROKER,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            auto_offset_reset="latest",
            group_id=f"flask-app-{request_id}",
        )
        consumer.poll(timeout_ms=1000)
    except Exception as e:
        logger.error("Kafka consumer init failed: %s", str(e))
        return {"error": "Failed to connect to cluster."}

    producer = get_kafka_producer()
    if producer is None:
        consumer.close()
        return {"error": "Kafka broker is not available."}

    try:
        producer.send(KAFKA_REQUEST_TOPIC, value=message)
        producer.flush()
        logger.info("Sent message to Kafka: %s", message)
    except Exception as e:
        logger.error("Failed to send message: %s", str(e))
        consumer.close()
        return {"error": f"Failed to send message: {str(e)}"}
    finally:
        producer.close()

    start_time = time.time()
    try:
        while time.time() - start_time < KAFKA_TIMEOUT:
            records = consumer.poll(timeout_ms=1000)
            for topic_partition, messages in records.items():
                for msg in messages:
                    response = msg.value
                    if response.get("request_id") == request_id:
                        consumer.close()
                        return response
    except Exception as e:
        logger.error("Error reading from Kafka: %s", str(e))
    
    consumer.close()
    return {"error": f"Timeout ({KAFKA_TIMEOUT}s). The cluster might be busy running Hadoop."}


@app.route("/")
def index():
    """Home page - Enter Google Scholar Search URL."""
    return render_template("index.html")


@app.route("/load", methods=["POST"])
def load_engine():
    """Send the Google Scholar URL to the cluster for indexing."""
    scholar_url = request.form.get("scholar_url", "").strip()

    if not scholar_url:
        flash("Please enter a valid Google Scholar URL.", "error")
        return redirect(url_for("index"))

    # Validate URL format
    if "scholar.google.com" not in scholar_url:
        flash("Please enter a valid Google Scholar URL.", "error")
        return redirect(url_for("index"))

    # Send indexing request to cluster via Kafka
    message = {
        "action": "index",
        "scholar_url": scholar_url,
    }

    response = send_kafka_message(message)

    if "error" in response:
        flash(f"Error: {response['error']}", "error")
        return redirect(url_for("index"))

    # Store the session state
    session["indexed"] = True
    session["scholar_url"] = scholar_url
    session["index_data"] = response.get("data", {})

    return redirect(url_for("select_action"))


@app.route("/select")
def select_action():
    """Action selection page after indexing is complete."""
    if not session.get("indexed"):
        flash("Please load a Google Scholar URL first.", "error")
        return redirect(url_for("index"))
    return render_template("select_action.html")


@app.route("/search", methods=["GET", "POST"])
def search_term():
    """Search for a term in the inverted indices."""
    if not session.get("indexed"):
        flash("Please load a Google Scholar URL first.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        search_term_value = request.form.get("search_term", "").strip()

        if not search_term_value:
            flash("Please enter a search term.", "error")
            return redirect(url_for("search_term"))

        # Send search request to cluster via Kafka
        message = {
            "action": "search",
            "term": search_term_value,
        }

        response = send_kafka_message(message)

        if "error" in response:
            flash(f"Error: {response['error']}", "error")
            return redirect(url_for("search_term"))

        results = response.get("results", [])
        execution_time = response.get("execution_time_ms", "N/A")

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
    if not session.get("indexed"):
        flash("Please load a Google Scholar URL first.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        try:
            n = int(request.form.get("n", "10"))
        except ValueError:
            flash("Please enter a valid number.", "error")
            return redirect(url_for("top_n"))

        if n <= 0:
            flash("Please enter a positive number.", "error")
            return redirect(url_for("top_n"))

        # Send Top-N request to cluster via Kafka
        message = {
            "action": "topn",
            "n": n,
        }

        response = send_kafka_message(message)

        if "error" in response:
            flash(f"Error: {response['error']}", "error")
            return redirect(url_for("top_n"))

        results = response.get("results", [])
        execution_time = response.get("execution_time_ms", "N/A")

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
    app.run(host="0.0.0.0", port=port, debug=True)

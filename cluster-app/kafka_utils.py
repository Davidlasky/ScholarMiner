"""
Kafka utility module for the cluster-based backend application.
Consumes requests from the lightweight app and produces responses.
"""

import os
import json
import logging
from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
KAFKA_REQUEST_TOPIC = os.environ.get("KAFKA_REQUEST_TOPIC", "search-requests")
KAFKA_RESPONSE_TOPIC = os.environ.get("KAFKA_RESPONSE_TOPIC", "search-responses")


def create_producer():
    """Create a Kafka producer for sending responses back to the lightweight app."""
    try:
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            request_timeout_ms=10000,
            retries=3,
        )
        logger.info("Kafka producer connected to %s", KAFKA_BROKER)
        return producer
    except NoBrokersAvailable:
        logger.error("Cannot connect to Kafka broker at %s", KAFKA_BROKER)
        raise


def create_consumer():
    """Create a Kafka consumer for receiving requests from the lightweight app."""
    try:
        consumer = KafkaConsumer(
            KAFKA_REQUEST_TOPIC,
            bootstrap_servers=KAFKA_BROKER,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            auto_offset_reset="latest",
            group_id="cluster-backend",
            enable_auto_commit=True,
        )
        logger.info("Kafka consumer connected to %s, topic: %s", KAFKA_BROKER, KAFKA_REQUEST_TOPIC)
        return consumer
    except NoBrokersAvailable:
        logger.error("Cannot connect to Kafka broker at %s", KAFKA_BROKER)
        raise


def send_response(producer, response):
    """Send a response message back to the lightweight app via Kafka."""
    try:
        producer.send(KAFKA_RESPONSE_TOPIC, value=response)
        producer.flush()
        logger.info("Sent response for request_id: %s", response.get("request_id"))
    except Exception as e:
        logger.error("Failed to send response: %s", str(e))
        raise

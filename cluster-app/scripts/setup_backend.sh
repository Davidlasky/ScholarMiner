#!/bin/bash
###############################################################################
# Setup script for the cluster-based backend application.
# Run this on the Dataproc master node.
#
# NOTE: Dataproc clusters use a Conda-managed Python environment at
# /opt/conda/default/bin/python3. We must install packages using the
# Conda pip to ensure they are visible to the default python3 command.
###############################################################################

set -e

echo "=== Setting up IEEE Search Engine Backend ==="

# Install Python dependencies using the Conda-managed pip
# (Dataproc's default python3 is /opt/conda/default/bin/python3, NOT /usr/bin/python3)
echo "Installing Python dependencies into Conda environment..."
/opt/conda/default/bin/pip install kafka-python-ng requests beautifulsoup4

# Verify the installation
echo "Verifying kafka-python-ng installation..."
/opt/conda/default/bin/python3 -c "import kafka; print('kafka-python-ng installed successfully')"

# Get Kafka broker IP from cluster metadata
KAFKA_BROKER=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/kafka-broker" -H "Metadata-Flavor: Google" 2>/dev/null || echo "")

if [ -z "$KAFKA_BROKER" ]; then
    echo "WARNING: Kafka broker IP not found in metadata. Using default."
    KAFKA_BROKER="localhost:9092"
else
    KAFKA_BROKER="${KAFKA_BROKER}:9093"
fi

echo "Kafka broker: $KAFKA_BROKER"

# Set environment variables
export KAFKA_BROKER="$KAFKA_BROKER"
export KAFKA_REQUEST_TOPIC="search-requests"
export KAFKA_RESPONSE_TOPIC="search-responses"

# Get GCS bucket name
GCS_BUCKET=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/dataproc-bucket" -H "Metadata-Flavor: Google" 2>/dev/null || echo "")
export GCS_BUCKET="$GCS_BUCKET"

echo "GCS Bucket: $GCS_BUCKET"

# Find Hadoop streaming jar
HADOOP_STREAMING_JAR=$(find /usr/lib -name 'hadoop-streaming*.jar' 2>/dev/null | head -1)
export HADOOP_STREAMING_JAR="$HADOOP_STREAMING_JAR"
echo "Hadoop streaming jar: $HADOOP_STREAMING_JAR"

# Create working directory and copy files
WORK_DIR="/opt/ieee-search-engine"
sudo mkdir -p "$WORK_DIR"
sudo cp -r /tmp/cluster-app/* "$WORK_DIR/" 2>/dev/null || true

echo "=== Setup complete ==="
echo ""
echo "To start the backend, run:"
echo "  cd $WORK_DIR && python3 backend.py"

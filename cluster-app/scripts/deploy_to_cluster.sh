#!/bin/bash
###############################################################################
# Deploy the backend application to the Dataproc cluster master node.
# Run this from your local machine after Terraform has provisioned the cluster.
###############################################################################

set -e

# Configuration
PROJECT_ID="${1:-aerial-citron-428307-c5}"
CLUSTER_NAME="${2:-ieee-search-cluster}"
REGION="${3:-us-west1}"
ZONE="${4:-us-west1-a}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_APP_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Deploying Backend to Dataproc Cluster ==="
echo "Project: $PROJECT_ID"
echo "Cluster: $CLUSTER_NAME"
echo "Region: $REGION"

# Get the master node name
MASTER_NODE=$(gcloud dataproc clusters describe "$CLUSTER_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --format="value(config.masterConfig.instanceNames[0])")

echo "Master node: $MASTER_NODE"

# Get Kafka broker IP from Terraform output
KAFKA_IP=$(cd "$CLUSTER_APP_DIR/../terraform" && terraform output -raw kafka_internal_ip 2>/dev/null || echo "")
GCS_BUCKET=$(cd "$CLUSTER_APP_DIR/../terraform" && terraform output -raw gcs_bucket 2>/dev/null || echo "")

echo "Kafka IP: $KAFKA_IP"
echo "GCS Bucket: $GCS_BUCKET"

# Copy cluster-app files to the master node
echo "Copying files to master node..."
gcloud compute scp --recurse "$CLUSTER_APP_DIR" "${MASTER_NODE}:/tmp/cluster-app" \
    --zone="$ZONE" \
    --project="$PROJECT_ID"

# Run setup script on master node
echo "Running setup on master node..."
gcloud compute ssh "$MASTER_NODE" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --command="chmod +x /tmp/cluster-app/scripts/setup_backend.sh && /tmp/cluster-app/scripts/setup_backend.sh"

# Start the backend
echo "Starting backend application..."
gcloud compute ssh "$MASTER_NODE" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --command="cd /opt/ieee-search-engine && \
        export KAFKA_BROKER='${KAFKA_IP}:9093' && \
        export GCS_BUCKET='${GCS_BUCKET}' && \
        export KAFKA_REQUEST_TOPIC='search-requests' && \
        export KAFKA_RESPONSE_TOPIC='search-responses' && \
        nohup python3 backend.py > /tmp/backend.log 2>&1 &"

echo ""
echo "=== Deployment Complete ==="
echo "Backend is running on $MASTER_NODE"
echo "Check logs: gcloud compute ssh $MASTER_NODE --zone=$ZONE --command='tail -f /tmp/backend.log'"


# ScholarMiner Deployment Guide

This guide provides the technical steps to deploy the high-availability **ScholarMiner** search engine on Google Cloud Platform.

## 1. Prerequisites & Authentication

Ensure you have the Google Cloud SDK, Terraform, and Docker installed.

```bash
# Authenticate and set your project
gcloud auth login
gcloud config set project [YOUR_PROJECT_ID]
gcloud auth application-default login

```

## 2. Infrastructure Provisioning (Terraform)

The infrastructure uses **Debian 12** for all nodes to ensure compatibility with modern Docker Compose plugins.

1. Navigate to the `terraform/` directory.
2. Initialize and deploy:
```bash
terraform init
terraform apply -auto-approve

```


3. **Note the Outputs**: After ~10 minutes, Terraform will output the `search_engine_url`, `grafana_url`, `kafka_internal_ip`, and `backend_db_internal_ip`.

> **Note**: The PostgreSQL schema (`inverted_index` table) is automatically initialized during the `backend_node` startup script.

## 3. Deploy the Backend Worker to Dataproc

The worker bridges Kafka messages to Hadoop MapReduce jobs.

1. **Upload Code to Master Node**:
From your local `ScholarMiner` root:
```bash
gcloud compute scp --recurse cluster-app ieee-search-cluster-m:/tmp/ --zone=[YOUR_ZONE]

```


2. **SSH into the Master Node**:
```bash
gcloud compute ssh ieee-search-cluster-m --zone=[YOUR_ZONE]

```


3. **Configure and Run the Worker**:
Install dependencies and start the backend listener using the internal IPs from your Terraform outputs.
```bash
#!/bin/bash
set -e

# Define operational zone
ZONE="us-west1-a"

# Dynamic Discovery: Retrieve Internal IPs
echo "Querying infrastructure metadata..."
DB_INTERNAL_IP=$(gcloud compute instances describe ieee-backend-node --zone=$ZONE --format='get(networkInterfaces[0].networkIP)')
KAFKA_INTERNAL_IP=$(gcloud compute instances describe kafka-broker --zone=$ZONE --format='get(networkInterfaces[0].networkIP)')

# Project Context Discovery
PROJECT_ID=$(gcloud config get-value project)
BUCKET_NAME="${PROJECT_ID}-ieee-search-data"

# Environment Configuration
export DB_HOST="$DB_INTERNAL_IP"
export KAFKA_BROKER="$KAFKA_INTERNAL_IP:9093"
export GCS_BUCKET="$BUCKET_NAME"

echo "Configuration Validated:"
echo " - Backend DB: $DB_HOST"
echo " - Kafka Broker: $KAFKA_BROKER"
echo " - GCS Bucket: $GCS_BUCKET"

# Dependency Resolution
echo "Installing Python dependencies..."
/opt/conda/default/bin/pip install --quiet kafka-python-ng requests beautifulsoup4

# Process Management
cd /tmp/cluster-app
echo "Restarting Backend Worker..."
pkill -f backend.py || true
nohup python3 backend.py > /tmp/backend.log 2>&1 &

# Initial Health Check
sleep 3
if grep -q "Connected to Kafka successfully" /tmp/backend.log; then
    echo "Deployment Successful: Worker is listening for requests."
else
    echo "Warning: Worker started but connection not yet confirmed. Check /tmp/backend.log."
fi

```



## 4. Verification & Observability

1. **Search UI**: Access the search engine at the `search_engine_url` (Port 5000).
2. **Monitoring**: Access Grafana at the `grafana_url` (Port 3000) to view real-time system metrics and traffic spikes.
3. **Hadoop Logs**: If jobs are not appearing in the GCP Console, check the local YARN resource manager on the master node or view `/tmp/backend.log`.

## 5. Cleanup

To avoid ongoing GCP charges, destroy all resources immediately after testing.

```bash
terraform destroy -auto-approve

```

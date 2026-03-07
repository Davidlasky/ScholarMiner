
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
# Install dependencies in the Dataproc Conda environment
/opt/conda/default/bin/pip install kafka-python-ng requests beautifulsoup4

cd /tmp/cluster-app
export DB_HOST='[backend_db_internal_ip]'
export KAFKA_BROKER='[kafka_internal_ip]:9093'
export GCS_BUCKET='[project-id]-ieee-search-data'

# Start worker in background
nohup python3 backend.py > /tmp/backend.log 2>&1 &
tail -f /tmp/backend.log

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

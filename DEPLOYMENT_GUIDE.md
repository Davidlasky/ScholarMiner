# ScholarMiner Deployment Guide

This guide covers the current deployment workflow for ScholarMiner on Google Cloud Platform. The deployment is Terraform-first: infrastructure provisioning, frontend deployment, backend artifact upload, and Dataproc initialization are handled automatically by the Terraform configuration.

## Prerequisites

Install the following locally before deploying:

- Google Cloud SDK
- Terraform
- Docker

Authenticate with Google Cloud and set the active project:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
```

## Deploy with Terraform

1. Create a Terraform variables file:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

2. Update `terraform.tfvars` with your values:

- `project_id`
- `region`
- `zone`
- `serpapi_key`

3. Initialize Terraform:

```bash
terraform init
```

4. Apply the configuration:

```bash
terraform apply -auto-approve
```

Provisioning can take several minutes. During this process, Terraform:

- creates the GCP infrastructure
- builds and pushes the frontend container image
- provisions the web node, Kafka VM, backend node, and Dataproc cluster
- uploads backend scripts to GCS
- runs the Dataproc initialization script that installs dependencies and starts the backend worker
- initializes the PostgreSQL schema on the backend node

## Terraform Outputs

After a successful apply, Terraform will print deployment details including:

- `search_engine_url`
- `grafana_dashboard_url`
- `dataproc_cluster_name`
- `dataproc_master_instance`
- `kafka_internal_ip`
- `kafka_external_ip`
- `gcs_bucket`

You can retrieve them again later with:

```bash
terraform output
```

## Verify the Deployment

Use the following checks after provisioning:

1. Open `search_engine_url` and confirm the Flask application loads.
2. Open `grafana_dashboard_url` and confirm Grafana is reachable.
3. In the GCP Console, confirm the Dataproc cluster is healthy and the instances are running.
4. Submit a sample Google Scholar URL through the web app and verify that indexing completes and search results are returned.

## Troubleshooting

If deployment succeeds but indexing or queries fail:

- inspect Terraform output for missing or unexpected values
- check the Dataproc cluster status in the GCP Console
- inspect the backend worker log on the Dataproc master at `/var/log/backend.log`
- verify that the SerpAPI key in `terraform.tfvars` is valid
- confirm the web node can reach Kafka and Redis through the provisioned internal network

## Cleanup

To remove all provisioned infrastructure and avoid ongoing cloud charges:

```bash
terraform destroy -auto-approve
```

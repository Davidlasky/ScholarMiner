# ScholarMiner Deployment Guide

This guide covers the current deployment workflow for ScholarMiner on Google Cloud Platform. The deployment is Terraform-first: infrastructure provisioning, container image builds, regional web and worker services, managed stateful services, and the Dataproc batch cluster are all handled by the Terraform configuration.

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

1. Update `terraform.tfvars` with your values:

- `project_id`
- `region`
- `zone`
- `serpapi_key`
- `frontend_image`
- `worker_image`
- `data_bucket_force_destroy`

For the usual demo workflow, set `data_bucket_force_destroy = true` so `terraform destroy` also removes the versioned data bucket contents. Set it to `false` if you want to protect indexed data from accidental deletion.

1. Initialize Terraform:

```bash
terraform init
```

1. Apply the configuration:

```bash
terraform apply -auto-approve
```

Provisioning can take several minutes. During this process, Terraform:

- creates the GCP infrastructure
- builds and pushes the frontend and worker container images
- provisions a regional web tier behind an external load balancer
- provisions a regional worker tier that consumes asynchronous indexing tasks
- creates Pub/Sub, Cloud SQL, Memorystore, Secret Manager secrets, and a versioned GCS bucket
- provisions the Dataproc cluster used for Hadoop Streaming jobs
- provisions an observability node with Prometheus and Grafana, including auto-provisioned datasources and a starter dashboard

## Terraform Outputs

After a successful apply, Terraform will print deployment details including:

- `search_engine_url`
- `grafana_dashboard_url`
- `prometheus_url`
- `grafana_admin_password`
- `dataproc_cluster_name`
- `dataproc_master_instance`
- `gcs_bucket`
- `cloudsql_private_ip`
- `redis_host`
- `pubsub_topic`

You can retrieve them again later with:

```bash
terraform output
```

For the Grafana login password specifically:

```bash
terraform output -raw grafana_admin_password
```

## Verify the Deployment

Use the following checks after provisioning:

1. Open `search_engine_url` and confirm the Flask application loads.
2. Open `grafana_dashboard_url` and confirm Grafana is reachable.
3. Open `prometheus_url` and confirm Prometheus is reachable and scraping the web, worker, and node-exporter targets.
4. In Grafana, confirm the pre-provisioned Prometheus and Google Cloud Monitoring datasources are present.
5. In the GCP Console, confirm the Dataproc cluster is healthy and the instances are running.
6. Submit a sample Google Scholar URL through the web app, wait for the task status page to report completion, and verify that search results are returned.

## Troubleshooting

If deployment succeeds but indexing or queries fail:

- inspect Terraform output for missing or unexpected values
- check the Dataproc cluster status in the GCP Console
- inspect a worker instance in the managed instance group and review `docker logs scholarminer-worker`
- verify that the SerpAPI key in `terraform.tfvars` is valid
- confirm the web and worker instances can reach Cloud SQL, Memorystore, Pub/Sub, and GCS through the provisioned network

## Cleanup

To remove all provisioned infrastructure and avoid ongoing cloud charges:

```bash
terraform destroy -auto-approve
```

If `data_bucket_force_destroy` is `false`, empty the data bucket first or Terraform will intentionally refuse to delete it.

The Terraform config sets `deletion_policy = "ABANDON"` on the Private Services Access connection so `destroy` does not fail when Google keeps producer-side subnets around after Cloud SQL or Memorystore deletion.

If your deployment was created before this workaround was added, run the following once before cleanup so Terraform records the policy in state:

```bash
terraform apply -target=google_service_networking_connection.private_service_connection -auto-approve
```

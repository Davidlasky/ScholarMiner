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
- `build_images` (default `true`; set `false` to deploy existing image tags when local Docker is unavailable)
- `observability_allowed_cidrs` (default `[]`, which keeps Grafana and Prometheus private)

For the usual demo workflow, set `data_bucket_force_destroy = true` so `terraform destroy` also removes the versioned data bucket contents. Set it to `false` if you want to protect indexed data from accidental deletion.

1. Initialize Terraform:

```bash
terraform init
```

1. Apply the configuration:

```bash
terraform apply -auto-approve
```

For a disposable debugging deployment, you may pass
`-var='observability_allowed_cidrs=["0.0.0.0/0"]'` to expose Grafana (3000) and
Prometheus (9090) publicly. Do not leave that deployment running afterward.
When `build_images = false`, verify both specified tags exist in the image
registry before applying; Terraform will not rebuild them. Do not use an older
worker tag for fresh indexing if it predates the Hadoop Streaming JAR-path fix
in `cluster-app/backend.py`.

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

## Reproduce the Top-N benchmark

Use a **disposable, empty deployment** for the deterministic synthetic corpus.
The benchmark seed script refuses to replace a non-empty index. It does not
scrape Google Scholar, so it measures indexed query paths rather than scraping
quality or a production dataset. Keep the generated corpus hash and the JSON
result together. The full procedure is:

1. Generate a TSV corpus with `utils/generate_benchmark_corpus.py`, record its
   SHA-256, and upload it to a unique `input/<run-id>/papers.tsv` GCS path.
2. Submit `inverted_index_mapper.py` and `inverted_index_reducer.py` to the
   deployment's Dataproc cluster with Hadoop Streaming. Use the JAR at
   `file:///usr/lib/hadoop/hadoop-streaming.jar`; place output under
   `output/inverted_index/<run-id>/` and check that `part-*` is non-empty.
3. On one healthy worker VM, copy `cluster-app/scripts/seed_benchmark_index.py`
   into the worker container and run it with
   `SCHOLARMINER_ALLOW_BENCHMARK_SEED=1`, `--index-output-task-id <run-id>`,
   `--source-url https://scholar.google.com/scholar?q=scholarminer-benchmark-<run-id>`,
   `--session-task-id <new-uuid>`, and `--papers-indexed <count>`. It verifies
   the Redis sorted set size and creates a completed task for the web session.
4. Run `utils/benchmark_topn.py` against `search_engine_url` with `--seeded`,
   `--completed-task-id <new-uuid>`, `--index-output-task-id <run-id>`, the
   deployment project/region/cluster/bucket, corpus hash/count, sample counts,
   and `--output <result.json>`. The script verifies that every web Top-N query
   hits Redis and that its results match repeated Hadoop Top-N jobs on the same
   inverted-index output. It reports application processing time separately
   from full HTTP response time and Hadoop job wall-clock time.
   To measure a second web access path without repeating Dataproc jobs, pass
   `--baseline-runs 0 --baseline-reference <first-result.json>`; the script
   checks corpus, index, and result identity and labels the reused baseline.
5. Run `terraform destroy` and verify Cloud SQL, Memorystore, Dataproc, VMs,
   and ScholarMiner Pub/Sub resources are gone. These can continue to incur
   charges if only the compute instances are removed.

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

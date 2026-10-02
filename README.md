# ScholarMiner

ScholarMiner is a distributed search and indexing system for research papers. The application accepts a Google Scholar results URL, extracts matching IEEE papers and abstracts, builds an inverted index with Hadoop Streaming, and exposes search and Top-N term queries through a Flask web interface.

The current deployment uses a single-region high-availability design: a load-balanced web tier submits asynchronous indexing tasks through Pub/Sub, a separate worker service drives scraping and Dataproc jobs, and managed stateful services handle the cache and durable persistence layers.

## Core Capabilities

- Accepts a Google Scholar results URL from the web application
- Uses SerpAPI to discover IEEE papers from Google Scholar and Scrapling to retrieve abstracts, with Scholar snippets as a fallback
- Builds and refreshes an inverted index with Hadoop Streaming on Dataproc
- Serves low-latency search and Top-N term queries through Memorystore-backed Redis
- Persists indexed state to Cloud SQL for PostgreSQL and backs up cache state to Google Cloud Storage
- Tracks long-running indexing work through asynchronous task status records
- Exposes Prometheus metrics for the web tier and worker tier, with Grafana auto-provisioned for dashboarding
- Provisions cloud infrastructure with Terraform

## Technology Stack

- Python
- Flask
- Google Pub/Sub
- SerpAPI / Scrapling
- Hadoop Streaming / Dataproc
- Memorystore for Redis
- Cloud SQL for PostgreSQL
- Secret Manager
- Prometheus
- Grafana
- Terraform
- Docker
- Google Cloud Platform

## System Architecture

```mermaid
flowchart LR
    U[Browser] --> LB[Global HTTP Load Balancer]
    LB --> W[Flask Web Containers / Regional MIG]

    W -->|submit indexing task| Q[(Pub/Sub)]
    Q --> B[Worker Containers / Regional MIG]

    subgraph DP[Dataproc Cluster]
        H[Hadoop Streaming Jobs]
    end

    B -->|submit indexing job| H
    B --> S[Scraper]
    B --> R[(Memorystore Redis)]
    B --> P[(Cloud SQL PostgreSQL)]
    B --> G[(Google Cloud Storage)]
    H -->|read input / write index shards| G
    subgraph O[Observability VM]
        M[Prometheus]
        D[Grafana]
    end
    D -->|query metrics| M

    W -->|task status + query fallback| P
    W -->|cached reads| R
    M -->|scrape web metrics| W
    M -->|scrape worker metrics| B
    M -->|scrape host metrics| N[Web and Worker Node Exporters]
```

The web and worker containers run in separate regional managed instance groups, with two instances each by default. Cloud SQL uses regional availability and Memorystore uses `STANDARD_HA`. Dataproc has three masters and two workers in the configured zone; the observability stack runs on one VM. Secret Manager supplies application credentials to the containers.

## Engineering Decisions and Design Patterns

### Event-Driven Task Submission

The frontend does not execute long-running scraping or indexing work directly. Instead, it submits indexing tasks to Pub/Sub and tracks progress through task status rows in PostgreSQL. This follows a producer-consumer model while avoiding the fragility of synchronous request/reply messaging for long-running batch work.

### CQRS-Inspired Read/Write Separation

The project uses a practical read/write split:

- Write-heavy operations such as scraping, Hadoop indexing, and index refreshes run asynchronously in the backend worker
- Read-heavy operations such as term lookup and Top-N queries are served directly from Redis whenever possible

This keeps interactive queries fast while isolating the more expensive indexing workflow behind an asynchronous boundary.

### Idempotent Indexing and Duplicate Work Avoidance

To avoid repeating expensive scrape-and-index operations for the same source URL, the worker stores previously processed source URLs in PostgreSQL and mirrors them into Redis when available. Repeated indexing requests can short-circuit early, which reduces unnecessary external API usage and redundant cluster work.

A PostgreSQL advisory lock serializes index rebuilds across workers. Each successful rebuild replaces the shared index with the submitted source's snapshot; separate submissions do not accumulate into a combined corpus or create per-user indexes.

### Polyglot Persistence by Access Pattern

Different storage systems are used for different operational goals:

- Redis handles latency-sensitive reads and ranked term queries
- PostgreSQL stores structured index data durably, including task status and indexed source tracking
- GCS is used as a recovery layer so the worker can restore previously computed state on startup

This is a deliberate trade-off in favor of operational clarity rather than forcing every workload through a single datastore.

### Batch Processing with a Thin Interactive Layer

The heavy computation is pushed into Hadoop Streaming jobs rather than the web process. The Flask application remains a thin orchestration layer, while the backend handles distributed processing, persistence, and cache refreshes. This separation keeps the application easier to reason about and makes performance bottlenecks more explicit.

## Request Flow

1. A user submits a Google Scholar results URL in the Flask application.
2. The app records a task row in PostgreSQL and publishes the indexing request to Pub/Sub.
3. A worker service consumes the task, scrapes paper data, uploads input shards to GCS, and submits a Dataproc job.
4. Hadoop Streaming generates the updated inverted index.
5. The worker persists the latest snapshot to Redis and PostgreSQL, and writes a recovery copy to GCS.
6. Search and Top-N requests are answered from Redis when available, with PostgreSQL used as the durable fallback path.

## Project Layout

```text
ScholarMiner/
├── lightweight-app/
│   ├── app.py                       # Flask routes, task status, Redis/PostgreSQL reads
│   ├── templates/                   # Indexing, task status, search, and Top-N pages
│   ├── Dockerfile                   # Gunicorn web container
│   └── requirements.txt
├── cluster-app/
│   ├── backend.py                   # Pub/Sub worker, Dataproc jobs, persistence, metrics
│   ├── scraper.py                   # SerpAPI discovery and IEEE abstract retrieval
│   ├── mapreduce/                   # Inverted-index and Top-N mapper/reducer scripts
│   ├── scripts/seed_benchmark_index.py
│   ├── stopwords.txt
│   ├── Dockerfile                   # Standalone worker container
│   └── requirements.txt
├── terraform/
│   ├── main.tf                     # GCP infrastructure and optional image builds
│   ├── variables.tf
│   ├── terraform.tfvars.example
│   └── startup/                    # Web, worker, and observability VM startup templates
├── utils/
│   ├── benchmark_topn.py
│   ├── generate_benchmark_corpus.py
│   └── load_test.py
├── benchmarks/                     # Dated Top-N results and methodology
└── DEPLOYMENT_GUIDE.md              # Deployment, benchmark reproduction, and cleanup
```

Terraform state, provider caches, local variable files, and `.env` files are excluded from Git. The Terraform provider lockfile remains versioned.

## Top-N Benchmark

On September 22, 2026, a deterministic 500-paper synthetic corpus produced 42 indexed terms. Redis Top-10 processing had a median of **0.88 ms** (p95 **1.05 ms**), while full HTTP responses through the public load balancer had a median of **104.106 ms** across 50 queries. The same index's Hadoop Top-N baseline had a median job time of **119.320 s** across three jobs, reused for the public run. These measurements cover pre-indexed queries; the application timer excludes HTML rendering and network latency. See [benchmark records and methodology](benchmarks/README.md) for raw results and validation limits.

## End-to-End Setup

The steps below are enough for a new user to clone the repository, deploy the full system, use the web application, and access the observability stack.

### Prerequisites

- A GCP project with billing enabled
- Google Cloud SDK
- Terraform
- Docker with `buildx` and a container registry you can push to when `build_images = true` (the default)
- A SerpAPI key for Google Scholar scraping

### 1. Clone the Repository

```bash
git clone https://github.com/Davidlasky/ScholarMiner.git
cd ScholarMiner
```

### 2. Authenticate GCP and Docker

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
docker login
```

`terraform apply` uses Application Default Credentials for GCP API calls. With `build_images = true`, it also uses `docker buildx --push` for the web and worker images, so container registry auth must be valid. With `build_images = false`, it deploys existing image tags and skips local Docker builds and pushes.

### 3. Configure Terraform Variables

Copy the example file:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Update `terraform.tfvars` with values you control:

- `project_id`
- `region`
- `zone`
- `serpapi_key`
- `frontend_image`
- `worker_image`
- `data_bucket_force_destroy`
- `build_images` (default `true`; set `false` to use existing image tags)
- `observability_allowed_cidrs` (default `[]`; permits no public Grafana or Prometheus access)

If you change `region`, keep `zone` and `ha_zones` in that region. The regional web and worker instance counts default to two each.

With local image builds enabled, `frontend_image` and `worker_image` must point to image tags that your registry account can push to. With builds disabled, both tags must already exist and include the current application changes. For Docker Hub, a typical example is:

```tfvars
frontend_image = "yourdockerhubusername/scholarminer-web:latest"
worker_image   = "yourdockerhubusername/scholarminer-worker:latest"
data_bucket_force_destroy = true
```

Set `data_bucket_force_destroy = true` for the default demo workflow where you want `terraform destroy` to remove the versioned data bucket automatically. Set it to `false` if you want Terraform to refuse bucket deletion while indexed data or prior object versions still exist.

### 4. Deploy the Full System

```bash
terraform init
terraform apply -auto-approve
```

This step does all of the following:

- Builds and pushes the frontend and worker images when `build_images = true`
- Provisions the regional web tier, worker tier, Dataproc cluster, Cloud SQL, Memorystore, Pub/Sub, GCS, and observability node
- Auto-provisions Grafana datasources and a starter dashboard

Deployment can take several minutes, and the provisioned resources will incur cloud charges until you destroy them.

### 5. Retrieve the URLs and Login Credentials

List all outputs:

```bash
terraform output
```

The most important outputs are:

- `search_engine_url`
- `grafana_dashboard_url`
- `prometheus_url`
- `grafana_admin_password`

You can fetch individual values directly:

```bash
terraform output -raw search_engine_url
terraform output -raw grafana_dashboard_url
terraform output -raw prometheus_url
terraform output -raw grafana_admin_password
```

Grafana login:

- Username: `admin`
- Password: the value from `terraform output -raw grafana_admin_password`

### 6. Run the Application End to End

1. Open `search_engine_url` in your browser.
2. Paste a Google Scholar results URL into the indexing form.
3. Wait for the task status page to move from `PENDING` or `RUNNING` to `COMPLETE`.
4. Use `Search for Term` to query the inverted index.
5. Use `Most Frequent Terms` to view the Top-N term leaderboard.

The intended input is a Google Scholar results page URL, not an IEEE Xplore paper URL directly.

### 7. Use the Observability Stack

After the deployment is up:

Grafana and Prometheus are not publicly accessible by default. To use their output URLs from your browser, set `observability_allowed_cidrs` to your client IP's CIDR (for example, `["YOUR_PUBLIC_IP/32"]`) and apply the configuration, or establish an SSH tunnel to the observability VM.

1. Open `prometheus_url`.
2. Open `Status -> Targets` and confirm Prometheus is scraping:
   - the web service
   - the worker service
   - the node exporters
3. Open `grafana_dashboard_url`.
4. Log in with `admin` and the generated password.
5. Open the auto-provisioned `ScholarMiner Overview` dashboard.

### 8. Redeploy After Code Changes

For Terraform configuration changes, rerun from the repository root:

```bash
cd terraform
terraform apply -auto-approve
```

For application changes, use a new image tag and update the corresponding Terraform variable. Building and pushing an existing tag alone does not refresh containers on running VMs; an existing deployment also needs a managed instance group rollout or instance recreation. When using prebuilt images, ensure the worker includes the Hadoop Streaming JAR-path fix before running fresh indexing.

The current workflow is intentionally ephemeral: apply to stand up the full system, test the changes, then destroy the stack when you are done.

### 9. Destroy the Stack

When you are finished testing:

```bash
cd terraform
terraform destroy -auto-approve
```

If you keep `data_bucket_force_destroy = true`, Terraform will remove the indexed data bucket and its object versions during teardown. If you set it to `false`, empty the bucket manually before destroying the stack.

`google_service_networking_connection.private_service_connection` uses `deletion_policy = "ABANDON"` to avoid a known GCP destroy race where Private Services Access producer subnets can outlive Cloud SQL or Memorystore deletion. This means `terraform destroy` can intentionally leave the PSA connection and allocated range behind if Google has not fully released them yet.

If you are updating an existing deployment that predates this workaround, run the following once before destroying so Terraform records the policy in state without recreating the full stack:

```bash
cd terraform
terraform apply -target=google_service_networking_connection.private_service_connection -auto-approve
```

### Local Frontend Only

Create your own untracked `lightweight-app/.env` for local frontend configuration and pass it with Docker's `--env-file`, or export the variables before starting Python/Gunicorn. The application reads process environment variables. It requires reachable PostgreSQL, Pub/Sub, and the worker service for the indexing workflow; Redis supplies cached reads. A full Terraform deployment injects its configuration through VM startup templates.

### Troubleshooting

- If `terraform apply` fails on `google_service_networking_connection.private_service_connection` with an authentication error, rerun the `gcloud auth login` and `gcloud auth application-default login` commands above, then run `terraform apply` again.
- If Grafana is up but you cannot log in, fetch the password again with `terraform output -raw grafana_admin_password`.
- If the task status page stays in `RUNNING` for too long, check the worker instances and Dataproc cluster in GCP.
- If Prometheus is up but targets are missing, verify that the web and worker managed instance groups are healthy and that the instances are running.

## License

This project is licensed under the GNU Affero General Public License v3. See `LICENSE` for details.

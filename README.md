# Distributed Data Mining & Search Engine for IEEE Xplore

This project implements a distributed data mining and search engine that processes scholarly paper abstracts from Google Scholar and IEEE Xplore. A lightweight web application handles user interaction, while a cloud-based Hadoop cluster performs all heavy computation. Apache Kafka serves as the communication bridge between the two components.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [How Data is Distributed Across Nodes](#how-data-is-distributed-across-nodes)
- [Prerequisites](#prerequisites)
- [Deployment Guide](#deployment-guide)
  - [Step 1: Provision GCP Infrastructure with Terraform](#step-1-provision-gcp-infrastructure-with-terraform)
  - [Step 2: Deploy the Backend to Dataproc](#step-2-deploy-the-backend-to-dataproc)
  - [Step 3: Configure and Run the Lightweight Web App](#step-3-configure-and-run-the-lightweight-web-app)
  - [Step 4: Using the Application](#step-4-using-the-application)
  - [Step 5: Clean Up Resources](#step-5-clean-up-resources)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Citations](#citations)

---

## System Architecture

The system is composed of two independently deployed components that communicate exclusively through Apache Kafka. The lightweight web app performs **no heavy computation** — all scraping, indexing, and search processing happens on the cloud cluster.

```
+------------------+        +---------------------+        +---------------------------+
|  User (Browser)  | <----> | Lightweight Web App |        | Apache Kafka (GCP VM)     |
+------------------+        | (Flask in Docker)   |        +---------------------------+
                             |                     |          |                    ^
                             | - Accepts Scholar   | -------> | Topic:             |
                             |   URL from user     |          | search-requests    |
                             | - Sends requests    |          |                    |
                             |   to Kafka          |          | Topic:             |
                             | - Displays results  | <------- | search-responses   |
                             +---------------------+          |                    |
                                                              v                    |
                                                    +---------------------------+  |
                                                    | Cluster Backend           |  |
                                                    | (Python on Dataproc-m)    |  |
                                                    |                           |  |
                                                    | - Scrapes Scholar/IEEE    |  |
                                                    | - Runs Hadoop M/R jobs    | -+
                                                    | - Sends results to Kafka  |
                                                    +---------------------------+
                                                              |
                                              +--------------+--------------+
                                              |                             |
                                    +------------------+        +------------------+
                                    | Worker Node w-0  |        | Worker Node w-1  |
                                    | (Hadoop Mapper/  |        | (Hadoop Mapper/  |
                                    |  Reducer)        |        |  Reducer)        |
                                    +------------------+        +------------------+
```

---

## How Data is Distributed Across Nodes

This section explains how the Hadoop cluster distributes data and what happens in the event of a node failure.

### Data Distribution

When the backend receives an indexing request, it writes all scraped paper abstracts into a single TSV file and uploads it to **HDFS (Hadoop Distributed File System)**. HDFS automatically splits this file into **128 MB blocks** and distributes those blocks across the two worker nodes (`ieee-search-cluster-w-0` and `ieee-search-cluster-w-1`).

During the **Map phase**, each worker node processes only the blocks stored locally on its own disk. This is called **data locality** — moving computation to the data rather than the other way around. The mapper on each worker reads its local blocks, tokenizes the abstract text, removes stop words, and emits `(word, doc_id:title:citations:url:frequency)` key-value pairs.

During the **Reduce phase**, all intermediate pairs with the same key (i.e., the same word) are shuffled to the same reducer. The reducer aggregates all postings for each word and writes the final inverted index entry. The output is stored back in HDFS and read by the master node to serve search queries.

### Fault Tolerance

HDFS stores **3 replicas** of every data block by default (configurable via `dfs.replication`). If one worker node fails during a MapReduce job, the YARN ResourceManager detects the failure and automatically reschedules the failed map or reduce tasks on the remaining healthy nodes. The data blocks that were on the failed node are re-replicated from the surviving copies to restore the replication factor. This means the system can tolerate the failure of any single worker node without data loss or job failure.

---

## Prerequisites

Before deploying, ensure the following are available:

- A **Google Cloud Platform account** with billing enabled.
- **Docker and Docker Compose** installed on your local machine ([Installation Guide](https://docs.docker.com/get-docker/)).
- Access to **Google Cloud Shell** (no local Terraform installation required — Terraform is pre-installed in Cloud Shell).

> **Only one manual configuration step is required**: After Terraform finishes, you must update the `KAFKA_BROKER` IP address in `lightweight-app/.env` with the external IP printed in the Terraform output. This is unavoidable because the IP is only known after the VM is created.

---

## Deployment Guide

### Step 1: Provision GCP Infrastructure with Terraform

All infrastructure is provisioned automatically using Terraform. No manual GCP Console configuration is needed.

**1.1 Open Google Cloud Shell**

Go to [https://console.cloud.google.com/](https://console.cloud.google.com/) and click the **Activate Cloud Shell** button (`>_`) in the top-right corner.

**1.2 Upload and Extract the Project**

In the Cloud Shell terminal, click the three-dot menu (`⋮`) and select **Upload**. Upload the `ieee-search-engine.zip` file, then run:

```bash
unzip ieee-search-engine.zip
cd ieee-search-engine/terraform
```

**1.3 Initialize and Apply Terraform**

```bash
terraform init
terraform apply -auto-approve
```

This step takes approximately 10–15 minutes. It creates:
- A **Dataproc cluster** (`ieee-search-cluster`) with 1 master node and 2 worker nodes.
- A **Kafka broker VM** (`kafka-broker`) running Apache Kafka with dual listeners.
- A **GCS bucket** for storing MapReduce scripts and data.
- **Firewall rules** for Kafka (ports 9092, 9093) and SSH (IAP range).

**1.4 Record the Outputs**

When complete, Terraform prints output values. Write down the `kafka_external_ip`:

```
Outputs:
kafka_external_ip = "34.XX.XX.XX"
kafka_internal_ip = "10.142.0.X"
gcs_bucket        = "aerial-citron-428307-c5-ieee-search-data"
```

---

### Step 2: Deploy the Backend to Dataproc

**2.1 Open SSH to the Master Node**

In the GCP Console, go to **Compute Engine → VM Instances** and click the **SSH** button next to `ieee-search-cluster-m`.

**2.2 Install Dependencies and Start the Backend**

In the SSH window, run the following block:

```bash
# Install Python dependencies into the Conda environment
/opt/conda/default/bin/pip install kafka-python-ng requests beautifulsoup4

# Verify installation
python3 -c "import kafka; print('Kafka library ready')"

# Kill any old processes
pkill -f backend.py || true

# Start the backend
cd /tmp/cluster-app
export KAFKA_BROKER='10.142.0.X:9093'        # Replace with your kafka_internal_ip
export KAFKA_REQUEST_TOPIC='search-requests'
export KAFKA_RESPONSE_TOPIC='search-responses'
export GCS_BUCKET='aerial-citron-428307-c5-ieee-search-data'

nohup python3 backend.py > /tmp/backend.log 2>&1 &

# Watch the log for success
tail -f /tmp/backend.log
```

Wait until you see:
```
INFO - Connected to Kafka successfully
INFO - Backend is ready and listening for requests...
```

> **Note**: The deployment script (`cluster-app/scripts/deploy_to_cluster.sh`) automates this process if `gcloud compute ssh` is working in your environment. If you encounter SSH errors (return code 255), use the browser-based SSH button as described above.

---

### Step 3: Configure and Run the Lightweight Web App

Switch to your **local machine**.

**3.1 Set the Kafka Broker IP (The Only Manual Step)**

Open `lightweight-app/.env` and update the `KAFKA_BROKER` line with the `kafka_external_ip` from the Terraform output:

```env
KAFKA_BROKER=34.XX.XX.XX:9092
KAFKA_REQUEST_TOPIC=search-requests
KAFKA_RESPONSE_TOPIC=search-responses
KAFKA_TIMEOUT=300
```

**3.2 Build and Run with Docker**

From the project root directory:

```bash
docker-compose up --build
```

The app will be available at **http://localhost:5001**.

---

### Step 4: Using the Application

The application follows a linear workflow:

| Step | Action | What Happens |
|---|---|---|
| 1 | Enter a Google Scholar URL and click **"Index Papers"** | The web app sends the URL to the cluster via Kafka. The cluster scrapes up to 15 pages, fetches IEEE Xplore abstracts, and runs the Inverted Index MapReduce job. |
| 2 | Click **"Search for Term"** | Enter a word. The cluster looks it up in the inverted index and returns matching papers with their citation counts and term frequencies. |
| 3 | Click **"Most Frequent Terms"** | Enter a number N. The cluster runs the Top-N MapReduce job and returns the N most frequent non-stop-word terms across all indexed abstracts. |

> **Tip**: Indexing takes 3–6 minutes because it involves scraping 15 pages, fetching abstracts, and running a Hadoop job. Search and Top-N queries are much faster once the index is built.

---

### Step 5: Clean Up Resources

**Always destroy resources when you are done to avoid ongoing charges (~$1.50–2.00/hour).**

In Cloud Shell:

```bash
cd ~/ieee-search-engine/terraform
terraform destroy -auto-approve
```

On your local machine:

```bash
docker-compose down
```

---

## Project Structure

```
ieee-search-engine/
├── lightweight-app/              # Thin client web application (runs locally in Docker)
│   ├── app.py                    # Flask application — handles UI and Kafka messaging only
│   ├── templates/                # 7 HTML templates matching the project mockup
│   │   ├── base.html
│   │   ├── index.html            # Home page: URL input
│   │   ├── select_action.html    # Action selection after indexing
│   │   ├── search_term.html      # Search term input
│   │   ├── search_results.html   # Search results table (Doc ID, Citations, Name, Frequency)
│   │   ├── topn.html             # Top-N input
│   │   └── topn_results.html     # Top-N results table (Term, Total Frequency, Execution Time)
│   ├── Dockerfile                # Docker container definition (Gunicorn, 600s timeout)
│   ├── requirements.txt
│   └── .env                      # Kafka broker IP (update after Terraform apply)
│
├── cluster-app/                  # Heavy-processing backend (runs on Dataproc master node)
│   ├── backend.py                # Main orchestrator: consumes Kafka, runs jobs, sends results
│   ├── scraper.py                # Google Scholar (15 pages) + IEEE Xplore abstract parser
│   │                             # Includes randomized delays and mock-data fallback for 429/403
│   ├── kafka_utils.py            # Kafka producer/consumer helper functions
│   ├── stopwords.txt             # Stop word list for MapReduce filtering
│   ├── mapreduce/
│   │   ├── inverted_index_mapper.py    # Hadoop Streaming mapper: tokenize + emit word-doc pairs
│   │   ├── inverted_index_reducer.py   # Hadoop Streaming reducer: aggregate postings per word
│   │   ├── topn_mapper.py              # Hadoop Streaming mapper: emit term frequencies
│   │   └── topn_reducer.py             # Hadoop Streaming reducer: sort and select top N
│   └── scripts/
│       ├── setup_backend.sh      # Installs deps via Conda pip, sets up working directory
│       └── deploy_to_cluster.sh  # Copies files to cluster and starts backend
│
├── terraform/                    # Infrastructure as Code (provisions all GCP resources)
│   ├── main.tf                   # Dataproc cluster, Kafka VM, GCS bucket, firewall rules
│   ├── variables.tf              # Variable declarations
│   └── terraform.tfvars          # Variable values (project ID, region, zone)
│
├── docker-compose.yml            # Runs the lightweight app locally
├── .gitignore
├── README.md                     # This file
└── DEPLOYMENT_GUIDE.md           # Detailed troubleshooting guide
```

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'kafka'` on Dataproc**

Dataproc uses a Conda-managed Python at `/opt/conda/default/bin/python3`. Always install packages with:
```bash
/opt/conda/default/bin/pip install kafka-python-ng
```

**`gcloud compute ssh` fails with return code 255**

Use the browser-based SSH button in the GCP Console (Compute Engine → VM Instances) instead. Add the IAP firewall rule if needed:
```bash
gcloud compute firewall-rules create allow-ssh-ingress-from-iap \
    --direction=INGRESS --action=allow --rules=tcp:22 \
    --source-ranges=35.235.240.0/20
```

**Scraper returns 429 or 403 (Google blocking)**

The scraper automatically falls back to sample IEEE paper data when blocked. Wait 15–30 minutes for the block to expire before retrying with a real URL.

**Web app shows `ERR_EMPTY_RESPONSE`**

The Gunicorn timeout was too short. The `Dockerfile` is already configured with `--timeout 600`. Rebuild the Docker image with `docker-compose up --build`.

**Web app shows `Timeout waiting for response from cluster`**

Each request uses a unique Kafka consumer group ID (with timestamp) to avoid missing responses. If this persists, check the backend log (`tail -f /tmp/backend.log`) to confirm the cluster received the request.

---

## Citations

[1] Apache Software Foundation. *Apache Hadoop*. https://hadoop.apache.org/

[2] Apache Software Foundation. *Apache Kafka: A Distributed Streaming Platform*. https://kafka.apache.org/

[3] HashiCorp. *Terraform: Infrastructure as Code*. https://www.terraform.io/

[4] Google Cloud. *Cloud Dataproc Documentation*. https://cloud.google.com/dataproc/docs

[5] Google LLC. *Google Scholar*. https://scholar.google.com/

[6] IEEE. *IEEE Xplore Digital Library*. https://ieeexplore.ieee.org/

[7] Dean, J., & Ghemawat, S. (2008). *MapReduce: Simplified Data Processing on Large Clusters*. Communications of the ACM, 51(1), 107–113. https://dl.acm.org/doi/10.1145/1327452.1327492

[8] Shvachko, K., Kuang, H., Radia, S., & Chansler, R. (2010). *The Hadoop Distributed File System*. 2010 IEEE 26th Symposium on Mass Storage Systems and Technologies (MSST). https://ieeexplore.ieee.org/document/5496972

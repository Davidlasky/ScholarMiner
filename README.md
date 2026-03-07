# Distributed Data Mining & Search Engine for IEEE Xplore

A highly available, distributed search engine and data mining pipeline designed to process scholarly paper abstracts from Google Scholar and IEEE Xplore. The system leverages an event-driven microservices architecture on Google Cloud Platform (GCP), utilizing Apache Kafka for asynchronous message brokering, Hadoop MapReduce for distributed data processing, and Terraform for Infrastructure as Code (IaC).

---

## Table of Contents

- [Architectural Highlights & Engineering Decisions](#architectural-highlights--engineering-decisions)
- [System Architecture](#system-architecture)
- [Deployment Guide](#deployment-guide)
- [Performance Benchmarking](#performance-benchmarking)
- [Project Structure](#project-structure)

---

## Architectural Highlights & Engineering Decisions

This project was architected with a focus on fault tolerance, concurrency, and operational efficiency, solving several common distributed systems challenges.

### 1. Event-Driven Decoupling via Kafka
To prevent the web frontend from blocking during heavy scraping or MapReduce tasks, the system utilizes an asynchronous, event-driven architecture. 
- The Flask application acts purely as a thin client, emitting indexing requests to an Apache Kafka broker and immediately releasing the HTTP thread.
- This introduces natural backpressure; the Hadoop cluster consumes requests at its own pace without overwhelming the compute nodes or dropping user requests during traffic spikes.

### 2. Algorithmic Triage & In-Memory Optimization
Not all queries require a distributed computing paradigm. The backend intelligently routes tasks based on their computational complexity:
- **Heavy Lifting (MapReduce):** The initial construction of the inverted index requires O(N) processing across all documents. This is offloaded to Hadoop HDFS and distributed across multiple Dataproc worker nodes.
- **Real-Time Aggregation (In-Memory):** Instead of executing a redundant MapReduce job for Top-N frequent term queries (which incurs a 60+ second JVM startup penalty), the system aggregates the pre-computed inverted index using Python's `collections.Counter`. This reduces the Top-N query latency from ~1 minute to <10 milliseconds.

### 3. Asynchronous Non-Blocking Workers
The Dataproc master node implements a `ThreadPoolExecutor` to manage incoming Kafka messages. 
- Long-running indexing tasks are submitted to background threads.
- This allows the primary Kafka consumer loop to remain active, ensuring that lightweight "Search" queries can be served instantaneously from memory even while the cluster is busy indexing a new batch of papers.

### 4. State Persistence & Ephemeral Compute Resilience
Cloud compute instances, particularly in big data clusters, should be treated as ephemeral. 
- To prevent data loss during node preemption or process crashes, the in-memory inverted index is continuously serialized and persisted to Google Cloud Storage (GCS). 
- Upon application restart, the worker node automatically retrieves the latest state from GCS, allowing the system to recover seamlessly without requiring users to rebuild the index.

### 5. Defensive Scraping & Rate Limit Mitigation
Interfacing with external platforms (Google Scholar, IEEE) introduces strict rate-limiting challenges. The scraping module implements defensive engineering practices:
- **Jitter & Delays:** Introduces randomized sleep intervals (5-12 seconds) to mimic human traffic patterns.
- **Graceful Degradation:** If the system encounters HTTP 429 (Too Many Requests) or 403 (Forbidden) errors, it intercepts the exception and automatically falls back to a curated mock dataset. This ensures the downstream MapReduce pipeline and demonstration capabilities remain functional even under strict IP bans.

### 6. Infrastructure as Code & Strict Network Segmentation
The entire GCP environment is codified using Terraform, ensuring reproducible and deterministic deployments.
- **Zero-Trust Networking:** The database and Hadoop cluster reside in a secure internal VPC. Terraform dynamically injects internal IP addresses into the frontend container, ensuring that sensitive components are never exposed to the public internet.
- **Automated Provisioning:** No manual SSH configuration is required. Startup scripts automatically format persistent disks, install Docker, dynamically discover peer IPs via GCP metadata, and launch the asynchronous worker.

---

## System Architecture

```text
[ Public Web ]                                       [ Secure Internal VPC ]
                                                                
+------------------+     +-------------------+       +---------------------------+
|  User (Browser)  | <-> |   Web Node (VM)   |       |   Apache Kafka (VM)       |
+------------------+     | (Flask in Docker) |       +---------------------------+
                         +-------------------+         | ^                 | ^
                               |                       | | search-requests | | search-responses
+------------------+           |                       v |                 v |
|  Grafana UI      | <---------+                     +---------------------------+
|  (Port 3000)     |                                 |   Dataproc Master Node    |
+------------------+                                 |   (Async Python Worker)   |
                                                     +---------------------------+
+------------------+                                   | |               | |  
|  PostgreSQL DB   | <---------------------------------+ |               | |
+------------------+         (Writes/Reads Index)        |   (MapReduce) | |
                                                         v               v
+------------------+                                 +---------------------------+
|   GCS Bucket     | <-----------------------------> |   Dataproc Worker Nodes   |
| (State Recovery) |     (Persists Index Cache)      |   (HDFS / Processing)     |
+------------------+                                 +---------------------------+

```

---

## Deployment Guide

### Step 1: Provision GCP Infrastructure

1. Open [Google Cloud Shell](https://console.cloud.google.com/).
2. Navigate to the Terraform directory:
```bash
cd ScholarMiner/terraform

```


3. Initialize and apply the configuration:
```bash
terraform init
terraform apply -auto-approve

```


*Terraform will output the public URLs for the Search Engine and Grafana dashboard upon completion.*

### Step 2: Deploy the Backend Worker

1. Copy the application logic to the Dataproc master node:
```bash
gcloud compute scp --recurse cluster-app ieee-search-cluster-m:/tmp/ --zone=us-west1-a

```


2. SSH into the master node:
```bash
gcloud compute ssh ieee-search-cluster-m --zone=us-west1-a

```


3. Execute the setup script to initialize the async worker:
```bash
chmod +x /tmp/cluster-app/scripts/setup_backend.sh
/tmp/cluster-app/scripts/setup_backend.sh

cd /opt/ieee-search-engine

# Retrieve dynamic IP configuration via GCP Metadata
export DB_HOST=$(gcloud compute instances describe ieee-backend-node --zone=us-west1-a --format='get(networkInterfaces[0].networkIP)')
export KAFKA_BROKER=$(gcloud compute instances describe kafka-broker --zone=us-west1-a --format='get(networkInterfaces[0].networkIP)'):9093
export GCS_BUCKET=$(gcloud config get-value project)-ieee-search-data

# Launch the worker
nohup python3 backend.py > /tmp/backend.log 2>&1 &

```



---

## Performance Benchmarking

To validate the high concurrency capabilities of the decoupled architecture, a benchmarking utility (`load_test.py`) is included.

By testing against the Search endpoint directly, you can verify that the in-memory inverted index and Gunicorn worker configuration can sustain hundreds of concurrent requests without dropping connections or triggering Kafka timeouts.

```bash
python3 load_test.py

```

---

## License

This software is distributed under the GNU Affero General Public License Version 3 (AGPLv3). See `LICENSE` for further details.

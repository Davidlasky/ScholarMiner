# Distributed Data Mining & Search Engine for IEEE Xplore

A highly available, distributed search engine and data mining pipeline designed to process scholarly paper abstracts from Google Scholar and IEEE Xplore. The system leverages an event-driven microservices architecture on Google Cloud Platform (GCP), utilizing Apache Kafka for asynchronous message brokering, Hadoop MapReduce for distributed data processing, Redis for microsecond-latency caching, and Terraform for complete Infrastructure as Code (IaC) automation.

---

## Table of Contents

- [Architectural Highlights & Engineering Decisions](#architectural-highlights--engineering-decisions)
- [System Architecture](#system-architecture)
- [Deployment Guide](#deployment-guide)
- [Performance Benchmarking](#performance-benchmarking)
- [Project Structure](#project-structure)

---

## Architectural Highlights & Engineering Decisions

This project was architected with a focus on fault tolerance, concurrency, and operational efficiency, solving multiple distributed systems challenges.

### 1. CQRS Architecture with Redis Caching
To achieve industrial-grade scalability, the system separates write and read concerns (Command Query Responsibility Segregation).
- **Reads (Queries):** The lightweight Flask frontend (running on Gunicorn) directly queries a Redis instance for instantaneous (`< 1ms`) O(1) cache reads. 
- **Writes (Commands):** Heavy scraping and MapReduce indexing tasks are dispatched asynchronously to the Hadoop cluster via Apache Kafka, preventing HTTP timeout bottlenecks.

### 2. Accumulative Hadoop Indexing
Unlike naive systems that overwrite data, this engine features an intelligent **Accumulative Inverted Index** mapped over HDFS. Every new URL scraped deposits a new TSV shard into the HDFS input directory. Subsequent Hadoop MapReduce jobs natively process the entire corpus, creating an ever-expanding global search tree that aggregates all knowledge discovered across distributed scraping sessions.

### 3. Distributed URL Cache Registry
To prevent redundant API consumption and compute waste, the Dataproc backend maintains a distributed URL lock in Redis (`scraped_urls`). Duplicate indexing requests instantly bypass the Scraping and MapReduce stages and return success, saving massive compute resources.

### 4. Algorithmic Triage & In-Memory Optimization
- **Heavy Lifting (MapReduce):** The O(N) construction of the inverted index across all papers is offloaded to distributed Dataproc worker nodes.
- **Top-N Real-Time Slicing:** Instead of redundant Big Data processing for leaderboard queries, the Hadoop Reducer directly pushes term frequencies into a **Redis Sorted Set (`ZSET`)**. Complex Top-N slicing occurs in O(1) microsecond latency via `ZREVRANGE`.

### 5. Seamless Infrastructure Automations
The entire GCP compute layer and backend logic deployment are completely decoupled from manual interaction. 
- Using **Terraform**, startup scripts and initialization hooks dynamically provision the infrastructure. 
- The Dataproc Master node effortlessly constructs its Python environment, resolves metadata IP bindings, retrieves the latest `.py` artifacts from GCS buckets, and boots the async Kafka worker upon creation. No SSH required.
- The Frontend Docker image is automatically built (`buildx`) and cleanly pushed to Docker Hub via Terraform `local-exec` provisioning.

---

## System Architecture

```text
[ Public Web ]                                         [ Secure Internal VPC ]
                                                                
+------------------+     +-------------------+       +---------------------------+
|  User (Browser)  | <-> |   Web Node (VM)   | ----> |      Redis (Cache)        |
+------------------+     | (Gunicorn Flask)  |       |  (Microsecond Queries)    |
                         +-------------------+       +---------------------------+
                               |                       ^           ^
+------------------+       (Kafka Pub/Sub)             |           |
|  Grafana UI      | <---------|-----------------------|-----------+
|  (Port 3000)     |           v                       |
+------------------+     +-------------------+         |
                         |   Apache Kafka    |         |
                         +-------------------+         |
                               |    ^                  | (Persists Output)
+------------------+           v    |                  |
|  PostgreSQL DB   | <---+-------------------+         |
+------------------+     |  Dataproc Master  | --------+
                         |  (Async Worker)   |
+------------------+     +-------------------+
|   GCS Bucket     | <---|         |         |
| (State Recovery) |     +---------v---------+
+------------------+     | Dataproc Workers  |
                         |     (HDFS)        |
                         +-------------------+
```

---

## Deployment Guide

### Provision GCP Infrastructure

1. Rename the credentials file and insert your API Key:
```bash
cd ScholarMiner/terraform
cp terraform.tfvars.example terraform.tfvars
```
*(Open `terraform.tfvars` and paste your actual `serpapi_key`)*

2. Initialize and deploy via Terraform:
```bash
terraform init
terraform apply -auto-approve
```

*That's it! Terraform completely automates the Hadoop data pipeline, Postgres DB, Redis deployment, Docker image pushes, and cluster setup. At the end of the deployment, Terraform will output the public URLs for the Search Engine and Grafana dashboard.*

---

## Performance Benchmarking

The Frontend runs using **Gunicorn parallel workers**, empowering the microservices to handle an enormous volume of traffic. Because Search and Top-N queries directly hit Redis bypassing the Hadoop clusters, the system effectively acts as a High Availability (HA) node capable of sustaining tens of thousands of concurrent reads.

```bash
python3 load_test.py
```

---

## License

This software is distributed under the GNU Affero General Public License Version 3 (AGPLv3). See `LICENSE` for further details.

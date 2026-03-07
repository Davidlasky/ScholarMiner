# Hands-On Deployment Guide

This guide walks you through every step of deploying the IEEE Search Engine project on Google Cloud Platform. It includes exact commands, expected outputs, and solutions to common problems we encountered.

## Overview of What Gets Created

When you run Terraform, it provisions the following resources on GCP:

| Resource | Type | Purpose |
|---|---|---|
| `ieee-search-cluster` | Dataproc Cluster (1 master + 2 workers, e2-standard-2) | Runs Hadoop MapReduce jobs |
| `kafka-broker` | Compute Engine VM (e2-medium) | Hosts Apache Kafka for messaging |
| `{project-id}-ieee-search-data` | Cloud Storage Bucket | Stores MapReduce scripts and data |
| `allow-kafka` | Firewall Rule | Opens ports 9092, 9093, and 2181 for Kafka |
| `allow-internal-all` | Firewall Rule | Allows internal cluster communication |
| `allow-ssh-ingress-from-iap` | Firewall Rule | Allows SSH access from Cloud Shell |

**Estimated cost**: approximately $1.50-2.00/hour while all resources are running. **Always destroy resources when you are done testing.**

---

## Part 1: Preparation (5 minutes)

### 1.1 Verify APIs Are Enabled

The following APIs have already been enabled on your project:

- Cloud Dataproc API
- Compute Engine API
- Cloud Storage API

### 1.2 Install Prerequisites on Your Local Machine

You need the following tools installed locally. If you already have them, skip ahead.

**Google Cloud SDK:**
```bash
# macOS
brew install --cask google-cloud-sdk

# Linux
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```

**Terraform:**
```bash
# macOS
brew tap hashicorp/tap
brew install hashicorp/tap/terraform

# Linux
sudo apt-get update && sudo apt-get install -y gnupg software-properties-common
wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor | sudo tee /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
```

**Docker:**
```bash
# macOS
brew install --cask docker

# Linux
sudo apt-get update
sudo apt-get install docker.io docker-compose -y
```

### 1.3 Authenticate with GCP

```bash
gcloud auth login
gcloud config set project aerial-citron-428307-c5
gcloud auth application-default login
```

---

## Part 2: Deploy Infrastructure with Terraform (15-20 minutes)

You can run Terraform either from **Cloud Shell** (recommended, since it's pre-authenticated) or from your **local machine**.

### Option A: Using Google Cloud Shell (Recommended)

1. Go to https://console.cloud.google.com/ and click the **Activate Cloud Shell** button (`>_` icon) in the top bar.

2. Upload the project zip file:
   - Click the three-dot menu (`⋮`) in the Cloud Shell toolbar.
   - Select **Upload** and choose the `ieee-search-engine.zip` file.

3. Run these commands in Cloud Shell:
```bash
# Unzip and navigate
mkdir -p ieee-search-engine && cd ieee-search-engine
unzip -o ~/ieee-search-engine.zip
cd terraform

# Initialize Terraform
terraform init
```

**Expected output:**
```
Initializing the backend...
Initializing provider plugins...
- Finding hashicorp/google versions matching "~> 5.0"...
- Installing hashicorp/google v5.x.x...
Terraform has been successfully initialized!
```

4. Plan and apply:
```bash
terraform plan
terraform apply -auto-approve
```

**This will take 10-15 minutes.** The Dataproc cluster creation is the slowest part. You will see progress messages like:
```
google_compute_instance.kafka_vm: Creating...
google_compute_instance.kafka_vm: Still creating... [10s elapsed]
google_compute_instance.kafka_vm: Creation complete after 15s
google_dataproc_cluster.hadoop_cluster: Creating...
google_dataproc_cluster.hadoop_cluster: Still creating... [2m0s elapsed]
...
```

5. When complete, you will see outputs like:
```
Apply complete! Resources: 8 added, 0 changed, 0 destroyed.

Outputs:

dataproc_cluster_name = "ieee-search-cluster"
dataproc_master_instance = ["ieee-search-cluster-m"]
gcs_bucket = "aerial-citron-428307-c5-ieee-search-data"
kafka_external_ip = "34.138.XX.XX"
kafka_internal_ip = "10.128.0.XX"
```

**Write down the `kafka_external_ip` and `kafka_internal_ip` values.** You will need them.

### Option B: Using Your Local Machine

If you prefer to run Terraform locally, make sure you have authenticated with `gcloud auth application-default login`, then follow the same `terraform init`, `plan`, and `apply` commands from the `terraform/` directory.

---

## Part 3: Deploy the Backend to Dataproc (5 minutes)

After Terraform finishes, deploy the backend application to the Dataproc master node.

### 3.1 Open SSH to the Master Node

Go to **Compute Engine -> VM Instances** in the GCP Console and click the **SSH** button next to `ieee-search-cluster-m`.

### 3.2 Install Dependencies

In the SSH window, run:
```bash
/opt/conda/default/bin/pip install kafka-python-ng requests beautifulsoup4
```

### 3.3 Start the Backend

In the same SSH window, run:
```bash
# Kill any old processes
pkill -f backend.py || true

# Navigate and set variables
cd /tmp/cluster-app
export KAFKA_BROKER=\'10.142.0.2:9093\'
export KAFKA_REQUEST_TOPIC=\'search-requests\'
export KAFKA_RESPONSE_TOPIC=\'search-responses\'
export GCS_BUCKET=\'aerial-citron-428307-c5-ieee-search-data\'

# Start the backend in the background
nohup python3 backend.py > /tmp/backend.log 2>&1 &

# Watch the log for success
tail -f /tmp/backend.log
```

### 3.4 Verify the Backend Is Running

You should see something like:
```
INFO:__main__:Connected to Kafka successfully
INFO:__main__:Backend is ready and listening for requests...
```

---

## Part 4: Run the Lightweight Web App (5 minutes)

Now switch to your **local machine**.

### 4.1 Configure the Kafka Broker

Edit the file `lightweight-app/.env` and set the `KAFKA_BROKER` to the **external** IP of the Kafka VM:

```
KAFKA_BROKER=34.138.XX.XX:9092
```

### 4.2 Build and Run with Docker Compose

From the project root directory:
```bash
docker-compose up --build
```

You should see:
```
Building lightweight-app
...
Successfully built abc123
Creating ieee-search-engine-lightweight-app-1 ... done
Attaching to ieee-search-engine-lightweight-app-1
lightweight-app-1  |  * Running on all addresses (0.0.0.0)
lightweight-app-1  |  * Running on http://127.0.0.1:5000
```

### 4.3 Access the Application

Open your browser and go to: **http://localhost:5001**

You should see the IEEE Search Engine home page with a text input for a Google Scholar URL.

---

## Part 5: Using the Application

### 5.1 Index Papers

1. Go to Google Scholar (https://scholar.google.com/) and search for a topic (e.g., "hadoop mapreduce").
2. Copy the URL from your browser's address bar.
3. Paste it into the input field on the IEEE Search Engine home page.
4. Click **"Index Papers from this URL"**.
5. Wait for the system to scrape, parse, and build the inverted index. This can take several minutes depending on how many IEEE papers are found.

### 5.2 Search for a Term

1. After indexing is complete, click **"Search for Term"**.
2. Enter a term (e.g., "algorithm") and click **"Search"**.
3. You will see a table of papers containing that term, along with the frequency of the term in each paper's abstract.

### 5.3 Find Most Frequent Terms

1. From the action selection page, click **"Most Frequent Terms"**.
2. Enter a number N (e.g., 10) and click **"Get Top Terms"**.
3. You will see the N most frequent terms across all indexed abstracts.

---

## Part 6: Clean Up Resources

**Do this as soon as you are done testing to avoid charges.**

### 6.1 Stop the Docker Container

In your local terminal where Docker Compose is running, press `Ctrl+C`, then:
```bash
docker-compose down
```

### 6.2 Destroy GCP Resources

In Cloud Shell (or your local terminal):
```bash
cd ~/ieee-search-engine/terraform
terraform destroy -auto-approve
```

This will delete all GCP resources (Dataproc cluster, Kafka VM, GCS bucket, firewall rules). It takes about 5 minutes.

**Expected output:**
```
Destroy complete! Resources: 8 destroyed.
```

---

## Common Issues and Solutions

### Issue: Terraform fails with "API not enabled"

**Solution:** Enable the required APIs manually in the GCP Console:
- Go to https://console.cloud.google.com/apis/library
- Search for and enable: "Cloud Dataproc API", "Compute Engine API", "Cloud Storage API"

### Issue: `gcloud compute ssh` fails with `return code [255]`

**Solution:** Use the browser-based SSH button in the GCP Console (Compute Engine -> VM Instances) instead of the command line. This bypasses IAP tunneling issues.

### Issue: `ModuleNotFoundError: No module named 'kafka'` on Dataproc

**Solution:** The Dataproc cluster uses a Conda environment. You need to install the library there:
```bash
/opt/conda/default/bin/pip install kafka-python-ng requests beautifulsoup4
```

### Issue: Backend can't connect to Kafka (`ECONNREFUSED`)

**Solution:** The Kafka VM's startup script may have failed. SSH into the `kafka-broker` VM and run the installation script from the `DEPLOYMENT_GUIDE.md` manually to ensure Kafka is running with the correct dual-listener configuration.

### Issue: Scraper fails with `429 Too Many Requests`

**Solution:** Google has temporarily blocked your IP. The scraper has a built-in "mock mode" that will activate, allowing you to continue testing with sample data. Wait 15-30 minutes for the block to expire.

### Issue: Web app shows `ERR_EMPTY_RESPONSE`

**Solution:** The Gunicorn server in the Docker container timed out. The `Dockerfile` has been updated with a 10-minute timeout, which should be sufficient.

### Issue: Web app shows `Timeout waiting for response from cluster`

**Solution:** The Kafka consumer group ID in `app.py` was not unique enough. The code has been updated to use a timestamp, ensuring a fresh consumer for every request.

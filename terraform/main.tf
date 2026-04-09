###############################################################################
# Terraform Configuration for IEEE Search Engine - GCP Deployment
# Provisions: Dataproc Cluster (1 master + 2 workers), GCS Bucket, Kafka on GCE
###############################################################################

terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Define a dedicated temp bucket in Terraform
resource "google_storage_bucket" "temp_bucket" {
  name          = "${var.project_id}-dataproc-temp"
  location      = var.region
  force_destroy = true
}

###############################################################################
# Enable Required APIs
###############################################################################

resource "google_project_service" "dataproc" {
  service            = "dataproc.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_disk" "postgres_data_disk" {
  name = "postgres-data-disk"
  type = "pd-ssd"
  zone = var.zone
  size = 20
}

# Backend Node: Database & Monitoring
resource "google_compute_instance" "backend_node" {
  name         = "ieee-backend-node"
  machine_type = "e2-medium"
  zone         = var.zone
  tags         = ["backend-secure"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-11"
    }
  }

  attached_disk {
    source      = google_compute_disk.postgres_data_disk.id
    device_name = "postgres-data-disk"
  }

  network_interface {
    network = "default"
    access_config {}
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -e
    echo "Initializing Secure Backend Node..."

    # Format and mount persistent disk
    DISK_ID="/dev/disk/by-id/google-postgres-data-disk"
    MOUNT_DIR="/mnt/stateful_partition"
    
    while [ ! -e $DISK_ID ]; do sleep 1; done
    if ! blkid $DISK_ID; then mkfs.ext4 -m 0 -F -E lazy_itable_init=0,lazy_journal_init=0,discard $DISK_ID; fi
    
    mkdir -p $MOUNT_DIR
    mount -o discard,defaults $DISK_ID $MOUNT_DIR
    export PG_DATA_DIR="$MOUNT_DIR/pgdata"
    mkdir -p $PG_DATA_DIR
    # Install Docker
    apt-get update && apt-get install -y docker.io docker-compose

    # Setup Observability & DB config
    mkdir -p /opt/backend
    cd /opt/backend

cat << 'PROMETHEUS' > prometheus.yml
global:
  scrape_interval: 5s
scrape_configs:
  - job_name: 'flask_app'
    static_configs:
      - targets: ['ieee-web-node:5000'] 
  - job_name: 'node_exporter'
    static_configs:
      - targets: ['node-exporter:9100']
PROMETHEUS

# Changed to 3.3 for Debian 11 compatibility
cat << 'COMPOSE' > docker-compose.yml
version: '3.3'
services:
  postgres:
    image: postgres:15-alpine
    container_name: postgres_db
    networks:
      - search_net
    restart: always
    environment:
      POSTGRES_USER: admin
      POSTGRES_PASSWORD: supersecretpassword
      POSTGRES_DB: ieee_search
    ports:
      - "5432:5432"
    volumes:
      - $PG_DATA_DIR:/var/lib/postgresql/data
  prometheus:
    image: prom/prometheus
    restart: always
    ports:
      - "9090:9090"
    networks:
      - search_net
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
  redis:
    image: redis:7-alpine
    container_name: redis_db
    restart: always
    ports:
      - "6379:6379"
    networks:
      - search_net
  grafana:
    image: grafana/grafana
    restart: always
    ports:
      - "3000:3000"
    depends_on:
      - prometheus
      - postgres
    networks:
      - search_net
  node-exporter:
    image: prom/node-exporter:latest
    container_name: node_exporter
    restart: always
    ports:
      - "9100:9100"
    networks:
      - search_net

networks:
  search_net:
    driver: bridge
COMPOSE

    docker-compose up -d

    # Auto-initialize the PostgreSQL table so the Web Node doesn't crash on boot
    until docker exec postgres_db pg_isready -U admin -d ieee_search; do
      sleep 5
    done

    docker exec postgres_db psql -U admin -d ieee_search -c "
    CREATE TABLE IF NOT EXISTS inverted_index (
        term TEXT PRIMARY KEY,
        doc_ids TEXT,
        count INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );"
  EOT
}

# Web Node: Flask Frontend
resource "google_compute_instance" "web_node" {
  name         = "ieee-web-node"
  machine_type = "e2-micro"
  zone         = var.zone
  tags         = ["web-public"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    apt-get update && apt-get install -y docker.io
    
    # Inject IPs directly from Terraform resources
    docker run -d --name lightweight-app --restart always -p 5000:5000 \
      -e DB_URL="postgresql://admin:supersecretpassword@${google_compute_instance.backend_node.network_interface.0.network_ip}:5432/ieee_search" \
      -e KAFKA_BROKER="${google_compute_instance.kafka_vm.network_interface.0.network_ip}:9093" \
      -e REDIS_HOST="${google_compute_instance.backend_node.network_interface.0.network_ip}" \
      -e REDIS_PORT="6379" \
      ${var.frontend_image}
  EOT

  depends_on = [
    null_resource.frontend_image_build_push,
  ]
}

# =====================================================================
# 4. STRICT FIREWALL RULES (Network Segmentation)
# =====================================================================

# 4.1 Allow public access to Flask Web UI (Port 5000)
resource "google_compute_firewall" "allow_web_public" {
  name    = "allow-web-public"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["5000"]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["web-public"]
}

# 4.2 Allow public access to Grafana Dashboard (Port 3000)
resource "google_compute_firewall" "allow_grafana_public" {
  name    = "allow-grafana-public"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["3000"]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["backend-secure"]
}

# 4.3 STRICT INTERNAL RULE: Only VPC instances can access Postgres & Prometheus
resource "google_compute_firewall" "allow_backend_internal" {
  name    = "allow-backend-internal"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["5432", "9090", "6379"]
  }
  source_ranges = ["10.0.0.0/8"] # VPC Internal IP range only
  target_tags   = ["backend-secure"]
}

# =====================================================================
# 5. OUTPUTS
# =====================================================================
output "search_engine_url" {
  description = "Public URL to access the Flask Search Engine"
  value       = "http://${google_compute_instance.web_node.network_interface.0.access_config.0.nat_ip}:5000"
}

output "grafana_dashboard_url" {
  description = "Public URL to access the Grafana Observability Dashboard"
  value       = "http://${google_compute_instance.backend_node.network_interface.0.access_config.0.nat_ip}:3000"
}


###############################################################################
# GCS Bucket for storing data and MapReduce scripts
###############################################################################

resource "google_storage_bucket" "data_bucket" {
  name          = "${var.project_id}-ieee-search-data"
  location      = var.region
  force_destroy = true

  uniform_bucket_level_access = true
}

resource "google_storage_bucket_object" "dataproc_init_script" {
  name    = "scripts/init_backend.sh"
  content = <<-EOF
    #!/bin/bash
    set -euo pipefail

    ROLE=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/dataproc-role" -H "Metadata-Flavor: Google")
    BUCKET=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/gcs-bucket" -H "Metadata-Flavor: Google")
    KAFKA_IP=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/kafka-broker" -H "Metadata-Flavor: Google")
    REDIS_IP=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-ip" -H "Metadata-Flavor: Google")
    SERPAPI_KEY=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/serpapi-key" -H "Metadata-Flavor: Google")

    # Install dependencies for Scrapling
    apt-get update
    apt-get install -y python3-pip wget libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libgbm1 libasound2 libpangocairo-1.0-0 libxss1 libgtk-3-0

    if [[ "$ROLE" == "Master" ]]; then
      echo "Setting up backend on master node..."

      mkdir -p /opt/backend/mapreduce

      for attempt in {1..5}; do
        if gsutil cp gs://$BUCKET/app/backend.py /opt/backend/ \
          && gsutil cp gs://$BUCKET/app/scraper.py /opt/backend/ \
          && gsutil cp gs://$BUCKET/app/kafka_utils.py /opt/backend/ \
          && gsutil cp gs://$BUCKET/app/requirements.txt /opt/backend/ \
          && gsutil cp gs://$BUCKET/app/stopwords.txt /opt/backend/ \
          && gsutil cp gs://$BUCKET/mapreduce/*.py /opt/backend/mapreduce/; then
          break
        fi
        echo "Backend artifacts not ready yet, retrying in 5 seconds..."
        sleep 5
      done

      pip3 install -r /opt/backend/requirements.txt
      playwright install chromium || true

      HADOOP_STREAMING_JAR=$(find /usr/lib -name 'hadoop-streaming*.jar' 2>/dev/null | head -1)
      if [[ -z "$HADOOP_STREAMING_JAR" ]]; then
        HADOOP_STREAMING_JAR=/usr/lib/hadoop/hadoop-streaming.jar
      fi

      cat > /opt/backend/.env << ENV
REDIS_IP=$REDIS_IP
KAFKA_BROKER=$KAFKA_IP:9093
GCS_BUCKET=$BUCKET
HADOOP_STREAMING_JAR=$HADOOP_STREAMING_JAR
SERPAPI_KEY=$SERPAPI_KEY
ENV

      set -a
      source /opt/backend/.env
      set +a

      cd /opt/backend
      nohup python3 backend.py >> /var/log/backend.log 2>&1 &
      echo "Backend started."
    fi
  EOF
  bucket  = google_storage_bucket.data_bucket.name
}

# Backend application files
resource "google_storage_bucket_object" "backend_py" {
  name   = "app/backend.py"
  source = "${path.module}/../cluster-app/backend.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "scraper_py" {
  name   = "app/scraper.py"
  source = "${path.module}/../cluster-app/scraper.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "kafka_utils_py" {
  name   = "app/kafka_utils.py"
  source = "${path.module}/../cluster-app/kafka_utils.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "requirements_txt" {
  name   = "app/requirements.txt"
  source = "${path.module}/../cluster-app/requirements.txt"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "stopwords_app" {
  name   = "app/stopwords.txt"
  source = "${path.module}/../cluster-app/stopwords.txt"
  bucket = google_storage_bucket.data_bucket.name
}

# Upload MapReduce scripts to GCS
resource "google_storage_bucket_object" "inverted_index_mapper" {
  name   = "mapreduce/inverted_index_mapper.py"
  source = "${path.module}/../cluster-app/mapreduce/inverted_index_mapper.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "inverted_index_reducer" {
  name   = "mapreduce/inverted_index_reducer.py"
  source = "${path.module}/../cluster-app/mapreduce/inverted_index_reducer.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "topn_mapper" {
  name   = "mapreduce/topn_mapper.py"
  source = "${path.module}/../cluster-app/mapreduce/topn_mapper.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "topn_reducer" {
  name   = "mapreduce/topn_reducer.py"
  source = "${path.module}/../cluster-app/mapreduce/topn_reducer.py"
  bucket = google_storage_bucket.data_bucket.name
}

resource "google_storage_bucket_object" "stopwords" {
  name   = "data/stopwords.txt"
  source = "${path.module}/../cluster-app/stopwords.txt"
  bucket = google_storage_bucket.data_bucket.name
}

###############################################################################
# Firewall Rules
###############################################################################

resource "google_compute_firewall" "allow_kafka" {
  name    = "allow-kafka"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["9092", "9093", "2181"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["kafka"]
}

resource "google_compute_firewall" "allow_internal" {
  name    = "allow-internal-all"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "icmp"
  }

  source_ranges = ["10.0.0.0/8"]
}

###############################################################################
# Kafka VM Instance (deployed via Terraform, NOT on local machine)
###############################################################################

resource "google_compute_instance" "kafka_vm" {
  name         = "kafka-broker"
  machine_type = "e2-medium"
  zone         = var.zone
  tags         = ["kafka"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
    }
  }

  network_interface {
    network = "default"
    access_config {
      // Ephemeral public IP
    }
  }

  metadata_startup_script = <<-SCRIPT
    #!/bin/bash
    set -e

    # Install Java and Wget
    sudo apt-get update && sudo apt-get install -y default-jdk wget

    # Download Kafka (use archive URL for stability)
    cd /tmp
    wget https://archive.apache.org/dist/kafka/3.6.1/kafka_2.13-3.6.1.tgz
    sudo tar -xzf kafka_2.13-3.6.1.tgz
    sudo mv kafka_2.13-3.6.1 /opt/kafka

    # Get IPs for configuration
    INTERNAL_IP=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip" -H "Metadata-Flavor: Google")
    EXTERNAL_IP=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" -H "Metadata-Flavor: Google")

    # Create the Dual-Listener Configuration
    sudo tee /opt/kafka/config/server.properties > /dev/null << EOF
    broker.id=0
    listeners=INTERNAL://0.0.0.0:9093,EXTERNAL://0.0.0.0:9092
    advertised.listeners=INTERNAL://$${INTERNAL_IP}:9093,EXTERNAL://$${EXTERNAL_IP}:9092
    listener.security.protocol.map=INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT
    inter.broker.listener.name=INTERNAL
    num.network.threads=3
    num.io.threads=8
    socket.send.buffer.bytes=102400
    socket.receive.buffer.bytes=102400
    socket.request.max.bytes=104857600
    log.dirs=/tmp/kafka-logs
    num.partitions=3
    num.recovery.threads.per.data.dir=1
    offsets.topic.replication.factor=1
    transaction.state.log.replication.factor=1
    transaction.state.log.min.isr=1
    log.retention.hours=168
    zookeeper.connect=localhost:2181
    zookeeper.connection.timeout.ms=18000
    auto.create.topics.enable=true
    EOF

    # Start Zookeeper in the background
    sudo /opt/kafka/bin/zookeeper-server-start.sh -daemon /opt/kafka/config/zookeeper.properties
    sleep 5

    # Start Kafka in the background
    sudo /opt/kafka/bin/kafka-server-start.sh -daemon /opt/kafka/config/server.properties
    sleep 5

    # Create topics
    /opt/kafka/bin/kafka-topics.sh --create --topic search-requests --bootstrap-server localhost:9092 --partitions 3 --replication-factor 1 || true
    /opt/kafka/bin/kafka-topics.sh --create --topic search-responses --bootstrap-server localhost:9092 --partitions 3 --replication-factor 1 || true

    echo "Kafka setup complete. Internal IP: $${INTERNAL_IP}, External IP: $${EXTERNAL_IP}"
  SCRIPT

  depends_on = [
    google_project_service.compute,
  ]
}

###############################################################################
# Dataproc Cluster (1 Master + 2 Workers)
###############################################################################

resource "google_dataproc_cluster" "hadoop_cluster" {
  name   = "ieee-search-cluster"
  region = var.region

  cluster_config {
    staging_bucket = google_storage_bucket.data_bucket.name
    temp_bucket    = google_storage_bucket.temp_bucket.name

    initialization_action {
      script      = "gs://${google_storage_bucket.data_bucket.name}/${google_storage_bucket_object.dataproc_init_script.name}"
      timeout_sec = 600
    }

    master_config {
      num_instances = 1
      machine_type  = "e2-standard-2"

      disk_config {
        boot_disk_type    = "pd-standard"
        boot_disk_size_gb = 50
      }
    }

    worker_config {
      num_instances = 2
      machine_type  = "e2-standard-2"

      disk_config {
        boot_disk_type    = "pd-standard"
        boot_disk_size_gb = 50
      }
    }

    software_config {
      image_version = "2.1-debian11"
      override_properties = {
        "dataproc:dataproc.allow.zero.workers" = "false"
      }
    }

    gce_cluster_config {
      zone = var.zone
      metadata = {
        "kafka-broker" = google_compute_instance.kafka_vm.network_interface[0].network_ip
        "gcs-bucket"   = google_storage_bucket.data_bucket.name
        "postgres-ip"  = google_compute_instance.backend_node.network_interface.0.network_ip
        "redis-ip"     = google_compute_instance.backend_node.network_interface.0.network_ip
        "serpapi-key"  = var.serpapi_key
      }
    }
  }

  depends_on = [
    google_project_service.dataproc,
    google_compute_instance.kafka_vm,
    google_storage_bucket_object.dataproc_init_script,
    google_storage_bucket_object.backend_py,
    google_storage_bucket_object.scraper_py,
    google_storage_bucket_object.kafka_utils_py,
    google_storage_bucket_object.requirements_txt,
    google_storage_bucket_object.stopwords_app,
  ]
}

# Frontend Application Docker Build & Push
resource "null_resource" "frontend_image_build_push" {
  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/../lightweight-app"
    command     = "docker buildx build --platform linux/amd64 -t ${var.frontend_image} --push ."
  }
}

###############################################################################
# Outputs
###############################################################################

output "dataproc_cluster_name" {
  value = google_dataproc_cluster.hadoop_cluster.name
}

output "dataproc_master_instance" {
  value = google_dataproc_cluster.hadoop_cluster.cluster_config[0].master_config[0].instance_names
}

output "kafka_internal_ip" {
  value = google_compute_instance.kafka_vm.network_interface[0].network_ip
}

output "kafka_external_ip" {
  value = google_compute_instance.kafka_vm.network_interface[0].access_config[0].nat_ip
}

output "gcs_bucket" {
  value = google_storage_bucket.data_bucket.name
}

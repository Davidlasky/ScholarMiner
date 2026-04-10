terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

data "google_project" "current" {
  project_id = var.project_id
}

data "google_compute_network" "default" {
  name = "default"
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "dataproc" {
  service            = "dataproc.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "sqladmin" {
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "redis" {
  service            = "redis.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "pubsub" {
  service            = "pubsub.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "servicenetworking" {
  service            = "servicenetworking.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_global_address" "private_service_range" {
  name          = "scholarminer-private-service-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = data.google_compute_network.default.self_link
}

resource "google_service_networking_connection" "private_service_connection" {
  network                 = data.google_compute_network.default.self_link
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]
  deletion_policy         = "ABANDON"

  depends_on = [
    google_project_service.servicenetworking,
  ]
}

resource "random_password" "db_password" {
  length  = 32
  special = false
}

resource "random_password" "flask_secret" {
  length  = 48
  special = false
}

resource "random_password" "grafana_admin_password" {
  length  = 32
  special = false
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "scholarminer-db-password"
  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager,
  ]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

resource "google_secret_manager_secret" "flask_secret" {
  secret_id = "scholarminer-flask-secret"
  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager,
  ]
}

resource "google_secret_manager_secret_version" "flask_secret" {
  secret      = google_secret_manager_secret.flask_secret.id
  secret_data = random_password.flask_secret.result
}

resource "google_secret_manager_secret" "serpapi_key" {
  secret_id = "scholarminer-serpapi-key"
  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager,
  ]
}

resource "google_secret_manager_secret_version" "serpapi_key" {
  secret      = google_secret_manager_secret.serpapi_key.id
  secret_data = var.serpapi_key
}

resource "google_storage_bucket" "temp_bucket" {
  name          = "${var.project_id}-dataproc-temp"
  location      = var.region
  force_destroy = true
}

resource "google_storage_bucket" "data_bucket" {
  name          = "${var.project_id}-scholarminer-data"
  location      = var.region
  force_destroy = var.data_bucket_force_destroy

  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }
}

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

resource "google_sql_database_instance" "postgres" {
  name                = "scholarminer-postgres"
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = false

  settings {
    tier              = "db-custom-2-7680"
    availability_type = "REGIONAL"
    disk_type         = "PD_SSD"
    disk_size         = 20

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = data.google_compute_network.default.self_link
    }
  }

  depends_on = [
    google_project_service.sqladmin,
    google_service_networking_connection.private_service_connection,
  ]
}

resource "google_sql_database" "application" {
  name     = "ieee_search"
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "application" {
  name     = "app_user"
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

resource "google_redis_instance" "cache" {
  name                    = "scholarminer-cache"
  tier                    = "STANDARD_HA"
  memory_size_gb          = 1
  region                  = var.region
  location_id             = var.ha_zones[0]
  alternative_location_id = var.ha_zones[1]
  authorized_network      = data.google_compute_network.default.self_link
  redis_version           = "REDIS_7_0"
  display_name            = "ScholarMiner Cache"

  depends_on = [
    google_project_service.redis,
    google_service_networking_connection.private_service_connection,
  ]
}

resource "google_pubsub_topic" "index_tasks" {
  name = "scholarminer-index-tasks"

  depends_on = [
    google_project_service.pubsub,
  ]
}

resource "google_pubsub_topic" "index_tasks_dlq" {
  name = "scholarminer-index-tasks-dlq"

  depends_on = [
    google_project_service.pubsub,
  ]
}

resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.index_tasks_dlq.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "index_tasks_worker" {
  name  = "scholarminer-index-worker"
  topic = google_pubsub_topic.index_tasks.name

  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "120s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.index_tasks_dlq.id
    max_delivery_attempts = 5
  }

  depends_on = [
    google_pubsub_topic_iam_member.dlq_publisher,
  ]
}

resource "google_service_account" "web" {
  account_id   = "scholarminer-web"
  display_name = "ScholarMiner Web Service"
}

resource "google_service_account" "worker" {
  account_id   = "scholarminer-worker"
  display_name = "ScholarMiner Worker Service"
}

resource "google_service_account" "observability" {
  account_id   = "scholarminer-observability"
  display_name = "ScholarMiner Observability Service"
}

resource "google_project_iam_member" "web_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.web.email}"
}

resource "google_project_iam_member" "web_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.web.email}"
}

resource "google_project_iam_member" "web_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.web.email}"
}

resource "google_project_iam_member" "worker_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_dataproc_editor" {
  project = var.project_id
  role    = "roles/dataproc.editor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "observability_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.observability.email}"
}

resource "google_project_iam_member" "observability_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.observability.email}"
}

resource "google_project_iam_member" "observability_compute_viewer" {
  project = var.project_id
  role    = "roles/compute.viewer"
  member  = "serviceAccount:${google_service_account.observability.email}"
}

resource "google_storage_bucket_iam_member" "worker_bucket_admin" {
  bucket = google_storage_bucket.data_bucket.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "null_resource" "frontend_image_build_push" {
  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/../lightweight-app"
    command     = "docker buildx build --platform linux/amd64 -t ${var.frontend_image} --push ."
  }
}

resource "null_resource" "worker_image_build_push" {
  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/../cluster-app"
    command     = "docker buildx build --platform linux/amd64 -t ${var.worker_image} --push ."
  }
}

resource "google_compute_instance" "observability_node" {
  name         = "scholarminer-observability"
  machine_type = "e2-medium"
  zone         = var.zone
  tags         = ["observability-public"]
  labels = {
    scholarminer_role = "observability"
  }

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = data.google_compute_network.default.self_link
    access_config {}
  }

  service_account {
    email  = google_service_account.observability.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile(
    "${path.module}/startup/observability_startup.sh.tftpl",
    {
      project_id             = var.project_id
      ha_zones               = var.ha_zones
      grafana_admin_password = random_password.grafana_admin_password.result
    }
  )

  depends_on = [
    google_project_service.compute,
    google_project_iam_member.observability_compute_viewer,
    google_project_iam_member.observability_monitoring_viewer,
  ]
}

resource "google_compute_instance_template" "web" {
  name_prefix  = "scholarminer-web-"
  machine_type = "e2-small"
  tags         = ["web-lb"]
  labels = {
    scholarminer_role       = "web"
    scholarminer_monitoring = "true"
  }

  disk {
    auto_delete  = true
    boot         = true
    source_image = "debian-cloud/debian-12"
  }

  network_interface {
    network = data.google_compute_network.default.self_link
    access_config {}
  }

  service_account {
    email  = google_service_account.web.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile(
    "${path.module}/startup/web_startup.sh.tftpl",
    {
      project_id          = var.project_id
      pubsub_topic        = google_pubsub_topic.index_tasks.name
      db_host             = google_sql_database_instance.postgres.private_ip_address
      db_name             = google_sql_database.application.name
      db_user             = google_sql_user.application.name
      db_password_secret  = google_secret_manager_secret.db_password.secret_id
      redis_host          = google_redis_instance.cache.host
      redis_port          = google_redis_instance.cache.port
      flask_secret_secret = google_secret_manager_secret.flask_secret.secret_id
      frontend_image      = var.frontend_image
    }
  )

  depends_on = [
    null_resource.frontend_image_build_push,
    google_secret_manager_secret_version.db_password,
    google_secret_manager_secret_version.flask_secret,
    google_project_iam_member.web_secret_accessor,
    google_project_iam_member.web_pubsub_publisher,
  ]
}

resource "google_compute_instance_template" "worker" {
  name_prefix  = "scholarminer-worker-"
  machine_type = "e2-standard-2"
  tags         = ["worker-health"]
  labels = {
    scholarminer_role       = "worker"
    scholarminer_monitoring = "true"
  }

  disk {
    auto_delete  = true
    boot         = true
    source_image = "debian-cloud/debian-12"
  }

  network_interface {
    network = data.google_compute_network.default.self_link
    access_config {}
  }

  service_account {
    email  = google_service_account.worker.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile(
    "${path.module}/startup/worker_startup.sh.tftpl",
    {
      project_id          = var.project_id
      pubsub_subscription = google_pubsub_subscription.index_tasks_worker.name
      db_host             = google_sql_database_instance.postgres.private_ip_address
      db_name             = google_sql_database.application.name
      db_user             = google_sql_user.application.name
      db_password_secret  = google_secret_manager_secret.db_password.secret_id
      redis_host          = google_redis_instance.cache.host
      redis_port          = google_redis_instance.cache.port
      gcs_bucket          = google_storage_bucket.data_bucket.name
      dataproc_cluster    = google_dataproc_cluster.hadoop_cluster.name
      dataproc_region     = var.region
      serpapi_secret      = google_secret_manager_secret.serpapi_key.secret_id
      worker_image        = var.worker_image
    }
  )

  depends_on = [
    null_resource.worker_image_build_push,
    google_secret_manager_secret_version.db_password,
    google_secret_manager_secret_version.serpapi_key,
    google_project_iam_member.worker_secret_accessor,
    google_project_iam_member.worker_pubsub_subscriber,
    google_project_iam_member.worker_dataproc_editor,
    google_storage_bucket_object.inverted_index_mapper,
    google_storage_bucket_object.inverted_index_reducer,
    google_storage_bucket_object.stopwords,
  ]
}

resource "google_compute_health_check" "web" {
  name               = "scholarminer-web-health"
  check_interval_sec = 10
  timeout_sec        = 5

  http_health_check {
    port         = 5000
    request_path = "/ready"
  }
}

resource "google_compute_health_check" "worker" {
  name               = "scholarminer-worker-health"
  check_interval_sec = 15
  timeout_sec        = 5

  http_health_check {
    port         = 8080
    request_path = "/ready"
  }
}

resource "google_compute_region_instance_group_manager" "web" {
  name               = "scholarminer-web-mig"
  base_instance_name = "scholarminer-web"
  region             = var.region
  target_size        = var.web_instance_count

  distribution_policy_zones = var.ha_zones

  version {
    instance_template = google_compute_instance_template.web.self_link
  }

  named_port {
    name = "http"
    port = 5000
  }

  auto_healing_policies {
    health_check      = google_compute_health_check.web.self_link
    initial_delay_sec = 300
  }
}

resource "google_compute_region_instance_group_manager" "worker" {
  name               = "scholarminer-worker-mig"
  base_instance_name = "scholarminer-worker"
  region             = var.region
  target_size        = var.worker_instance_count

  distribution_policy_zones = var.ha_zones

  version {
    instance_template = google_compute_instance_template.worker.self_link
  }

  named_port {
    name = "health"
    port = 8080
  }

  auto_healing_policies {
    health_check      = google_compute_health_check.worker.self_link
    initial_delay_sec = 300
  }
}

resource "google_compute_firewall" "allow_web_lb" {
  name    = "scholarminer-allow-web-lb"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["5000"]
  }

  source_ranges = ["130.211.0.0/22", "35.191.0.0/16"]
  target_tags   = ["web-lb"]
}

resource "google_compute_firewall" "allow_worker_health" {
  name    = "scholarminer-allow-worker-health"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }

  source_ranges = ["130.211.0.0/22", "35.191.0.0/16"]
  target_tags   = ["worker-health"]
}

resource "google_compute_firewall" "allow_internal" {
  name    = "scholarminer-allow-internal"
  network = data.google_compute_network.default.name

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

resource "google_compute_firewall" "allow_grafana_public" {
  name    = "scholarminer-allow-grafana"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["3000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["observability-public"]
}

resource "google_compute_firewall" "allow_prometheus_public" {
  name    = "scholarminer-allow-prometheus"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["9090"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["observability-public"]
}

resource "google_compute_global_address" "web_lb_ip" {
  name = "scholarminer-web-lb-ip"
}

resource "google_compute_backend_service" "web" {
  name          = "scholarminer-web-backend"
  protocol      = "HTTP"
  port_name     = "http"
  timeout_sec   = 30
  health_checks = [google_compute_health_check.web.self_link]

  backend {
    group = google_compute_region_instance_group_manager.web.instance_group
  }
}

resource "google_compute_url_map" "web" {
  name            = "scholarminer-web-url-map"
  default_service = google_compute_backend_service.web.self_link
}

resource "google_compute_target_http_proxy" "web" {
  name    = "scholarminer-web-http-proxy"
  url_map = google_compute_url_map.web.self_link
}

resource "google_compute_global_forwarding_rule" "web" {
  name       = "scholarminer-web-forwarding-rule"
  target     = google_compute_target_http_proxy.web.self_link
  port_range = "80"
  ip_address = google_compute_global_address.web_lb_ip.address
}

resource "google_dataproc_cluster" "hadoop_cluster" {
  name   = "scholarminer-dataproc"
  region = var.region

  cluster_config {
    staging_bucket = google_storage_bucket.data_bucket.name
    temp_bucket    = google_storage_bucket.temp_bucket.name

    master_config {
      num_instances = 3
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
    }
  }

  depends_on = [
    google_project_service.dataproc,
    google_storage_bucket_object.inverted_index_mapper,
    google_storage_bucket_object.inverted_index_reducer,
    google_storage_bucket_object.stopwords,
  ]
}

output "search_engine_url" {
  description = "Public URL to access the ScholarMiner web application"
  value       = "http://${google_compute_global_address.web_lb_ip.address}"
}

output "grafana_dashboard_url" {
  description = "Public URL to access the Grafana dashboard"
  value       = "http://${google_compute_instance.observability_node.network_interface.0.access_config.0.nat_ip}:3000"
}

output "prometheus_url" {
  description = "Public URL to access the Prometheus UI"
  value       = "http://${google_compute_instance.observability_node.network_interface.0.access_config.0.nat_ip}:9090"
}

output "grafana_admin_password" {
  description = "Grafana admin password for the auto-provisioned public dashboard"
  value       = random_password.grafana_admin_password.result
  sensitive   = true
}

output "dataproc_cluster_name" {
  value = google_dataproc_cluster.hadoop_cluster.name
}

output "dataproc_master_instance" {
  value = google_dataproc_cluster.hadoop_cluster.cluster_config[0].master_config[0].instance_names
}

output "gcs_bucket" {
  value = google_storage_bucket.data_bucket.name
}

output "cloudsql_private_ip" {
  value = google_sql_database_instance.postgres.private_ip_address
}

output "redis_host" {
  value = google_redis_instance.cache.host
}

output "pubsub_topic" {
  value = google_pubsub_topic.index_tasks.name
}

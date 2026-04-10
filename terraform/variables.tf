variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region"
  type        = string
  default     = "us-west1"
}

variable "zone" {
  description = "GCP Zone"
  type        = string
  default     = "us-west1-a"
}

variable "ha_zones" {
  description = "Zones used for regional managed instance groups and Memorystore HA"
  type        = list(string)
  default     = ["us-west1-a", "us-west1-b"]
}

variable "serpapi_key" {
  description = "SerpApi key for Google Scholar scraping"
  type        = string
  sensitive   = true
}

variable "frontend_image" {
  description = "Frontend docker image to build and push"
  type        = string
  default     = "laskyj/ieee-flask-app:latest"
}

variable "worker_image" {
  description = "Worker docker image to build and push"
  type        = string
  default     = "laskyj/scholarminer-worker:latest"
}

variable "web_instance_count" {
  description = "Number of web instances in the regional managed instance group"
  type        = number
  default     = 2
}

variable "worker_instance_count" {
  description = "Number of worker instances in the regional managed instance group"
  type        = number
  default     = 2
}

variable "data_bucket_force_destroy" {
  description = "Whether Terraform should delete the versioned data bucket and all objects during destroy"
  type        = bool
  default     = false
}

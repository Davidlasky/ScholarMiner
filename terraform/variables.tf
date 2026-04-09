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

variable "serpapi_key" {
  description = "SerpApi key for Google Scholar scraping"
  type        = string
}

variable "frontend_image" {
  description = "Frontend docker image to build and push"
  type        = string
  default     = "laskyj/ieee-flask-app:latest"
}

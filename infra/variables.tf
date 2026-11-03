variable "project_id" {
  description = "GCP project id."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL, Cloud Tasks and Cloud Storage."
  type        = string
  default     = "asia-south1"
}

variable "vertex_location" {
  description = "Vertex AI region for the embedding model (the only model API the server calls)."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  type    = string
  default = "ctx"
}

variable "image" {
  description = "Initial container image. CI deploys real images afterwards; Terraform ignores later image changes."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "public_base_url" {
  description = "Public URL (custom domain). Empty: the service's default run.app URL."
  type        = string
  default     = ""
}

variable "db_tier" {
  description = "Cloud SQL machine tier. db-f1-micro is the cheapest; use db-g1-small or larger for real use."
  type        = string
  default     = "db-f1-micro"
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}



variable "embedding_model" {
  type    = string
  default = "gemini-embedding-001"
}

variable "google_oauth_client_id" {
  description = "OAuth client for 'Sign in with Google' (APIs & Services > Credentials)."
  type        = string
  default     = ""
}

variable "google_oauth_client_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "github_oauth_client_id" {
  type    = string
  default = ""
}

variable "github_oauth_client_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "anthropic_api_key" {
  description = "Optional: exact Claude token counting."
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_repo" {
  description = "owner/name of the GitHub repo allowed to deploy (Workload Identity Federation). Empty: skip."
  type        = string
  default     = ""
}

variable "max_instances" {
  type    = number
  default = 3
}

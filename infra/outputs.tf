output "service_url" {
  value = local.base_url
}

output "mcp_url" {
  description = "Add this as a custom connector in Claude (Settings > Connectors) or ChatGPT."
  value       = "${local.base_url}/mcp"
}

output "google_oauth_redirect_uri" {
  description = "Authorized redirect URI to register on the Google OAuth client."
  value       = "${local.base_url}/auth/google/callback"
}

output "github_oauth_callback_url" {
  value = "${local.base_url}/auth/github/callback"
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  value = var.github_repo != "" ? google_iam_workload_identity_pool_provider.github[0].name : null
}

output "deploy_service_account" {
  value = var.github_repo != "" ? google_service_account.deploy[0].email : null
}

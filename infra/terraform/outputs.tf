output "env_group_name" {
  description = "The Render environment group the Blueprints reference via fromGroup."
  value       = render_env_group.managed.name
}

output "clickhouse_host" {
  value = clickhouse_service.events.endpoints.https.host
}

output "raw_events_bucket" {
  description = "Not Terraform-created (see main.tf) -- just echoes back the bucket name you supplied."
  value       = var.s3_bucket
}

output "observability_env_group_name" {
  description = "The Render environment group holding only the metrics token (Prometheus)."
  value       = render_env_group.observability.name
}

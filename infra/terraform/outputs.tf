output "env_group_name" {
  description = "The Render environment group the Blueprints reference via fromGroup."
  value       = render_env_group.managed.name
}

output "clickhouse_host" {
  value = clickhouse_service.events.endpoints.https.host
}

output "raw_events_bucket" {
  value = aws_s3_bucket.raw_events.bucket
}

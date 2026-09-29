variable "environment" {
  description = "Which environment this stack is: drives every resource name, so staging and prod never share a resource."
  type        = string

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be \"staging\" or \"prod\"."
  }
}

# --- Provider credentials -----------------------------------------------------
# Supply via TF_VAR_* environment variables (or CI secrets), never a committed
# tfvars file. All are sensitive so they never appear in plan output.

variable "render_api_key" {
  type      = string
  sensitive = true
}

variable "render_owner_id" {
  description = "The Render workspace id (usr-... or tea-...)."
  type        = string
}

variable "clickhouse_organization_id" {
  type = string
}

variable "clickhouse_token_key" {
  type      = string
  sensitive = true
}

variable "clickhouse_token_secret" {
  type      = string
  sensitive = true
}

# --- Sizing / placement (per-environment values live in *.tfvars) -------------

variable "render_region" {
  type    = string
  default = "oregon"
}

variable "postgres_plan" {
  description = "Render Postgres plan."
  type        = string
}

variable "postgres_version" {
  type    = string
  default = "16"
}

variable "keyvalue_plan" {
  description = "Render Key Value (Redis) plan."
  type        = string
}

variable "clickhouse_cloud_provider" {
  type    = string
  default = "aws"
}

variable "clickhouse_region" {
  description = "ClickHouse Cloud region; keep it close to render_region to keep ingest latency low."
  type        = string
  default     = "us-west-2"
}

variable "clickhouse_min_replica_memory_gb" {
  type = number
}

variable "clickhouse_max_replica_memory_gb" {
  type = number
}

variable "clickhouse_ip_allow_list" {
  description = "CIDRs allowed to reach ClickHouse. Render's outbound IPs for the chosen region (see Render docs) -- not 0.0.0.0/0 outside of a throwaway environment."
  type        = list(string)
}

# --- Object storage (Backblaze B2) ---------------------------------------------
# Not created by Terraform (see main.tf's comment on why) -- these are the
# bucket and Application Key values from B2's console, supplied via TF_VAR_*
# like the other provider credentials above, never a committed tfvars file.

variable "s3_endpoint_url" {
  description = "B2's S3-compatible endpoint for the bucket's region, e.g. https://s3.us-west-004.backblazeb2.com."
  type        = string
}

variable "s3_bucket" {
  type = string
}

variable "s3_region" {
  description = "B2's region id for the bucket, e.g. us-west-004 (part of the endpoint hostname)."
  type        = string
}

variable "s3_access_key" {
  description = "The B2 Application Key's keyID."
  type        = string
  sensitive   = true
}

variable "s3_secret_key" {
  description = "The B2 Application Key's applicationKey."
  type        = string
  sensitive   = true
}

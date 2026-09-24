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

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

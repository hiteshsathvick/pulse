provider "render" {
  api_key  = var.render_api_key
  owner_id = var.render_owner_id
}

provider "clickhouse" {
  organization_id = var.clickhouse_organization_id
  token_key       = var.clickhouse_token_key
  token_secret    = var.clickhouse_token_secret
}

locals {
  name = "pulse-${var.environment}"

  # Render hands back postgresql://user:pass@host/db. The app wants the
  # asyncpg dialect, and connects as `pulse_app` (a non-superuser -- Postgres
  # superusers bypass Row-Level Security even with FORCE, so the app must never
  # use the owner role; alembic/env.py creates pulse_app on first migrate).
  pg_internal            = render_postgres.db.connection_info.internal_connection_string
  pg_userinfo_host_db    = regex("^postgres(?:ql)?://(.+)$", local.pg_internal)[0]
  pg_host_and_db         = regex("^[^@]+@(.+)$", local.pg_userinfo_host_db)[0]
  database_bootstrap_url = "postgresql+asyncpg://${local.pg_userinfo_host_db}"
  database_url           = "postgresql+asyncpg://pulse_app:${random_password.app_db.result}@${local.pg_host_and_db}"
}

# --- Generated secrets --------------------------------------------------------

resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "random_password" "clickhouse" {
  length  = 32
  special = false
}

resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "random_password" "pii_hash_secret" {
  length  = 64
  special = false
}

# Bearer token for the API's /metrics route (Prometheus presents it). Letters and
# digits only: the Prometheus image's entrypoint substitutes it with sed and
# refuses anything else.
resource "random_password" "metrics_token" {
  length  = 48
  special = false
}

# --- Postgres (control plane) -------------------------------------------------

resource "render_postgres" "db" {
  name          = "${local.name}-db"
  plan          = var.postgres_plan
  region        = var.render_region
  version       = var.postgres_version
  database_name = "pulse"
  database_user = "pulse"

  high_availability_enabled = var.environment == "prod"
}

# --- Redis / Key Value (ingest buffer, rate limits, query cache) --------------
# noeviction is load-bearing, not a tuning knob: the ingest buffer is a Redis
# Stream that is the ONLY copy of an event between /ingest returning 202 and
# the worker landing it in ClickHouse. Under memory pressure an evicting policy
# (Render's default is allkeys-lru) would silently delete un-landed events;
# noeviction makes Redis refuse writes instead, which surfaces as a loud 5xx
# on /ingest -- backpressure, not data loss.

resource "render_keyvalue" "cache" {
  name              = "${local.name}-redis"
  plan              = var.keyvalue_plan
  region            = var.render_region
  max_memory_policy = "noeviction"
}

# --- ClickHouse Cloud (event plane) --------------------------------------------

resource "clickhouse_service" "events" {
  name           = "${local.name}-events"
  cloud_provider = var.clickhouse_cloud_provider
  region         = var.clickhouse_region

  password = random_password.clickhouse.result

  min_replica_memory_gb = var.clickhouse_min_replica_memory_gb
  max_replica_memory_gb = var.clickhouse_max_replica_memory_gb
  idle_scaling          = var.environment != "prod"
  # Required by the provider whenever idle_scaling is true (a real, first-plan
  # surprise no static check caught -- terraform validate doesn't call
  # ClickHouse's API). 5 is the provider's own minimum: idle as aggressively
  # as possible for a non-prod service that mostly sits unused.
  idle_timeout_minutes = var.environment != "prod" ? 5 : null

  ip_access = [
    for cidr in var.clickhouse_ip_allow_list : {
      source      = cidr
      description = "${local.name} allow-list"
    }
  ]

  lifecycle {
    precondition {
      condition     = length(var.clickhouse_ip_allow_list) > 0
      error_message = "clickhouse_ip_allow_list must not be empty: set it to Render's outbound IP ranges for your region."
    }
  }
}

# --- Object storage (raw batch archive, Phase 8) -------------------------------
#
# Backblaze B2, not AWS S3: it speaks the same S3 API the app's Minio client
# already uses (pulse/repositories/object_storage.py -- identical to how the
# local stack runs SeaweedFS instead of MinIO, Phase 24), and its free tier
# (10 GB storage, no egress fees) needs no card on file at all, confirmed
# directly on Backblaze's own sign-up page, unlike AWS.
#
# Not Terraform-managed, on purpose: B2's S3-compatible endpoint covers
# buckets and objects, but not IAM -- there is no API this provider (or any
# S3-compatible one) can reach to create a least-privilege credential the way
# aws_iam_user/aws_iam_access_key did. A bucket-scoped "Application Key" is
# B2's equivalent, created once by hand in the B2 web console (free, no
# Terraform resource for it exists); see docs/DEPLOYMENT.md. The four
# `s3_*` variables below are that key's values, supplied the same way as
# every other provider credential (TF_VAR_*, never committed).

# --- Handoff to Render ---------------------------------------------------------
# Everything the app reads from its environment that this stack owns, written
# into one Render environment group. The Blueprints reference it with
# `fromGroup`, so no connection string or secret is ever pasted by hand or
# committed. Variable names are exactly pulse/core/config.py's settings.

resource "render_env_group" "managed" {
  name = "${local.name}-managed"

  env_vars = {
    ENVIRONMENT            = { value = "production" }
    DATABASE_URL           = { value = local.database_url }
    DATABASE_BOOTSTRAP_URL = { value = local.database_bootstrap_url }
    REDIS_URL              = { value = render_keyvalue.cache.connection_info.internal_connection_string }

    CLICKHOUSE_HOST     = { value = clickhouse_service.events.endpoints.https.host }
    CLICKHOUSE_PORT     = { value = tostring(clickhouse_service.events.endpoints.https.port) }
    CLICKHOUSE_USER     = { value = "default" }
    CLICKHOUSE_PASSWORD = { value = random_password.clickhouse.result }
    CLICKHOUSE_DATABASE = { value = "default" }
    CLICKHOUSE_SECURE   = { value = "true" }

    S3_ENDPOINT_URL = { value = var.s3_endpoint_url }
    S3_ACCESS_KEY   = { value = var.s3_access_key }
    S3_SECRET_KEY   = { value = var.s3_secret_key }
    S3_BUCKET       = { value = var.s3_bucket }
    S3_REGION       = { value = var.s3_region }

    JWT_SECRET      = { value = random_password.jwt_secret.result }
    PII_HASH_SECRET = { value = random_password.pii_hash_secret.result }
    METRICS_TOKEN   = { value = random_password.metrics_token.result }
  }
}

# Prometheus needs the metrics token and nothing else, so it gets its own group
# rather than the managed one (which holds database and signing secrets). The
# token is the same generated value in both groups -- one source, two readers.
resource "render_env_group" "observability" {
  name = "${local.name}-observability"

  env_vars = {
    METRICS_TOKEN = { value = random_password.metrics_token.result }
  }
}

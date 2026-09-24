provider "render" {
  api_key  = var.render_api_key
  owner_id = var.render_owner_id
}

provider "clickhouse" {
  organization_id = var.clickhouse_organization_id
  token_key       = var.clickhouse_token_key
  token_secret    = var.clickhouse_token_secret
}

provider "aws" {
  region = var.aws_region
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

resource "random_id" "bucket_suffix" {
  byte_length = 4
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

resource "aws_s3_bucket" "raw_events" {
  bucket        = "${local.name}-raw-events-${random_id.bucket_suffix.hex}"
  force_destroy = var.environment != "prod"
}

resource "aws_s3_bucket_public_access_block" "raw_events" {
  bucket                  = aws_s3_bucket.raw_events.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_events" {
  bucket = aws_s3_bucket.raw_events.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "raw_events" {
  bucket = aws_s3_bucket.raw_events.id

  versioning_configuration {
    status = var.environment == "prod" ? "Enabled" : "Suspended"
  }
}

# Least privilege: the archiver only lists, reads and writes objects in this
# one bucket -- it cannot create or delete buckets, or touch anything else.
resource "aws_iam_user" "archiver" {
  name = "${local.name}-archiver"
}

resource "aws_iam_user_policy" "archiver" {
  name = "${local.name}-archiver"
  user = aws_iam_user.archiver.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.raw_events.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.raw_events.arn}/*"
      },
    ]
  })
}

resource "aws_iam_access_key" "archiver" {
  user = aws_iam_user.archiver.name
}

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

    S3_ENDPOINT_URL = { value = "https://s3.${var.aws_region}.amazonaws.com" }
    S3_ACCESS_KEY   = { value = aws_iam_access_key.archiver.id }
    S3_SECRET_KEY   = { value = aws_iam_access_key.archiver.secret }
    S3_BUCKET       = { value = aws_s3_bucket.raw_events.bucket }
    S3_REGION       = { value = var.aws_region }

    JWT_SECRET      = { value = random_password.jwt_secret.result }
    PII_HASH_SECRET = { value = random_password.pii_hash_secret.result }
  }
}

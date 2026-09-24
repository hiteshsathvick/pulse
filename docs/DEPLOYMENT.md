# Deployment

Phase 23. Staging and production on Render, with the data services on ClickHouse Cloud, AWS S3 and
Render's managed Postgres/Key Value — all described as code and validated in CI.

> **Status: built and validated, never applied.** No Render, ClickHouse Cloud or AWS account was
> used to produce this. Everything below is checked statically (Terraform validated against the real
> provider schemas, Blueprints validated against Render's published schema, the release logic unit
> tested), but a real first deploy will find things a static check can't. The Definition-of-Done
> sentence "a merge to main deploys to staging automatically" is therefore proven **up to the
> configuration** (`autoDeployTrigger: checksPass`), not by a live deploy. See "Not yet proven".

## What lives where

| Piece | Owner | Why |
|---|---|---|
| Postgres, Key Value (Redis) | Terraform (`infra/terraform`) | Render provider |
| ClickHouse | Terraform → ClickHouse Cloud | Render can't host it |
| Raw-batch archive bucket + least-privilege IAM user | Terraform → AWS S3 | |
| Every connection string and generated secret | Terraform → Render **env group** `pulse-<env>-managed` | Nothing pasted by hand, nothing committed |
| API, 4 workers, frontend | Render Blueprints (`infra/render/{staging,prod}.render.yaml`) | Render's native format |
| Prometheus + Grafana | Same Blueprints (a private service and a web service) | So the dashboards exist where the app runs; see "Deployed observability" |
| The `/metrics` bearer token | Terraform → env groups `pulse-<env>-managed` (API) and `pulse-<env>-observability` (Prometheus) | One generated value, two readers; Prometheus gets nothing else |
| Staging deploys | Render, `autoDeployTrigger: checksPass` | Merge → CI green → deploy |
| Production deploys | `.github/workflows/deploy-prod.yml` | Manual + approval gate |

## One-time setup

1. **Accounts and credentials** (as `TF_VAR_*` env vars or CI secrets — never a committed file):
   `render_api_key`, `render_owner_id`, `clickhouse_organization_id`, `clickhouse_token_key`,
   `clickhouse_token_secret`, plus AWS credentials for the S3 provider.
2. **Provision each environment** with its own Terraform **workspace** (separate state per
   environment — this is what keeps staging and prod from ever sharing a resource):
   ```bash
   cd infra/terraform
   terraform init
   terraform workspace new staging && terraform apply -var-file=staging.tfvars
   terraform workspace new prod    && terraform apply -var-file=prod.tfvars
   ```
   `prod.tfvars` deliberately ships `clickhouse_ip_allow_list = []`, which **fails the plan**: fill it
   with Render's outbound IPs for your region. Configure a **remote state backend** first — state
   contains the generated secrets in plain text and must not live on a laptop (local state files are
   git-ignored, but that is a safety net, not a plan). See "Remote Terraform state" below.
3. **Create the Blueprints** in Render, one per environment, each pointing at its own file
   (`infra/render/staging.render.yaml`, `infra/render/prod.render.yaml`). Fill the `sync: false` values
   Render prompts for: `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and the frontend's
   `NEXT_PUBLIC_API_URL` / `API_INTERNAL_URL`, and Grafana's `GF_SECURITY_ADMIN_PASSWORD` (choose one;
   it is never committed).
4. **Production gate.** In GitHub: Settings → Environments → `production` → *Required reviewers*.
   Add repository secret `RENDER_API_KEY` and variables `RENDER_PROD_SERVICE_IDS` (space-separated
   `srv-…` ids, **API first**) and `PROD_API_URL`.

## How a release works

- **Staging:** merge to `master` → CI passes → Render deploys every staging service automatically.
- **Production:** run *Deploy production* (Actions → Run workflow; optional commit SHA, default the tip
  of `master`). It (1) resolves the exact SHA, (2) **refuses if CI hasn't passed for that commit**,
  (3) waits for a reviewer to approve the `production` environment — the one gated click — then
  (4) releases via `infra/scripts/render_release.py`: **the API first**, waiting until it is `live`, and
  only then the workers and frontend together; any failed or timed-out deploy aborts the release **and
  rolls back** (below); (5) smoke-checks `/health`.

### Rollback

Before releasing, `render_release.py` records each service's currently-live deploy. If anything fails
it waits for in-flight deploys to settle (rolling back a service mid-build would race it), then rolls
every service that had **already gone live** back to its recorded deploy — workers first, API last —
and waits for those rollbacks to go live. The workflow still ends red, and the error says what was
rolled back. Three cases it reports rather than hides:

- **A rollback that itself fails** is reported as `ROLLBACK FAILED … intervene manually`.
- **A first release** has no previous live deploy, so there is nothing to roll back to; it says so.
- **Migrations are forward-only and are not undone.** A rolled-back version therefore runs against the
  migrated schema, which is why migrations must be backwards-compatible (next section). Rollback is
  only as safe as that discipline.

A failed **API** deploy needs no rollback: Render doesn't promote a deploy whose pre-deploy command or
health check failed, so the previous version keeps serving and no worker was ever released.

## Migrations on deploy

The API's `preDeployCommand` is `python -m pulse.migrate`: Postgres (alembic) **then** ClickHouse, both
idempotent, one command, no shell (Render exec's docker commands without one). A failing migration
fails the deploy and the previous version keeps serving. Because the API rolls first and workers
follow, a migration must be **backwards-compatible with the previous version's code** for the window
in which the old workers still run against the new schema (add columns/tables first; remove or rename
in a later release).

## Remote Terraform state

Local state is the default and is fine for one person on one machine; anything shared needs a remote
backend. It is **opt-in and per machine**, so nothing changes for anyone who doesn't use it:

```bash
cd infra/terraform
cp backend_override.tf.example backend_override.tf      # git-ignored; Terraform merges *_override.tf
terraform init \
  -backend-config="bucket=<state-bucket>" -backend-config="key=pulse/<staging|prod>.tfstate" \
  -backend-config="region=<aws-region>" -backend-config="dynamodb_table=<lock-table>"
```

The bucket and lock table are created once, by hand (Terraform can't create the place its own state
lives):

```bash
aws s3api create-bucket --bucket <state-bucket> --region <region>   # add --create-bucket-configuration outside us-east-1
aws s3api put-bucket-versioning --bucket <state-bucket> --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket <state-bucket> --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws dynamodb create-table --table-name <lock-table> --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH
```

Versioning matters: state is the record of what exists, and a corrupted write is otherwise unrecoverable.
`encrypt = true` is set in the example. Use a separate `key` per environment.

What was checked: CI validates the override; and against a local S3-compatible server (MinIO) `terraform
init` with this recipe selects the S3 backend and a workspace's state lands in the bucket. What was **not**
exercised: DynamoDB locking (no DynamoDB locally) and real AWS. Terraform 1.10+ can lock with S3 alone
(`use_lockfile`) and deprecates the DynamoDB table; this repo pins 1.9.8, so the table is used.

## Deployed observability

`docs/OBSERVABILITY.md` describes the stack running locally. Deployed, the same dashboard is served by a
Prometheus private service and a Grafana web service defined in both Blueprints
(`infra/observability/*.render.*`). What Render's networking forced:

- **Workers are private services, not background workers.** Render workers can't receive private-network
  traffic, so Prometheus could never reach them. Each worker serves `/metrics` on port 9100 (`PORT` and
  `METRICS_PORT` both 9100, so Render routes to the port the metrics server binds).
- **The API's metrics are a bearer-token route on its primary port.** Only a web service's primary port is
  reachable privately, so the separate metrics port used locally can't be. `GET /metrics` exists only when
  `METRICS_TOKEN` is set (otherwise a plain 404), and needs `Authorization: Bearer <token>` (constant-time
  comparison). Terraform generates the token and puts it in the API's and Prometheus's env groups.
- **Targets can't be hard-coded.** Render hostnames carry a random suffix, so the Blueprint passes each one
  in with `fromService` and Prometheus scrapes its `<host>-discovery` name, which resolves to *every*
  running instance — a 2-instance API is scraped per instance, not through a load balancer that would
  alternate between instances' counters. The config is rendered at container start from those variables;
  the entrypoint refuses to start if any is missing or contains anything unsafe.
- **Prometheus keeps its data on a persistent disk** (`/var/data`, per Render's Prometheus guide).
- **Grafana requires login** (locally it is anonymous); the admin password is a `sync: false` prompt.
- **There is no Tempo.** Traces need an OTLP endpoint: point `OTEL_EXPORTER_OTLP_ENDPOINT` at an external
  collector, or leave it unset. The dashboard's "Recent event traces" panel is empty without one.

Checked locally, against the real images: the API and an ingest worker given Render-style `-discovery`
aliases were discovered by the Render Prometheus image and both scraped `up` (the API with its bearer
token on its primary port); the data landed on the mounted volume; the Grafana image required login,
provisioned its datasource from the environment and the dashboard, and queried Prometheus.

## Things that will bite if you change them

- **Redis must be `noeviction`.** The ingest stream is the only copy of an event between `/ingest`
  returning 202 and the worker landing it in ClickHouse; an evicting policy (Render's default) would
  silently delete un-landed events. `noeviction` turns memory pressure into a loud `/ingest` error
  instead. This is only safe because acknowledged entries are now deleted (`worker/consumer.py::ack`);
  before Phase 23 the stream grew forever — found by the new stream-length dashboard panel.
- **The app must connect as `pulse_app`, never the database owner.** Superusers/owners bypass
  Row-Level Security, so `DATABASE_URL` uses `pulse_app` and only `DATABASE_BOOTSTRAP_URL` uses the
  owner (alembic creates the role). Pinned by a test.
- **`environment = "production"` is set for staging too** — `Settings.environment` only has
  development/test/production, and staging should behave like production.

## Not yet proven (be honest with yourself before relying on this)

- **No live deploy has happened.** Expect first-deploy surprises (Flowforge's Render deploy found three
  that no static check could).
- **Deployed metrics are simulated, not proven on Render.** The scrape path is verified with the real
  images against Render-style DNS names, but not against Render itself. The assumptions that only a real
  deploy can confirm: that `fromService … property: host` yields the name the `-discovery` hostname is
  built from, that a private service's `PORT` decides which port Render routes to, and that a persistent
  disk mounts writable for the root user. Tempo isn't deployed at all.
- **Frontend env vars** (`NEXT_PUBLIC_API_URL`, `API_INTERNAL_URL`) can't be composed by a Blueprint, so
  they're filled by hand once per environment.
- **No image signing / SBOM.** The image scan (Trivy) and dependency audits are now blocking gates — see
  `docs/THREAT_MODEL.md` for what that costs (a newly published advisory can turn CI red on its own).
- **Rollback is tested against a fake Render client and Render's documented API**, not a real service:
  the endpoints (`GET /services/{id}/deploys`, `POST /services/{id}/rollback`) and status values come from
  Render's API reference. The first real failed release is the first real test. Render's own caveat —
  a rollback does not disable auto-deploy, so an auto-deploy could restore what was rolled back — does not
  apply here: rollback only runs in the production workflow, and production never auto-deploys.

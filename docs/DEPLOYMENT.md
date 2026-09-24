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
   git-ignored, but that is a safety net, not a plan).
3. **Create the Blueprints** in Render, one per environment, each pointing at its own file
   (`infra/render/staging.render.yaml`, `infra/render/prod.render.yaml`). Fill the `sync: false` values
   Render prompts for: `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and the frontend's
   `NEXT_PUBLIC_API_URL` / `API_INTERNAL_URL`.
4. **Production gate.** In GitHub: Settings → Environments → `production` → *Required reviewers*.
   Add repository secret `RENDER_API_KEY` and variables `RENDER_PROD_SERVICE_IDS` (space-separated
   `srv-…` ids, **API first**) and `PROD_API_URL`.

## How a release works

- **Staging:** merge to `master` → CI passes → Render deploys every staging service automatically.
- **Production:** run *Deploy production* (Actions → Run workflow; optional commit SHA, default the tip
  of `master`). It (1) resolves the exact SHA, (2) **refuses if CI hasn't passed for that commit**,
  (3) waits for a reviewer to approve the `production` environment — the one gated click — then
  (4) releases via `infra/scripts/render_release.py`: **the API first**, waiting until it is `live`, and
  only then the workers and frontend together; any failed or timed-out deploy aborts the release;
  (5) smoke-checks `/health`.

## Migrations on deploy

The API's `preDeployCommand` is `python -m pulse.migrate`: Postgres (alembic) **then** ClickHouse, both
idempotent, one command, no shell (Render exec's docker commands without one). A failing migration
fails the deploy and the previous version keeps serving. Because the API rolls first and workers
follow, a migration must be **backwards-compatible with the previous version's code** for the window
in which the old workers still run against the new schema (add columns/tables first; remove or rename
in a later release).

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
- **Render workers aren't scraped by Prometheus.** The local observability stack scrapes containers on a
  shared network; on Render, traces (OTLP) and errors (Sentry) work, but metrics need Grafana Cloud's
  agent or similar. The dashboards are proven locally, not in the deployed environments.
- **Frontend env vars** (`NEXT_PUBLIC_API_URL`, `API_INTERNAL_URL`) can't be composed by a Blueprint, so
  they're filled by hand once per environment.
- **No image signing / SBOM**, and image scanning is advisory (same reason as dependency scanning:
  known CVEs with no available fix — see `docs/THREAT_MODEL.md`).
- **No rollback automation.** Render keeps previous deploys; rolling back is a manual "redeploy this
  commit", and a migration is not undone by rolling code back.

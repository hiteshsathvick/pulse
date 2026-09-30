# Deployment

Phase 23. Staging and production on Render, with the data services on ClickHouse Cloud, Backblaze B2
and Render's managed Postgres/Key Value — all described as code and validated in CI.

> **Status: staging applied and proven live (Phase 26); production still config-only.** `terraform apply`
> ran for real against Render, ClickHouse Cloud and Backblaze B2. `GET /health` on the live API reports
> Postgres, ClickHouse and Redis all `ok`; the frontend server-renders against the live API; and a real
> commit to `master` triggered a Render deploy with nobody touching a button, proving the Definition-of-Done
> sentence "a merge to main deploys to staging automatically" for real, not just by configuration. Two real
> bugs were caught by the first live `terraform plan`/`apply` that no static check could have found (a
> stale Postgres plan-id format; a ClickHouse Cloud field the provider requires but `validate` doesn't
> check) — see the Phase 26 changelog entry in SPEC.md. Production has still never been applied. See "Not
> yet proven" for what staging itself still hasn't exercised.

## What lives where

| Piece | Owner | Why |
|---|---|---|
| Postgres, Key Value (Redis) | Terraform (`infra/terraform`) | Render provider |
| ClickHouse | Terraform → ClickHouse Cloud | Render can't host it |
| Raw-batch archive bucket | Backblaze B2, created by hand (Phase 26 -- see below) | Same S3 API the app already speaks; no card on file needed, unlike AWS |
| Every connection string and generated secret | Terraform → Render **env group** `pulse-<env>-managed` | Nothing pasted by hand, nothing committed |
| API, 4 workers, frontend | Render Blueprints (`infra/render/{staging,prod}.render.yaml`) | Render's native format |
| Prometheus + Grafana | Same Blueprints (a private service and a web service) | So the dashboards exist where the app runs; see "Deployed observability" |
| The `/metrics` bearer token | Terraform → env groups `pulse-<env>-managed` (API) and `pulse-<env>-observability` (Prometheus) | One generated value, two readers; Prometheus gets nothing else |
| Staging deploys | Render, `autoDeployTrigger: checksPass` | Merge → CI green → deploy |
| Production deploys | `.github/workflows/deploy-prod.yml` | Manual + approval gate |

## One-time setup

1. **Accounts and credentials** (as `TF_VAR_*` env vars or CI secrets — never a committed file):
   `render_api_key`, `render_owner_id`, `clickhouse_organization_id`, `clickhouse_token_key`,
   `clickhouse_token_secret`, plus five Backblaze B2 values (below) for the object store.
2. **Create the B2 bucket and its key by hand** — not Terraform: B2's S3-compatible endpoint covers
   buckets and objects, but not an IAM-equivalent API a provider could call to create a scoped
   credential. In the [B2 web console](https://www.backblaze.com/cloud-storage) (free, no card): create
   a bucket (private, name it e.g. `pulse-<env>-raw-events`), then Account → Application Keys → *Add a
   New Application Key*, scoped to just that bucket with read+write access. That gives you
   `s3_bucket` (the bucket name), `s3_endpoint_url` (shown alongside the bucket, e.g.
   `https://s3.us-west-004.backblazeb2.com`), `s3_region` (the same endpoint's region segment, e.g.
   `us-west-004`), `s3_access_key` (the key's `keyID`) and `s3_secret_key` (the key's
   `applicationKey`, shown once).
3. **Provision each environment** with its own Terraform **workspace** (separate state per
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
4. **Create the Blueprints** in Render, one per environment, each pointing at its own file
   (`infra/render/staging.render.yaml`, `infra/render/prod.render.yaml`). Fill the `sync: false` values
   Render prompts for: `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and the frontend's
   `NEXT_PUBLIC_API_URL` / `API_INTERNAL_URL`, and Grafana's `GF_SECURITY_ADMIN_PASSWORD` (choose one;
   it is never committed).
5. **Production gate.** In GitHub: Settings → Environments → `production` → *Required reviewers*.
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
backend. It is **opt-in and per machine**, so nothing changes for anyone who doesn't use it -- and it is
the one place this project still touches AWS (Phase 26 moved the app's own object storage to Backblaze
B2; Terraform's *state* backend is a separate, independent choice, and this section's `aws s3api`/
`dynamodb` commands are only for anyone who opts into it):

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

- **Production has never been applied.** Only staging was; expect it to have its own first-deploy
  surprises even though staging's are now fixed (Flowforge's prod Render deploy found three that staging
  testing hadn't).
- **Deployed metrics were proven live in Phase 33.** A second staging apply (all 10 resources fresh, the
  same real bugs from Phase 26 already fixed so this one applied clean on the first try) put real traffic
  through the deployed API -- 20 events ingested via `/ingest` -- and the Grafana dashboard's own
  `Accepted / sec (API)` panel moved from a flat `0 ops/s` to a real `0.378 ops/s` spike at the same
  timestamp, confirmed with a screenshot, not just a query. Verified past the dashboard too: all 20 events
  were queried back out of ClickHouse via the real API's export endpoint, byte-for-byte matching what was
  sent. Tempo still isn't deployed.
- **Object storage connectivity from a real deployed Render service is now proven too (Phase 33).**
  `GET /health` on the live `pulse-staging-api.onrender.com` reported `"object_storage": "ok"` alongside
  Postgres/ClickHouse/Redis -- the first time Phase 29's check has ever run against a real B2 connection
  from Render's own network, not local SeaweedFS or a host venv. Combined with Phase 28's earlier proof
  that the B2 credentials/bucket themselves work, both halves of this gap are now closed.
- **Frontend env vars** (`NEXT_PUBLIC_API_URL`, `API_INTERNAL_URL`) can't be composed by a Blueprint, so
  they're filled by hand once per environment -- confirmed for real this round: Render didn't even
  auto-redeploy the frontend after the env var was corrected, a manual deploy trigger was needed.
- **SBOM + signed provenance attestation exist (Phase 30), but for the CI-built image, not what Render
  deploys.** CI generates a CycloneDX SBOM for the backend/frontend images it builds and scans, and signs
  it keylessly (Sigstore via GitHub's OIDC token — `actions/attest-build-provenance`, verifiable with
  `gh attestation verify <sbom> --owner <org>`). This proves the SBOM genuinely describes what that CI run
  built and scanned from that commit. It does **not** prove Render is running that exact image: Render
  builds every service from source itself (`runtime: docker` in the Blueprints), so CI's image is a
  parallel, unpushed artifact — same Dockerfile and source, but never the literal bits Render deploys. The
  image scan (Trivy) and dependency audits remain separately blocking gates — see `docs/THREAT_MODEL.md`
  for what that costs (a newly published advisory can turn CI red on its own).
- **Rollback was proven against a real service in Phase 34.** A third staging deploy, plus a real broken
  commit (the worker's entrypoint deliberately made to crash on start, pushed to a disposable branch and
  never merged) released with `infra/scripts/render_release.py`'s actual `HttpRenderClient` against real
  service ids. The worker's deploy genuinely came back `update_failed` from Render's own API; the API
  service (which HAD deployed the bad commit successfully) was correctly rolled back to its previous live
  deploy, confirmed independently by re-querying deploy history directly, not just trusting the script's own
  report. Still not proven: this ran through `render_release.py` directly against staging service ids, not
  through the real gated `deploy-prod.yml` workflow -- the production-only trigger path (manual dispatch,
  required-reviewer environment) itself remains unexercised. Render's own caveat — a rollback does not
  disable auto-deploy, so an auto-deploy could restore what was rolled back — does not apply here: rollback
  only runs in the production workflow, and production never auto-deploys.

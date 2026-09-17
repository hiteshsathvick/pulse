# Pulse

Self-hostable real-time product-analytics / event data platform.

See [`SPEC.md`](SPEC.md) for the engineering specification, [`CLAUDE.md`](CLAUDE.md) for agent
operating rules, and [`docs/PULSE_PROJECT_GUIDE.md`](docs/PULSE_PROJECT_GUIDE.md) for the product
and architecture rationale. We build strictly phase-by-phase per `SPEC.md` §7.

## Local development

```bash
cd infra/docker
cp .env.example .env
docker compose up --build
docker compose run --rm api alembic upgrade head           # Postgres -- first run only, see below
docker compose run --rm api python -m pulse.clickhouse_migrations  # ClickHouse -- first run only
```

- Frontend: http://localhost:3000
- API: http://localhost:8000
- Health check: http://localhost:8000/health (verifies Postgres, ClickHouse, and Redis connectivity)

The Postgres check will report an auth error until you've run `alembic upgrade head` at least once:
the app connects as a dedicated non-superuser role (`pulse_app`) so Row-Level Security actually applies
(Postgres bypasses RLS for superusers, even with `FORCE`), and that role is created by the migration's
bootstrap step, not by `docker compose up` itself. This degrades gracefully rather than crashing --
ClickHouse and Redis still report fine independently.

## Backend tests

```bash
cd infra/docker
docker compose run --rm api pytest -v
```

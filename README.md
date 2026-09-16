# Pulse

Self-hostable real-time product-analytics / event data platform.

See [`SPEC.md`](SPEC.md) for the engineering specification, [`CLAUDE.md`](CLAUDE.md) for agent
operating rules, and [`docs/PULSE_PROJECT_GUIDE.md`](docs/PULSE_PROJECT_GUIDE.md) for the product
and architecture rationale. We build strictly phase-by-phase per `SPEC.md` §7.

## Local development (Phase 0)

```bash
cd infra/docker
cp .env.example .env
docker compose up --build
```

- Frontend: http://localhost:3000
- API: http://localhost:8000
- Health check: http://localhost:8000/health (verifies Postgres, ClickHouse, and Redis connectivity)

## Backend tests

```bash
cd infra/docker
docker compose run --rm api pytest -v
```

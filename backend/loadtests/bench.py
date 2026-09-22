"""Phase 17 benchmark harness. See docs/PERFORMANCE.md for method and results.

  python loadtests/bench.py setup [--events N] [--users U] [--days D] [--reset]
  python loadtests/bench.py run   --scenario query|ingest --label NAME
                                  [--rollups on|off] [--users N] [--duration S]
  python loadtests/bench.py report

Everything runs against an isolated `pulse_bench` Postgres database, ClickHouse
database and Redis DB 1, so it can never touch (or be wiped by) the dev data or
the test suite -- whose teardown drops the shared `events` table. Host, port and
credentials come from the caller's environment exactly as for the API; only the
database names are swapped.

Run from the backend directory with the loadtest extra installed:
  uv pip install ".[loadtest]"
"""

import argparse
import asyncio
import csv
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
RESULTS = HERE / "results"
CONFIG = HERE / ".bench.json"  # holds API keys: gitignored
BENCH_DB = "pulse_bench"
BENCH_REDIS_DB = 1
API = "http://127.0.0.1:8000"
_UNLIMITED = "1000000000"

# Groups the per-request stats are judged in (docs/PERFORMANCE.md, SPEC.md 1.4).
DASHBOARD_ROLLUP_QUERIES = [
    "trend.dau",
    "trend.event_counts",
    "trend.wau",
    "trend.mau",
    "trend.hourly",
]
ADHOC_RAW_QUERIES = ["trend.filtered", "trend.breakdown", "funnel", "retention"]


def _swap_database(url: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + BENCH_DB


def bench_env() -> dict[str, str]:
    """The caller's environment with the databases swapped for the bench ones,
    and the per-tenant limits lifted (they have their own tests, and a load
    test that just measures its own 429s is useless)."""
    from pulse.core.config import Settings

    base = Settings()
    redis_url = base.redis_url.rsplit("/", 1)[0] + f"/{BENCH_REDIS_DB}"
    env = dict(os.environ)
    env.update(
        DATABASE_URL=_swap_database(base.database_url),
        DATABASE_BOOTSTRAP_URL=_swap_database(base.database_bootstrap_url),
        CLICKHOUSE_DATABASE=BENCH_DB,
        REDIS_URL=redis_url,
        QUERY_RATE_LIMIT_MAX_QUERIES=_UNLIMITED,
        INGEST_RATE_LIMIT_MAX_REQUESTS=_UNLIMITED,
    )
    return env


# --------------------------------------------------------------------- setup


def _run(cmd: list[str], env: dict[str, str]) -> None:
    subprocess.run(cmd, cwd=BACKEND, env=env, check=True, capture_output=True, text=True)


async def _create_postgres_db(env: dict[str, str], reset: bool) -> None:
    import asyncpg

    admin = env["DATABASE_BOOTSTRAP_URL"].replace("postgresql+asyncpg://", "postgresql://")
    admin = admin.rsplit("/", 1)[0] + "/postgres"
    conn = await asyncpg.connect(admin)
    try:
        if reset:
            await conn.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", BENCH_DB):
            await conn.execute(f"CREATE DATABASE {BENCH_DB}")
    finally:
        await conn.close()


def _clickhouse(env: dict[str, str], database: str):
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env.get("CLICKHOUSE_HOST", "localhost"),
        port=int(env.get("CLICKHOUSE_PORT", "8123")),
        username=env.get("CLICKHOUSE_USER", "pulse"),
        password=env.get("CLICKHOUSE_PASSWORD", "pulse"),
        database=database,
        send_receive_timeout=1800,
    )


def _generate_events(ch, org_id: str, project_id: str, rows: int, users: int, days: int) -> None:
    """Server-side: ClickHouse fabricates the rows, so a few million events take
    seconds instead of hours. The materialized view fires on these inserts, so
    the rollup fills as the data does. ~60% of events come from identified
    users, the rest from anonymous visitors; timestamps spread over `days`."""
    chunk = 1_000_000
    done = 0
    while done < rows:
        n = min(chunk, rows - done)
        ch.command(
            f"""
            INSERT INTO events
                (org_id, project_id, event_id, event_name, user_id, anonymous_id,
                 timestamp, received_at, properties, _ingest_batch)
            SELECT toUUID('{org_id}'), toUUID('{project_id}'), generateUUIDv4(),
                multiIf(r < 0.45, 'page viewed', r < 0.65, 'button clicked',
                        r < 0.80, 'signed up', r < 0.92, 'added to cart',
                        'checkout completed'),
                if(rand() % 100 < 60, concat('user-', toString(rand() % {users})), ''),
                concat('anon-', toString(rand() % {users * 2})),
                now64(3) - toIntervalMillisecond(rand64() % {days * 86_400_000}),
                now64(3),
                map('platform', arrayElement(['web', 'ios', 'android'], 1 + rand() % 3),
                    'country', arrayElement(['US', 'IN', 'DE', 'BR', 'JP'], 1 + rand() % 5)),
                generateUUIDv4()
            FROM (SELECT rand() / 4294967295 AS r FROM numbers({n}))
            """
        )
        done += n
        print(f"  generated {done:,} / {rows:,} events", flush=True)


async def cmd_setup(args: argparse.Namespace) -> None:
    if CONFIG.exists() and not args.reset:
        sys.exit(f"{CONFIG.name} exists: the benchmark is already set up (use --reset to rebuild)")
    env = bench_env()
    os.environ.update(env)  # in-process service calls below use the bench databases

    print("creating isolated databases ...")
    await _create_postgres_db(env, args.reset)
    ch_admin = _clickhouse(env, "default")
    if args.reset:
        ch_admin.command(f"DROP DATABASE IF EXISTS {BENCH_DB} SYNC")
    ch_admin.command(f"CREATE DATABASE IF NOT EXISTS {BENCH_DB}")

    print("migrating ...")
    _run([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    _run([sys.executable, "-m", "pulse.clickhouse_migrations"], env)

    from pulse.models import ApiKeyType, User
    from pulse.repositories.postgres import session_scope
    from pulse.services import api_keys, orgs, projects

    async with session_scope() as session:
        user = User(email="bench@example.com", password_hash="not-a-real-hash", name="Bench")
        session.add(user)
        await session.commit()
    org = await orgs.create_organization("Bench Org", "bench-org", user.id)
    large = await projects.create_project(org.id, "Bench Large", "bench-large", "UTC", user.id)
    medium = await projects.create_project(org.id, "Bench Medium", "bench-medium", "UTC", user.id)
    ingest = await projects.create_project(org.id, "Bench Ingest", "bench-ingest", "UTC", user.id)
    smalls = [
        await projects.create_project(
            org.id, f"Bench Small {i}", f"bench-small-{i}", "UTC", user.id
        )
        for i in range(args.small_projects)
    ]
    # A read key is scoped to exactly one project (resolve_query_scope), so
    # each project that queries run against needs its own key.
    _, large_read_key = await api_keys.create_api_key(org.id, large.id, ApiKeyType.READ, user.id)
    _, medium_read_key = await api_keys.create_api_key(org.id, medium.id, ApiKeyType.READ, user.id)
    _, write_key = await api_keys.create_api_key(org.id, ingest.id, ApiKeyType.WRITE, user.id)

    ch = _clickhouse(env, BENCH_DB)
    print(f"generating {args.events:,} events for the large project ...")
    started = time.monotonic()
    _generate_events(ch, str(org.id), str(large.id), args.events, args.users, args.days)
    print(f"generating {args.medium_events:,} events for the medium project ...")
    _generate_events(
        ch, str(org.id), str(medium.id), args.medium_events, args.users // 10, args.days
    )
    for small in smalls:
        _generate_events(ch, str(org.id), str(small.id), 100_000, 5_000, args.days)
    print(f"  done in {time.monotonic() - started:.0f}s")

    def sized(table: str) -> tuple[int, int]:
        rows, size = ch.query(
            "SELECT sum(rows), sum(bytes_on_disk) FROM system.parts "
            f"WHERE database = '{BENCH_DB}' AND table = '{table}' AND active"
        ).result_rows[0]
        return int(rows), int(size)

    events_rows, events_bytes = sized("events")
    rollup_rows, rollup_bytes = sized("event_hourly")
    CONFIG.write_text(
        json.dumps(
            {
                "org_id": str(org.id),
                "large_project_id": str(large.id),
                "medium_project_id": str(medium.id),
                "medium_project_events": args.medium_events,
                "ingest_project_id": str(ingest.id),
                "large_read_key": large_read_key,
                "medium_read_key": medium_read_key,
                "write_key": write_key,
                "days": args.days,
                "large_project_events": args.events,
                "users": args.users,
                "events_rows": events_rows,
                "events_bytes": events_bytes,
                "rollup_rows": rollup_rows,
                "rollup_bytes": rollup_bytes,
            },
            indent=2,
        )
    )
    print(
        f"events:       {events_rows:>12,} rows  {events_bytes / 1e6:>8.1f} MB\n"
        f"event_hourly: {rollup_rows:>12,} rows  {rollup_bytes / 1e6:>8.1f} MB  "
        f"({events_rows / max(rollup_rows, 1):.0f}x fewer rows)"
    )


# ----------------------------------------------------------------------- run


def _require_port_free() -> None:
    """A stale server on the port would answer the health check and the warm-up
    with the *wrong* database, and the run would measure (and fail against)
    something else entirely."""
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            sys.exit("port 8000 is already in use: stop that server before benchmarking")


def _kill_tree(proc: subprocess.Popen) -> None:
    """On Windows the venv launcher and the real server are separate processes,
    and terminate() only reaches the launcher -- leaving the server running."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        proc.terminate()


def _wait_healthy(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    sys.exit("the API did not become healthy")


def _post(path: str, body: dict, key: str) -> None:
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    urllib.request.urlopen(request, timeout=60).read()


def _warm_up(config: dict, project: str) -> None:
    """Three passes of every query, so the first measured requests aren't paying
    for cold connections and cold ClickHouse marks."""
    os.environ["BENCH_CONFIG"] = str(CONFIG)
    sys.path.insert(0, str(HERE))
    import locustfile  # noqa: PLC0415  (needs BENCH_CONFIG/BENCH_PROJECT set first)

    base = locustfile._QUERY_BASE
    read_key = config[f"{project}_read_key"]
    for _ in range(3):
        for endpoint, spec, _eligible in locustfile._QUERIES.values():
            try:
                _post(f"{base}/{endpoint}?refresh=true", spec, read_key)
            except urllib.error.HTTPError:
                pass  # e.g. refused by the row cap on a "rollups off" run: that is a result


def _landed(ch, project_id: str) -> int:
    return int(
        ch.query(
            "SELECT count() FROM events WHERE project_id = {p:UUID}", parameters={"p": project_id}
        ).result_rows[0][0]
    )


def _read_stats(prefix: Path) -> dict[str, dict]:
    with open(f"{prefix}_stats.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    stats = {}
    for row in rows:
        stats[row["Name"]] = {
            "requests": int(row["Request Count"]),
            "failures": int(row["Failure Count"]),
            "rps": float(row["Requests/s"]),
            "avg_ms": float(row["Average Response Time"]),
            "p50_ms": float(row["50%"]),
            "p95_ms": float(row["95%"]),
            "p99_ms": float(row["99%"]),
            "max_ms": float(row["Max Response Time"]),
        }
    return stats


def cmd_run(args: argparse.Namespace) -> None:
    if not CONFIG.exists():
        sys.exit("run `bench.py setup` first")
    config = json.loads(CONFIG.read_text())
    env = bench_env()
    env["QUERY_ROLLUPS_ENABLED"] = "true" if args.rollups == "on" else "false"
    env["BENCH_CONFIG"] = str(CONFIG)
    env["LOAD_SCENARIO"] = args.scenario
    env["EXPECT_ROLLUPS"] = args.rollups
    env["INGEST_BATCH"] = str(args.batch)
    env["BENCH_PROJECT"] = args.project
    env["BENCH_ROUTING_OUT"] = str(RESULTS / f"{args.label}_routing.json")
    env["QUERY_SUBSET"] = args.subset
    RESULTS.mkdir(exist_ok=True)
    prefix = RESULTS / args.label

    import redis

    redis.Redis.from_url(env["REDIS_URL"]).flushdb()  # bench DB only: cache, stream, dedup

    _require_port_free()
    mv_dropped = False
    if args.mv == "off":
        _clickhouse(env, BENCH_DB).command("DROP VIEW IF EXISTS mv_event_hourly")
        mv_dropped = True
    procs: list[subprocess.Popen] = []

    def spawn(cmd: list[str], extra: dict[str, str] | None = None) -> subprocess.Popen:
        debug = os.environ.get("BENCH_DEBUG") == "1"
        proc = subprocess.Popen(
            cmd,
            cwd=BACKEND,
            env={**env, **(extra or {})},
            stdout=None if debug else subprocess.DEVNULL,
            stderr=None if debug else subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    try:
        spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pulse.main:app",
                "--port",
                "8000",
                "--no-access-log",
                "--log-level",
                "warning",
                "--workers",
                str(args.api_workers),
            ]
        )
        _wait_healthy()
        if args.scenario == "ingest":
            for i in range(args.workers):
                spawn(
                    [sys.executable, "-m", "pulse.worker.main"],
                    {"WORKER_CONSUMER_NAME": f"bench-worker-{i}"},
                )
        else:
            print("warming up ...", flush=True)
            os.environ["BENCH_PROJECT"] = args.project
            _warm_up(config, args.project)

        ch = _clickhouse(env, BENCH_DB)
        before = _landed(ch, config["ingest_project_id"])
        started = time.monotonic()

        print(
            f"running {args.scenario} for {args.duration}s with {args.users} users ...", flush=True
        )
        locust_run = subprocess.run(
            [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(HERE / "locustfile.py"),
                "--headless",
                "--host",
                API,
                "-u",
                str(args.users),
                "-r",
                str(args.users),
                "-t",
                f"{args.duration}s",
                "--csv",
                str(prefix),
                "--reset-stats",
            ],
            cwd=BACKEND,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if locust_run.returncode not in (0, 1):
            # 1 just means "locust recorded at least one non-2xx response" --
            # expected and informative here (e.g. a large ad-hoc query correctly
            # refused by the row cap, SPEC.md 1.4). Anything else is a real
            # harness problem (locust itself crashed, bad args, etc).
            sys.exit(f"locust exited {locust_run.returncode}: rerun with BENCH_DEBUG=1 to see why")
        stats = _read_stats(prefix)
        result: dict = {
            "label": args.label,
            "scenario": args.scenario,
            "rollups": args.rollups,
            "mv": args.mv,
            "subset": args.subset,
            "users": args.users,
            "duration_s": args.duration,
            "stats": stats,
        }

        if args.scenario == "ingest":
            accepted = (stats["ingest"]["requests"] - stats["ingest"]["failures"]) * args.batch
            print(f"accepted {accepted:,} events; waiting for the workers to land them ...")
            deadline, last, stalled_since = time.monotonic() + 600, -1, time.monotonic()
            while time.monotonic() < deadline:
                landed = _landed(ch, config["ingest_project_id"]) - before
                if landed >= accepted:
                    break
                if landed != last:
                    last, stalled_since = landed, time.monotonic()
                elif time.monotonic() - stalled_since > 60:
                    break
                time.sleep(1)
            landed = _landed(ch, config["ingest_project_id"]) - before
            elapsed = time.monotonic() - started
            result["ingest"] = {
                "batch": args.batch,
                "workers": args.workers,
                "accepted_events": accepted,
                "landed_events": landed,
                "lost_events": accepted - landed,
                "accept_events_per_s": accepted / args.duration,
                "end_to_end_events_per_s": landed / elapsed,
                "seconds_until_fully_landed": elapsed,
            }
        routing_file = RESULTS / f"{args.label}_routing.json"
        if routing_file.exists():
            result["routing"] = json.loads(routing_file.read_text())
            routing_file.unlink()
        (RESULTS / f"{args.label}.json").write_text(json.dumps(result, indent=2))
        _print_run(result)
    finally:
        if mv_dropped:
            from pulse.rollups.maintenance import recreate_view

            recreate_view(_clickhouse(env, BENCH_DB))
        for proc in procs:
            _kill_tree(proc)
        for proc in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


# -------------------------------------------------------------------- report


def _print_run(result: dict) -> None:
    print(
        f"\n## {result['label']}  ({result['scenario']}, rollups {result['rollups']}, "
        f"{result['users']} users, {result['duration_s']}s)"
    )
    print("| request | count | fail | req/s | p50 ms | p95 ms | p99 ms | max ms |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, s in sorted(result["stats"].items()):
        if name == "Aggregated":
            continue
        print(
            f"| {name} | {s['requests']} | {s['failures']} | {s['rps']:.1f} | {s['p50_ms']:.0f} "
            f"| {s['p95_ms']:.0f} | {s['p99_ms']:.0f} | {s['max_ms']:.0f} |"
        )
    for row in result.get("routing", []):
        print(
            f"  routing: {row['query']:<20} {row['source']:<7} "
            f"approximate={row['approximate']!s:<5} x{row['count']}"
        )
    if "ingest" in result:
        print("\n" + json.dumps(result["ingest"], indent=2))


def cmd_report(_: argparse.Namespace) -> None:
    for path in sorted(RESULTS.glob("*.json")):
        _print_run(json.loads(path.read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup")
    setup.add_argument("--events", type=int, default=50_000_000, help="large project")
    setup.add_argument("--medium-events", type=int, default=5_000_000)
    setup.add_argument("--users", type=int, default=200_000, help="large project")
    setup.add_argument("--days", type=int, default=90)
    setup.add_argument("--small-projects", type=int, default=3)
    setup.add_argument("--reset", action="store_true")

    run = commands.add_parser("run")
    run.add_argument("--scenario", choices=["query", "ingest"], required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--project", choices=["large", "medium"], default="large")
    run.add_argument(
        "--subset",
        choices=["dashboard", "adhoc", "mixed"],
        default="mixed",
        help="which queries run: SPEC.md 1.4 states separate targets for each",
    )
    run.add_argument("--rollups", choices=["on", "off"], default="on")
    run.add_argument("--users", type=int, default=10)
    run.add_argument("--duration", type=int, default=60)
    run.add_argument("--batch", type=int, default=100, help="events per /ingest request")
    run.add_argument("--workers", type=int, default=1, help="ingestion worker processes")
    run.add_argument("--api-workers", type=int, default=1)
    run.add_argument(
        "--mv",
        choices=["on", "off"],
        default="on",
        help="off: drop the rollup view for the run (its cost to ingestion), then restore it",
    )

    commands.add_parser("report")

    args = parser.parse_args()
    if args.command == "setup":
        asyncio.run(cmd_setup(args))
    elif args.command == "run":
        cmd_run(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()

"""`python -m pulse.rollups verify [--project ID]` / `python -m pulse.rollups rebuild`."""

import argparse
import asyncio
import sys
import uuid

from pulse.repositories import clickhouse
from pulse.rollups import maintenance


async def _run(args: argparse.Namespace) -> int:
    client = await clickhouse.get_client()
    try:
        if args.command == "verify":
            report = await maintenance.verify(
                client, project_id=uuid.UUID(args.project) if args.project else None
            )
            if report.ok:
                print("rollup matches events: no drift")
                return 0
            print(f"DRIFT in {report.total_mismatched_hours} (event, hour) buckets; first few:")
            for m in report.sample:
                print(
                    f"  {m.project_id} {m.event_name!r} {m.hour}: events {m.raw_events} raw vs "
                    f"{m.rollup_events} rollup, users {m.raw_users} raw vs {m.rollup_users} rollup"
                )
            print(
                "repair with: python -m pulse.rollups rebuild  (stop the ingestion workers first)"
            )
            return 1

        print("rebuilding the rollup -- the ingestion workers should be stopped")
        rows = await maintenance.rebuild(client)
        print(f"rebuilt: {rows} rollup rows")
        return 0
    finally:
        await clickhouse.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m pulse.rollups")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="compare the rollup with the raw events")
    verify.add_argument("--project", help="limit the check to one project id")
    commands.add_parser("rebuild", help="recompute the rollup from the raw events")
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()

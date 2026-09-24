"""Phase 23 migrations-on-deploy: `python -m pulse.migrate` brings BOTH stores
to their latest schema -- Postgres (alembic) and then ClickHouse -- in one
step, so a deploy's pre-deploy hook is a single command.

Order matters and is fixed: Postgres first. Neither store's migrations depend
on the other's, but failing on the smaller, transactional-DDL store before
touching the ClickHouse one keeps a bad release from half-applying the store
that can't roll DDL back. Both are idempotent, so re-running after a partial
failure (or on every deploy) is the normal case, not a special one.

Any failure propagates as a non-zero exit: the platform treats a failed
pre-deploy command as a failed deploy and keeps the previous version serving,
which is exactly the behavior wanted when a migration is bad. Deliberately a
Python entrypoint rather than a shell one-liner -- the platform's docker
commands are exec'd without a shell, so `a && b` doesn't work there."""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.__main__ import main as migrate_clickhouse

logger = logging.getLogger("pulse.migrate")


def _project_root() -> Path:
    """The directory holding alembic.ini. Next to the package in a source
    checkout, but the deployed image runs the app from /app while the package
    may be imported from site-packages, so the working directory is checked
    too instead of trusting __file__."""
    for candidate in (Path(__file__).resolve().parent.parent, Path.cwd()):
        if (candidate / "alembic.ini").exists():
            return candidate
    raise RuntimeError("alembic.ini not found next to the package or in the working directory")


def upgrade_postgres() -> None:
    root = _project_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("migrating Postgres (alembic upgrade head)")
    upgrade_postgres()
    logger.info("migrating ClickHouse")
    asyncio.run(migrate_clickhouse())
    logger.info("migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

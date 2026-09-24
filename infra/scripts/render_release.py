"""Releases a commit to a set of Render services, in order (Phase 23).

The order is the point. The FIRST service id is the API: it owns the
pre-deploy command that migrates Postgres and ClickHouse, so it must reach
`live` -- migrations applied, health check passing -- before anything else is
rolled. Only then are the workers (which assume the migrated schema) and the
frontend released, together. Any failed or timed-out deploy aborts the whole
release with a non-zero exit, so a bad API deploy never gets workers pointed at
it.

Talks to Render's REST API with the standard library only, so the workflow
needs no dependencies. Usage:

    RENDER_API_KEY=... python render_release.py <commit-sha> <api-id> [<other-id> ...]
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from typing import Protocol

API = "https://api.render.com/v1"
FAILED = {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}


class DeployFailed(Exception):
    pass


class RenderClient(Protocol):
    def trigger(self, service_id: str, commit: str) -> str: ...
    def status(self, service_id: str, deploy_id: str) -> str: ...


class HttpRenderClient:
    def __init__(self, api_key: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, body: dict[str, str] | None = None) -> dict[str, str]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, headers=self._headers, method=method
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result: dict[str, str] = json.load(response)
            return result

    def trigger(self, service_id: str, commit: str) -> str:
        return self._call("POST", f"/services/{service_id}/deploys", {"commitId": commit})["id"]

    def status(self, service_id: str, deploy_id: str) -> str:
        return self._call("GET", f"/services/{service_id}/deploys/{deploy_id}")["status"]


def wait_until_live(
    client: RenderClient,
    deploys: dict[str, str],
    *,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Blocks until every {service_id: deploy_id} is live. Raises DeployFailed on
    a failed deploy or when the timeout passes -- never returns quietly."""
    deadline = clock() + timeout_s
    pending = dict(deploys)
    while pending:
        for service_id, deploy_id in list(pending.items()):
            status = client.status(service_id, deploy_id)
            if status == "live":
                del pending[service_id]
            elif status in FAILED:
                raise DeployFailed(f"{service_id}: deploy {deploy_id} ended as {status}")
        if pending:
            if clock() >= deadline:
                raise DeployFailed(f"timed out waiting for: {', '.join(sorted(pending))}")
            sleep(poll_s)


def release(
    client: RenderClient,
    commit: str,
    service_ids: Sequence[str],
    *,
    timeout_s: float = 1200,
    poll_s: float = 10,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    if not service_ids:
        raise ValueError("at least the API service id is required")
    api_id, *others = service_ids

    wait_until_live(
        client,
        {api_id: client.trigger(api_id, commit)},
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep=sleep,
        clock=clock,
    )
    if others:
        wait_until_live(
            client,
            {service_id: client.trigger(service_id, commit) for service_id in others},
            timeout_s=timeout_s,
            poll_s=poll_s,
            sleep=sleep,
            clock=clock,
        )


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    commit, service_ids = argv[1], argv[2:]
    try:
        release(HttpRenderClient(os.environ["RENDER_API_KEY"]), commit, service_ids)
    except DeployFailed as exc:
        print(f"::error title=Release failed::{exc}")
        return 1
    print(f"released {commit} to {len(service_ids)} service(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

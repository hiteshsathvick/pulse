"""Releases a commit to a set of Render services, in order (Phase 23), and rolls
back if the release fails (Phase 24).

The order is the point. The FIRST service id is the API: it owns the
pre-deploy command that migrates Postgres and ClickHouse, so it must reach
`live` -- migrations applied, health check passing -- before anything else is
rolled. Only then are the workers (which assume the migrated schema) and the
frontend released, together. Any failed or timed-out deploy aborts the whole
release with a non-zero exit, so a bad API deploy never gets workers pointed at
it.

Rollback. Before releasing, the script records each service's currently live
deploy. If anything fails, it waits for any in-flight deploys to settle, then rolls
every service that had already gone live back to its recorded deploy (workers
first, API last) and waits for those rollbacks to go live. The release still exits
non-zero. What it cannot undo: database migrations are forward-only, so a
rolled-back version must tolerate the migrated schema (expand, then contract --
docs/DEPLOYMENT.md). A rollback that itself fails is reported as such, loudly.

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
from typing import Any, Protocol

API = "https://api.render.com/v1"
FAILED = {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}

_FORWARD_ONLY = (
    "Database migrations are forward-only and were NOT rolled back; the previous "
    "version must tolerate the migrated schema."
)


class DeployFailed(Exception):
    pass


class RenderClient(Protocol):
    def trigger(self, service_id: str, commit: str) -> str: ...
    def status(self, service_id: str, deploy_id: str) -> str: ...
    def live_deploy(self, service_id: str) -> str | None: ...
    def rollback(self, service_id: str, deploy_id: str) -> str: ...


class HttpRenderClient:
    def __init__(self, api_key: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, body: dict[str, str] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, headers=self._headers, method=method
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def trigger(self, service_id: str, commit: str) -> str:
        result = self._call("POST", f"/services/{service_id}/deploys", {"commitId": commit})
        return str(result["id"])

    def status(self, service_id: str, deploy_id: str) -> str:
        return str(self._call("GET", f"/services/{service_id}/deploys/{deploy_id}")["status"])

    def live_deploy(self, service_id: str) -> str | None:
        """The deploy currently serving traffic, or None for a service that has
        never had one (a first release: nothing to roll back to)."""
        rows = self._call("GET", f"/services/{service_id}/deploys?limit=20")
        for row in rows:
            if row["deploy"]["status"] == "live":
                return str(row["deploy"]["id"])
        return None

    def rollback(self, service_id: str, deploy_id: str) -> str:
        result = self._call("POST", f"/services/{service_id}/rollback", {"deployId": deploy_id})
        return str(result["id"])


def wait_until_live(
    client: RenderClient,
    deploys: dict[str, str],
    *,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    live: set[str] | None = None,
) -> None:
    """Blocks until every {service_id: deploy_id} is live. Raises DeployFailed on
    a failed deploy or when the timeout passes -- never returns quietly. Each
    service that goes live is added to `live` as it does, so a caller can tell
    afterwards which ones had already been released when something failed."""
    deadline = clock() + timeout_s
    pending = dict(deploys)
    while pending:
        for service_id, deploy_id in list(pending.items()):
            status = client.status(service_id, deploy_id)
            if status == "live":
                if live is not None:
                    live.add(service_id)
                del pending[service_id]
            elif status in FAILED:
                raise DeployFailed(f"{service_id}: deploy {deploy_id} ended as {status}")
        if pending:
            if clock() >= deadline:
                raise DeployFailed(f"timed out waiting for: {', '.join(sorted(pending))}")
            sleep(poll_s)


def _settle(
    client: RenderClient,
    deploys: dict[str, str],
    live: set[str],
    *,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Waits for in-flight deploys to reach a terminal state WITHOUT raising,
    recording which went live. Used before a rollback: rolling back a service
    whose new deploy is still building would race it."""
    deadline = clock() + timeout_s
    pending = dict(deploys)
    while pending:
        for service_id, deploy_id in list(pending.items()):
            status = client.status(service_id, deploy_id)
            if status == "live":
                live.add(service_id)
                del pending[service_id]
            elif status in FAILED:
                del pending[service_id]
        if pending:
            if clock() >= deadline:
                return
            sleep(poll_s)


def _roll_back(
    client: RenderClient,
    service_ids: Sequence[str],
    previous: dict[str, str | None],
    *,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str:
    """Returns a one-line account of what happened, for the failure message."""
    if not service_ids:
        return "Nothing had gone live, so there was nothing to roll back."
    started: dict[str, str] = {}
    stranded: list[str] = []
    for service_id in service_ids:
        target = previous.get(service_id)
        if target is None:
            stranded.append(service_id)
            continue
        started[service_id] = client.rollback(service_id, target)
    try:
        wait_until_live(
            client, started, timeout_s=timeout_s, poll_s=poll_s, sleep=sleep, clock=clock
        )
    except DeployFailed as exc:
        return f"ROLLBACK FAILED ({exc}) -- intervene manually. {_FORWARD_ONLY}"
    parts = [f"Rolled back {', '.join(started) or 'nothing'} to the previously live deploy."]
    if stranded:
        parts.append(
            f"No previous live deploy for {', '.join(stranded)} (a first release), "
            "so they were left on the new version."
        )
    parts.append(_FORWARD_ONLY)
    return " ".join(parts)


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
    timing: dict[str, Any] = {
        "timeout_s": timeout_s,
        "poll_s": poll_s,
        "sleep": sleep,
        "clock": clock,
    }

    # Recorded BEFORE anything changes: once the new deploy is live, "the previous
    # one" is no longer distinguishable from the list.
    previous = {service_id: client.live_deploy(service_id) for service_id in service_ids}
    triggered: dict[str, str] = {}
    live: set[str] = set()
    try:
        triggered[api_id] = client.trigger(api_id, commit)
        wait_until_live(client, {api_id: triggered[api_id]}, live=live, **timing)
        if others:
            for service_id in others:
                triggered[service_id] = client.trigger(service_id, commit)
            wait_until_live(client, {s: triggered[s] for s in others}, live=live, **timing)
    except DeployFailed as failure:
        _settle(client, {s: d for s, d in triggered.items() if s not in live}, live, **timing)
        # Workers first, API last: the reverse of the release order.
        to_roll_back = [s for s in reversed(service_ids) if s in live]
        outcome = _roll_back(client, to_roll_back, previous, **timing)
        raise DeployFailed(f"{failure}. {outcome}") from failure


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

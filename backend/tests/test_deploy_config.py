"""Phase 23: the deploy configuration is code, so it gets tests. Everything
here is static (no cloud, no Docker) and is aimed at mistakes that otherwise
surface only during a real deploy: a typo'd module in a worker command, an env
var Terraform writes that the app never reads, a dashboard querying a metric
that doesn't exist, a secret committed as a plain value, staging and prod
drifting apart."""

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import REGISTRY

from pulse.core.config import Settings
from pulse.observability import metrics  # noqa: F401  (registers the metrics the dashboard queries)

_ROOT = Path(__file__).resolve().parents[2]
_RENDER = _ROOT / "infra" / "render"
_TERRAFORM = _ROOT / "infra" / "terraform"
_ENVS = ("staging", "prod")


def _blueprint(env: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((_RENDER / f"{env}.render.yaml").read_text())
    return data


def _services(env: str) -> dict[str, dict[str, Any]]:
    """Keyed by name with the environment prefix removed, so the two
    environments can be compared like for like."""
    prefix = f"pulse-{env}-"
    return {s["name"].removeprefix(prefix): s for s in _blueprint(env)["services"]}


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Render Blueprints ---------------------------------------------------------


def test_staging_and_prod_define_the_same_services_the_same_way() -> None:
    staging, prod = _services("staging"), _services("prod")
    assert set(staging) == set(prod)
    for name in staging:
        for field in ("type", "runtime", "dockerfilePath", "dockerContext", "dockerCommand"):
            assert staging[name].get(field) == prod[name].get(field), (name, field)
        staging_keys = {e.get("key") or e.get("fromGroup") for e in staging[name]["envVars"]}
        prod_keys = {e.get("key") or e.get("fromGroup") for e in prod[name]["envVars"]}
        assert {k.replace("staging", "ENV") for k in staging_keys} == {
            k.replace("prod", "ENV") for k in prod_keys
        }, name


def test_staging_deploys_when_ci_passes_and_prod_never_auto_deploys() -> None:
    """The DoD's two halves: merge -> staging automatically; prod only by a
    deliberate, gated action."""
    assert {s["autoDeployTrigger"] for s in _services("staging").values()} == {"checksPass"}
    assert {s["autoDeployTrigger"] for s in _services("prod").values()} == {"off"}


@pytest.mark.parametrize("env", _ENVS)
def test_each_service_pulls_only_the_env_group_it_needs(env: str) -> None:
    """The managed group holds database, ClickHouse and signing secrets. The API
    and workers need them. The frontend and Grafana are browser-facing and must
    not be handed credentials they have no use for; Prometheus needs just the
    metrics token, so it gets its own one-secret group instead."""
    for name, service in _services(env).items():
        groups = [e["fromGroup"] for e in service["envVars"] if "fromGroup" in e]
        if name in ("frontend", "grafana"):
            expected: list[str] = []
        elif name == "prometheus":
            expected = [f"pulse-{env}-observability"]
        else:
            expected = [f"pulse-{env}-managed"]
        assert groups == expected, name


@pytest.mark.parametrize("env", _ENVS)
def test_no_secret_is_committed_as_a_plain_value(env: str) -> None:
    sensitive = re.compile(r"SECRET|PASSWORD|TOKEN|DSN|KEY|URL", re.IGNORECASE)
    for name, service in _services(env).items():
        for var in service["envVars"]:
            key = var.get("key")
            if key and sensitive.search(key) and key != "PORT":
                assert var.get("sync") is False, f"{name}: {key} must be sync:false, not a value"


@pytest.mark.parametrize("env", _ENVS)
def test_the_api_migrates_both_stores_before_serving_and_has_a_health_check(env: str) -> None:
    api = _services(env)["api"]
    assert api["preDeployCommand"] == "python -m pulse.migrate"
    assert api["healthCheckPath"] == "/health"
    assert next(e["value"] for e in api["envVars"] if e.get("key") == "PORT") == "8000"


@pytest.mark.parametrize("env", _ENVS)
def test_every_worker_command_points_at_a_real_module(env: str) -> None:
    """A typo'd `python -m pulse.wroker.main` would only fail after deploying."""
    commands = [s["dockerCommand"] for s in _services(env).values() if "dockerCommand" in s]
    commands.append(_services(env)["api"]["preDeployCommand"])
    assert len(commands) == 5  # four workers + the migrate hook
    for command in commands:
        prefix, module = command.split()[:2], command.split()[2]
        assert prefix == ["python", "-m"], command
        assert importlib.util.find_spec(module) is not None, f"{command}: no such module"


# --- Deployed observability (Phase 24) ------------------------------------------

_OBS = _ROOT / "infra" / "observability"


def _env_map(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in service["envVars"] if "key" in e}


@pytest.mark.parametrize("env", _ENVS)
def test_workers_are_private_services_serving_metrics_on_the_scraped_port(env: str) -> None:
    """Render background workers can't receive private-network traffic, so a
    `worker` could never be scraped. As private services they can -- provided the
    port Render routes to (PORT) is the port the metrics server binds."""
    template = (_OBS / "prometheus.render.yml").read_text()
    for name in ("ingest-worker", "alert-worker", "billing-worker", "retention-worker"):
        service = _services(env)[name]
        assert service["type"] == "pserv", name
        envs = _env_map(service)
        assert envs["PORT"]["value"] == envs["METRICS_PORT"]["value"] == "9100", name
        assert f"@@HOST_{name.upper().replace('-', '_')}@@-discovery" in template
    assert template.count("port: 9100") == 4


@pytest.mark.parametrize("env", _ENVS)
def test_prometheus_is_handed_every_value_its_config_template_needs(env: str) -> None:
    template = (_OBS / "prometheus.render.yml").read_text()
    markers = set(re.findall(r"@@([A-Z_]+)@@", template))
    prometheus = _services(env)["prometheus"]
    provided = set(_env_map(prometheus)) - {"PORT"}
    # METRICS_TOKEN arrives through the observability env group, not a key.
    assert markers == provided | {"METRICS_TOKEN"}
    # ...and the entrypoint refuses to start if any marker is unset.
    entrypoint = (_OBS / "prometheus-render-entrypoint.sh").read_text()
    for marker in markers:
        assert f"@@{marker}@@" in entrypoint and marker in entrypoint.split("sed", 1)[0]


@pytest.mark.parametrize("env", _ENVS)
def test_every_fromservice_reference_names_a_real_service_of_that_type(env: str) -> None:
    blueprint = _blueprint(env)
    by_name = {s["name"]: s for s in blueprint["services"]}
    checked = 0
    for service in blueprint["services"]:
        for var in service["envVars"]:
            ref = var.get("fromService")
            if ref:
                checked += 1
                assert ref["name"] in by_name, (service["name"], ref)
                assert by_name[ref["name"]]["type"] == ref["type"], (service["name"], ref)
    assert checked == 6  # five Prometheus targets + Grafana's Prometheus address


@pytest.mark.parametrize("env", _ENVS)
def test_the_prometheus_api_scrape_uses_the_apis_primary_port_and_a_bearer_token(env: str) -> None:
    """Only a web service's primary port is reachable over the private network, so
    the API's metrics are a token-protected route on that port, not port 9100."""
    template = yaml.safe_load((_OBS / "prometheus.render.yml").read_text())["scrape_configs"]
    api_job = next(j for j in template if j["job_name"] == "pulse-api")
    assert api_job["dns_sd_configs"][0]["port"] == int(
        _env_map(_services(env)["api"])["PORT"]["value"]
    )
    assert api_job["authorization"] == {"type": "Bearer", "credentials": "@@METRICS_TOKEN@@"}
    # Every other job is a worker: no credentials in its config.
    assert all("authorization" not in j for j in template if j is not api_job)


def test_the_deployed_scrape_jobs_match_the_local_ones() -> None:
    """Same job names locally and deployed, so a dashboard or alert written
    against one works against the other."""
    local = {
        j["job_name"]
        for j in yaml.safe_load((_OBS / "prometheus.yml").read_text())["scrape_configs"]
    }
    deployed = {
        j["job_name"]
        for j in yaml.safe_load((_OBS / "prometheus.render.yml").read_text())["scrape_configs"]
    }
    assert local == deployed


@pytest.mark.parametrize("env", _ENVS)
def test_grafana_is_login_protected_and_its_password_is_not_committed(env: str) -> None:
    envs = _env_map(_services(env)["grafana"])
    assert envs["GF_AUTH_ANONYMOUS_ENABLED"]["value"] == "false"  # local compose enables it
    assert envs["GF_SECURITY_ADMIN_PASSWORD"].get("sync") is False
    assert "value" not in envs["GF_SECURITY_ADMIN_PASSWORD"]


def test_the_deployed_grafana_datasource_keeps_the_uid_the_dashboard_uses() -> None:
    deployed = yaml.safe_load((_OBS / "grafana" / "render" / "datasources.yaml").read_text())
    dashboard = (_OBS / "grafana" / "dashboards" / "pulse-health.json").read_text()
    (source,) = deployed["datasources"]
    assert source["uid"] == "prometheus"
    assert '"uid": "prometheus"' in dashboard
    assert "${PROMETHEUS_HOSTPORT}" in source["url"]  # not a hard-coded hostname


def test_shell_scripts_are_pinned_to_lf_line_endings() -> None:
    """A CRLF shebang fails inside a Linux container ("not found"), and this repo
    is developed on Windows with core.autocrlf on."""
    assert "*.sh text eol=lf" in (_ROOT / ".gitattributes").read_text()
    assert b"\r\n" not in (_OBS / "prometheus-render-entrypoint.sh").read_bytes()


def test_terraform_shares_one_metrics_token_between_the_api_and_prometheus() -> None:
    text = (_TERRAFORM / "main.tf").read_text()
    managed = text.split('resource "render_env_group" "managed"', 1)[1].split(
        'resource "render_env_group" "observability"', 1
    )[0]
    observability = text.split('resource "render_env_group" "observability"', 1)[1]
    assert "METRICS_TOKEN   = { value = random_password.metrics_token.result }" in managed
    # The observability group holds the token and NOTHING else.
    assert re.findall(r"^\s{4}([A-Z_]+)\s*=", observability, re.MULTILINE) == ["METRICS_TOKEN"]
    # The entrypoint substitutes it with sed, so it must be letters and digits only.
    token = text.split('resource "random_password" "metrics_token"', 1)[1].split("}", 1)[0]
    assert "special = false" in token


# --- Terraform -----------------------------------------------------------------


def _terraform_env_group_keys() -> set[str]:
    text = (_TERRAFORM / "main.tf").read_text()
    block = text.split('resource "render_env_group" "managed"', 1)[1]
    return set(re.findall(r"^\s+([A-Z][A-Z0-9_]+)\s+=\s+\{", block, flags=re.MULTILINE))


def test_every_env_var_terraform_hands_to_render_is_a_real_setting() -> None:
    """The env group is the app's whole configuration in a deployed
    environment. A misspelled key would be silently ignored (extra="ignore")
    and the app would run on a dev default instead -- e.g. the dev JWT secret."""
    settings_fields = {name.upper() for name in Settings.model_fields}
    unknown = _terraform_env_group_keys() - settings_fields
    assert not unknown, f"Terraform writes vars the app never reads: {sorted(unknown)}"


def test_terraform_provides_every_secret_and_connection_the_app_needs_to_run() -> None:
    required = {
        "DATABASE_URL",
        "DATABASE_BOOTSTRAP_URL",
        "REDIS_URL",
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_SECURE",
        "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "JWT_SECRET",
        "PII_HASH_SECRET",
    }
    assert required <= _terraform_env_group_keys()


def test_redis_never_evicts_because_the_ingest_stream_is_the_only_copy() -> None:
    text = (_TERRAFORM / "main.tf").read_text()
    keyvalue = text.split('resource "render_keyvalue" "cache"', 1)[1].split("\n}\n", 1)[0]
    assert 'max_memory_policy = "noeviction"' in keyvalue


def test_the_app_never_connects_as_the_database_owner() -> None:
    """RLS is bypassed for superusers/owners, so DATABASE_URL must use pulse_app
    and only the bootstrap URL may use the owner."""
    text = (_TERRAFORM / "main.tf").read_text()
    assert 'database_url           = "postgresql+asyncpg://pulse_app:' in text


def test_prod_clickhouse_cannot_be_opened_to_the_world_by_default() -> None:
    assert "clickhouse_ip_allow_list = []" in (_TERRAFORM / "prod.tfvars").read_text()
    assert "length(var.clickhouse_ip_allow_list) > 0" in (_TERRAFORM / "main.tf").read_text()


def test_terraform_lockfile_is_committed_and_state_is_ignored() -> None:
    assert (_TERRAFORM / ".terraform.lock.hcl").exists()
    gitignore = (_ROOT / ".gitignore").read_text()
    assert "*.tfstate" in gitignore
    assert "infra/terraform/.terraform/" in gitignore


# --- Workflows -----------------------------------------------------------------


def test_prod_deploy_is_manual_only_and_gated_by_an_environment() -> None:
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text())
    triggers = workflow[True]  # PyYAML reads the bare key `on:` as boolean True
    assert list(triggers) == ["workflow_dispatch"]
    assert workflow["jobs"]["deploy"]["environment"]["name"] == "production"
    assert "verify-ci" in workflow["jobs"]["deploy"]["needs"]


# --- Observability config ------------------------------------------------------


def test_every_metric_the_dashboard_queries_actually_exists() -> None:
    dashboard = json.loads(
        (_ROOT / "infra/observability/grafana/dashboards/pulse-health.json").read_text()
    )
    # Metric *families*, not samples: a histogram nobody has observed yet has a
    # family but no samples, and must still count as existing.
    families = {family.name for family in REGISTRY.collect()}

    queried: set[str] = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            queried |= set(re.findall(r"pulse_[a-z_]+", target.get("expr", "")))

    assert queried, "the dashboard queries nothing"
    missing = {m for m in queried if re.sub(r"_(bucket|sum|count|total)$", "", m) not in families}
    assert not missing, f"dashboard queries metrics that are never exported: {sorted(missing)}"


def test_prometheus_scrapes_exactly_the_services_that_serve_metrics() -> None:
    prom = yaml.safe_load((_ROOT / "infra/observability/prometheus.yml").read_text())
    scraped = {
        target.split(":")[0]
        for job in prom["scrape_configs"]
        for group in job["static_configs"]
        for target in group["targets"]
    }
    override = yaml.safe_load((_ROOT / "infra/docker/docker-compose.observability.yml").read_text())
    serving = {
        name
        for name, service in override["services"].items()
        if "METRICS_PORT" in (service.get("environment") or {})
    }
    assert (
        scraped
        == serving
        == {
            "api",
            "ingest-worker",
            "alert-worker",
            "billing-worker",
            "retention-worker",
        }
    )


# --- The release script --------------------------------------------------------


class _FakeRender:
    """Records the order things happen in. `outcomes` maps service id to the
    status sequence its deploy reports (the last value repeats)."""

    def __init__(
        self,
        outcomes: dict[str, list[str]],
        *,
        previous_live: dict[str, str | None] | None = None,
        rollback_outcomes: dict[str, list[str]] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.rollback_outcomes = rollback_outcomes or {}
        self._previous_live = previous_live or {}
        self.events: list[str] = []
        self._polls: dict[str, int] = {}

    def live_deploy(self, service_id: str) -> str | None:
        self.events.append(f"live_deploy:{service_id}")
        return self._previous_live.get(service_id, f"prev-{service_id}")

    def rollback(self, service_id: str, deploy_id: str) -> str:
        self.events.append(f"rollback:{service_id}:{deploy_id}")
        return f"rb-{service_id}"

    def trigger(self, service_id: str, commit: str) -> str:
        self.events.append(f"trigger:{service_id}:{commit}")
        return f"dep-{service_id}"

    def status(self, service_id: str, deploy_id: str) -> str:
        if deploy_id.startswith("rb-"):
            sequence = self.rollback_outcomes.get(service_id, ["live"])
            key = f"rb-{service_id}"
        else:
            sequence, key = self.outcomes[service_id], service_id
        index = self._polls.get(key, 0)
        self._polls[key] = index + 1
        status = sequence[min(index, len(sequence) - 1)]
        self.events.append(f"status:{service_id}:{status}")
        return status


@pytest.fixture(scope="module")
def release_module() -> Any:
    return _load(_ROOT / "infra" / "scripts" / "render_release.py", "render_release")


def test_the_api_is_live_before_any_worker_is_released(release_module: Any) -> None:
    fake = _FakeRender(
        {
            "api": ["build_in_progress", "pre_deploy_in_progress", "live"],
            "w1": ["live"],
            "w2": ["live"],
        }
    )
    release_module.release(fake, "abc123", ["api", "w1", "w2"], sleep=lambda _s: None)

    api_live = fake.events.index("status:api:live")
    assert fake.events.index("trigger:w1:abc123") > api_live
    assert fake.events.index("trigger:w2:abc123") > api_live
    triggers = [e for e in fake.events if e.startswith("trigger:")]
    assert triggers[0] == "trigger:api:abc123"  # the API is always released first


def test_a_failed_api_deploy_never_touches_the_workers(release_module: Any) -> None:
    """A migration failure surfaces as the API's pre-deploy failing -- workers
    must not then be rolled onto a schema that never got migrated."""
    fake = _FakeRender({"api": ["pre_deploy_in_progress", "pre_deploy_failed"], "w1": ["live"]})

    with pytest.raises(release_module.DeployFailed, match="pre_deploy_failed"):
        release_module.release(fake, "abc123", ["api", "w1"], sleep=lambda _s: None)

    assert not any(event.startswith("trigger:w1") for event in fake.events)


def test_a_failed_worker_deploy_fails_the_release(release_module: Any) -> None:
    fake = _FakeRender({"api": ["live"], "w1": ["update_failed"]})
    with pytest.raises(release_module.DeployFailed, match="update_failed"):
        release_module.release(fake, "abc123", ["api", "w1"], sleep=lambda _s: None)


def test_a_deploy_that_never_goes_live_times_out_instead_of_hanging(release_module: Any) -> None:
    fake = _FakeRender({"api": ["build_in_progress"]})
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(release_module.DeployFailed, match="timed out"):
        release_module.release(
            fake,
            "abc123",
            ["api"],
            timeout_s=300,
            poll_s=1,
            sleep=lambda _s: None,
            clock=lambda: float(next(ticks)),
        )


def test_a_release_with_no_services_is_rejected(release_module: Any) -> None:
    with pytest.raises(ValueError):
        release_module.release(_FakeRender({}), "abc123", [], sleep=lambda _s: None)


# --- Rollback (Phase 24) -------------------------------------------------------


def _release(module: Any, fake: Any, services: list[str], **kwargs: Any) -> None:
    module.release(fake, "abc123", services, sleep=lambda _s: None, **kwargs)


def test_the_live_deploys_are_recorded_before_anything_is_triggered(release_module: Any) -> None:
    """Once the new deploy is live, "the previous one" can no longer be told apart
    from the list, so the snapshot has to come first."""
    fake = _FakeRender({"api": ["live"], "w1": ["live"]})
    _release(release_module, fake, ["api", "w1"])

    first_trigger = next(i for i, e in enumerate(fake.events) if e.startswith("trigger:"))
    snapshots = [i for i, e in enumerate(fake.events) if e.startswith("live_deploy:")]
    assert len(snapshots) == 2 and max(snapshots) < first_trigger
    assert not any(e.startswith("rollback:") for e in fake.events)  # success: no rollback


def test_a_failed_worker_rolls_back_what_already_went_live_workers_first(
    release_module: Any,
) -> None:
    fake = _FakeRender({"api": ["live"], "w1": ["update_failed"], "w2": ["live"]})

    with pytest.raises(release_module.DeployFailed) as caught:
        _release(release_module, fake, ["api", "w1", "w2"])

    rollbacks = [e for e in fake.events if e.startswith("rollback:")]
    # w2 and the API went live, so they are rolled back -- workers before the API.
    # w1 never went live (Render keeps serving its old deploy), so it is not touched.
    assert rollbacks == ["rollback:w2:prev-w2", "rollback:api:prev-api"]
    message = str(caught.value)
    assert "update_failed" in message and "Rolled back w2, api" in message
    assert "forward-only" in message  # migrations are NOT undone, and it says so


def test_a_failed_api_deploy_has_nothing_to_roll_back(release_module: Any) -> None:
    fake = _FakeRender({"api": ["pre_deploy_failed"], "w1": ["live"]})

    with pytest.raises(release_module.DeployFailed, match="nothing to roll back"):
        _release(release_module, fake, ["api", "w1"])

    assert not any(e.startswith("rollback:") for e in fake.events)


def test_an_in_flight_deploy_is_allowed_to_settle_before_rollback(release_module: Any) -> None:
    """w1 fails while w2 is still building. Rolling w2 back mid-build would race
    its deploy, so the script waits for it to reach a terminal state first."""
    fake = _FakeRender(
        {"api": ["live"], "w1": ["update_failed"], "w2": ["build_in_progress", "live"]}
    )

    with pytest.raises(release_module.DeployFailed):
        _release(release_module, fake, ["api", "w1", "w2"])

    assert fake.events.index("status:w2:live") < fake.events.index("rollback:w2:prev-w2")


def test_a_failed_rollback_is_reported_loudly_not_swallowed(release_module: Any) -> None:
    fake = _FakeRender(
        {"api": ["live"], "w1": ["update_failed"]},
        rollback_outcomes={"api": ["update_failed"]},
    )

    with pytest.raises(release_module.DeployFailed, match="ROLLBACK FAILED"):
        _release(release_module, fake, ["api", "w1"])


def test_a_first_release_has_nothing_to_roll_back_to_and_says_so(release_module: Any) -> None:
    fake = _FakeRender(
        {"api": ["live"], "w1": ["update_failed"]}, previous_live={"api": None, "w1": None}
    )

    with pytest.raises(release_module.DeployFailed, match="first release"):
        _release(release_module, fake, ["api", "w1"])

    assert not any(e.startswith("rollback:") for e in fake.events)


def test_a_deploy_that_never_settles_does_not_hang_the_rollback(release_module: Any) -> None:
    fake = _FakeRender({"api": ["live"], "w1": ["update_failed"], "w2": ["build_in_progress"]})
    ticks = iter(range(0, 100_000, 100))

    with pytest.raises(release_module.DeployFailed):
        release_module.release(
            fake,
            "abc123",
            ["api", "w1", "w2"],
            timeout_s=300,
            poll_s=1,
            sleep=lambda _s: None,
            clock=lambda: float(next(ticks)),
        )

    # w2 never reached a terminal state, so it is not rolled back; the API is.
    assert [e for e in fake.events if e.startswith("rollback:")] == ["rollback:api:prev-api"]


def test_the_http_client_finds_the_live_deploy_and_posts_a_rollback(release_module: Any) -> None:
    calls: list[tuple[str, str, Any]] = []

    class Canned(release_module.HttpRenderClient):
        def _call(self, method: str, path: str, body: Any = None) -> Any:
            calls.append((method, path, body))
            if method == "GET":
                return [
                    {"deploy": {"id": "dep-new", "status": "build_in_progress"}, "cursor": "a"},
                    {"deploy": {"id": "dep-live", "status": "live"}, "cursor": "b"},
                    {"deploy": {"id": "dep-old", "status": "deactivated"}, "cursor": "c"},
                ]
            return {"id": "dep-rolled-back"}

    client = Canned("key")
    assert client.live_deploy("srv-1") == "dep-live"
    assert client.rollback("srv-1", "dep-live") == "dep-rolled-back"
    assert calls == [
        ("GET", "/services/srv-1/deploys?limit=20", None),
        ("POST", "/services/srv-1/rollback", {"deployId": "dep-live"}),
    ]


def test_the_http_client_reports_no_live_deploy_for_a_service_that_never_had_one(
    release_module: Any,
) -> None:
    class Canned(release_module.HttpRenderClient):
        def _call(self, method: str, path: str, body: Any = None) -> Any:
            return [{"deploy": {"id": "dep-1", "status": "build_failed"}, "cursor": "a"}]

    assert Canned("key").live_deploy("srv-1") is None


# --- CI gates and image contents (Phase 24) -------------------------------------


def _ci() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return data


def test_the_dependency_and_image_scans_are_blocking_gates() -> None:
    """They were advisory in Phase 22-23 only because of findings that couldn't be
    fixed then. Now they can, so neither may quietly go back to `|| true` or
    `continue-on-error` -- that would make a red scan invisible again."""
    jobs = _ci()["jobs"]
    for step in jobs["dependency-scan"]["steps"]:
        assert "|| true" not in step.get("run", ""), step.get("name")
    trivy = next(s for s in jobs["build"]["steps"] if "Trivy" in s.get("name", ""))
    assert not trivy.get("continue-on-error")
    assert "--exit-code 1" in trivy["run"] and "--ignore-unfixed" in trivy["run"]


def test_the_python_dependency_audits_install_the_project_first_in_their_own_venv() -> None:
    """Auditing a runner where nothing was installed audits nothing -- the flaw the
    advisory version of this job had. And one shared environment would let the
    backend's packages leak into the SDK's audit."""
    steps = {s["name"]: s["run"] for s in _ci()["jobs"]["dependency-scan"]["steps"] if "run" in s}
    backend, sdk = steps["pip-audit (backend)"], steps["pip-audit (sdk-python)"]
    assert 'pip install -e ".[dev]"' in backend and 'pip install -e ".[dev]"' in sdk
    assert "/tmp/backend-venv" in backend and "/tmp/sdk-venv" in sdk
    assert "/tmp/backend-venv" not in sdk


def test_the_backend_image_installs_the_debian_security_updates() -> None:
    dockerfile = (_ROOT / "backend" / "Dockerfile").read_text().replace("\r\n", "\n")
    assert "apt-get upgrade -y" in dockerfile
    assert dockerfile.index("apt-get upgrade") < dockerfile.index("pip install")


@pytest.mark.parametrize("name", ["prometheus.render.Dockerfile", "grafana.render.Dockerfile"])
def test_the_observability_dockerfiles_only_copy_files_that_exist(name: str) -> None:
    """A Dockerfile whose COPY source has been moved fails only at deploy time."""
    for line in (_OBS / name).read_text().splitlines():
        if line.startswith("COPY "):
            source = line.split()[1]
            assert (_OBS / source).exists(), f"{name}: COPY source {source} does not exist"


def test_the_terraform_remote_state_example_is_opt_in_and_ignored() -> None:
    example = (_TERRAFORM / "backend_override.tf.example").read_text()
    assert 'backend "s3"' in example and "encrypt = true" in example
    # No bucket, key or table is committed: they are passed at `terraform init`.
    assert not re.search(r"^\s*(bucket|key|dynamodb_table)\s*=", example, re.MULTILINE)
    assert "infra/terraform/backend_override.tf" in (_ROOT / ".gitignore").read_text()
    # It is only an example: without the override, Terraform still uses local state.
    assert "backend " not in (_TERRAFORM / "versions.tf").read_text()

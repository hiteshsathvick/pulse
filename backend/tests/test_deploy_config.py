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
def test_backend_services_pull_the_managed_env_group_and_the_frontend_does_not(env: str) -> None:
    """The env group holds database, ClickHouse and signing secrets. The API
    and workers need them; the frontend is a browser-facing renderer and must
    not be handed credentials it has no use for."""
    for name, service in _services(env).items():
        groups = [e["fromGroup"] for e in service["envVars"] if "fromGroup" in e]
        expected = [] if name == "frontend" else [f"pulse-{env}-managed"]
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

    def __init__(self, outcomes: dict[str, list[str]]) -> None:
        self.outcomes = outcomes
        self.events: list[str] = []
        self._polls: dict[str, int] = {}

    def trigger(self, service_id: str, commit: str) -> str:
        self.events.append(f"trigger:{service_id}:{commit}")
        return f"dep-{service_id}"

    def status(self, service_id: str, deploy_id: str) -> str:
        sequence = self.outcomes[service_id]
        index = self._polls.get(service_id, 0)
        self._polls[service_id] = index + 1
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
    assert fake.events[0] == "trigger:api:abc123"


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

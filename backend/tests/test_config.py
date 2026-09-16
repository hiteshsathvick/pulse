from pulse.core.config import Settings, get_settings

_ENV_VARS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "DATABASE_URL",
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_SECURE",
    "REDIS_URL",
)


def test_settings_defaults(monkeypatch) -> None:
    # Defaults must hold regardless of the ambient environment -- this suite
    # also runs inside the docker-compose api container, where these are all
    # set for real, so assert against a deliberately cleared environment.
    for key in _ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.redis_url == "redis://localhost:6379/0"


def test_settings_reads_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example.internal")

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "WARNING"
    assert settings.clickhouse_host == "ch.example.internal"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()

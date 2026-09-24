"""One call per process entrypoint (the API module, each worker's main()) to
bring up Phase 23's three pieces together. Everything it starts is opt-in via
Settings and a no-op when unconfigured, so calling it is always safe."""

from pulse.core.config import get_settings
from pulse.observability.metrics import start_metrics_server
from pulse.observability.sentry import init_sentry
from pulse.observability.tracing import setup_tracing


def setup_observability(service_name: str) -> None:
    settings = get_settings()
    # Sentry first: if tracing or metrics setup itself throws, that error is
    # already reportable.
    init_sentry(service_name, settings)
    setup_tracing(service_name, settings.otel_exporter_otlp_endpoint)
    start_metrics_server(settings.metrics_port)

#!/bin/sh
# Renders prometheus.render.yml from environment variables, then runs Prometheus.
# Fails at start (visible in the deploy) if any target is missing, rather than
# running a Prometheus that silently scrapes nothing.
set -eu

for var in METRICS_TOKEN HOST_API HOST_INGEST_WORKER HOST_ALERT_WORKER \
           HOST_BILLING_WORKER HOST_RETENTION_WORKER; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "prometheus-render-entrypoint: $var is not set" >&2
    exit 1
  fi
done

# The values are substituted with sed, so refuse anything that could alter the
# expression or break out of the YAML string. Terraform generates the token from
# letters and digits only; hostnames are letters, digits and hyphens.
case "$METRICS_TOKEN" in
  *[!A-Za-z0-9]*) echo "prometheus-render-entrypoint: METRICS_TOKEN must be alphanumeric" >&2; exit 1 ;;
esac
for var in HOST_API HOST_INGEST_WORKER HOST_ALERT_WORKER \
           HOST_BILLING_WORKER HOST_RETENTION_WORKER; do
  eval "value=\${$var}"
  case "$value" in
    *[!A-Za-z0-9-]*) echo "prometheus-render-entrypoint: $var must be a bare hostname" >&2; exit 1 ;;
  esac
done

sed \
  -e "s|@@METRICS_TOKEN@@|$METRICS_TOKEN|g" \
  -e "s|@@HOST_API@@|$HOST_API|g" \
  -e "s|@@HOST_INGEST_WORKER@@|$HOST_INGEST_WORKER|g" \
  -e "s|@@HOST_ALERT_WORKER@@|$HOST_ALERT_WORKER|g" \
  -e "s|@@HOST_BILLING_WORKER@@|$HOST_BILLING_WORKER|g" \
  -e "s|@@HOST_RETENTION_WORKER@@|$HOST_RETENTION_WORKER|g" \
  /etc/prometheus/prometheus.tmpl.yml > /tmp/prometheus.yml

exec /bin/prometheus \
  --config.file=/tmp/prometheus.yml \
  --storage.tsdb.path=/var/data/prometheus \
  --storage.tsdb.retention.time=15d \
  --web.listen-address=0.0.0.0:9090

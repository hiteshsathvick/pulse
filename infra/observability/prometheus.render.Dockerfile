FROM prom/prometheus:v2.55.0
# Root, because a Render persistent disk (mounted at /var/data, per Render's
# Prometheus guide) is owned by root and the image's default `nobody` user
# could not write the time-series database to it.
USER root
COPY prometheus.render.yml /etc/prometheus/prometheus.tmpl.yml
COPY prometheus-render-entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]

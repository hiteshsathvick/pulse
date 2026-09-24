FROM grafana/grafana:11.3.0
COPY grafana/provisioning/dashboards /etc/grafana/provisioning/dashboards
COPY grafana/render/datasources.yaml /etc/grafana/provisioning/datasources/datasources.yaml
COPY grafana/dashboards /var/lib/grafana/dashboards

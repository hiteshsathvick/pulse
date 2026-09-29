environment = "staging"

# Render's flexible Postgres plan IDs use an underscore (basic_256mb), not a
# hyphen -- confirmed against the render_postgres provider resource and
# Render's own docs (the old hyphenated "Basic-256mb" style is a legacy
# instance type, not available for a newly created database). Found before
# ever running a real apply, exactly the kind of first-deploy surprise
# docs/DEPLOYMENT.md's "Not yet proven" section warned about.
postgres_plan = "basic_256mb"
keyvalue_plan = "starter"

clickhouse_min_replica_memory_gb = 8
clickhouse_max_replica_memory_gb = 8

# Staging is disposable and holds no customer data; still replace this with
# Render's outbound IP ranges for your region before pointing anything real at it.
clickhouse_ip_allow_list = ["0.0.0.0/0"]

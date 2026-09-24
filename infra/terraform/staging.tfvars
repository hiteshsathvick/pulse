environment = "staging"

postgres_plan = "basic-256mb"
keyvalue_plan = "starter"

clickhouse_min_replica_memory_gb = 8
clickhouse_max_replica_memory_gb = 8

# Staging is disposable and holds no customer data; still replace this with
# Render's outbound IP ranges for your region before pointing anything real at it.
clickhouse_ip_allow_list = ["0.0.0.0/0"]

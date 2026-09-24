environment = "prod"

postgres_plan = "basic-1gb"
keyvalue_plan = "standard"

clickhouse_min_replica_memory_gb = 16
clickhouse_max_replica_memory_gb = 32

# REQUIRED before applying: Render's outbound IP ranges for your region.
# Deliberately empty -- an empty list fails `plan` (see the precondition in
# main.tf) rather than silently opening production ClickHouse to the world.
clickhouse_ip_allow_list = []

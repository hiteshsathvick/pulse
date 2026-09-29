environment = "prod"

# See staging.tfvars: underscore, not hyphen -- the old hyphenated style is a
# legacy instance type Render no longer accepts for a new database. The exact
# size suffix here (1gb) is unconfirmed against Render's real API (only
# basic_256mb was confirmed directly); verify with `terraform plan` before
# ever applying this file for real.
postgres_plan = "basic_1gb"
keyvalue_plan = "standard"

clickhouse_min_replica_memory_gb = 16
clickhouse_max_replica_memory_gb = 32

# REQUIRED before applying: Render's outbound IP ranges for your region.
# Deliberately empty -- an empty list fails `plan` (see the precondition in
# main.tf) rather than silently opening production ClickHouse to the world.
clickhouse_ip_allow_list = []

CREATE TABLE events
(
    org_id        UUID,
    project_id    UUID,
    event_id      UUID,
    event_name    LowCardinality(String),
    user_id       String,
    anonymous_id  String,
    timestamp     DateTime64(3),
    received_at   DateTime64(3),
    properties    Map(String, String),
    _ingest_batch UUID
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, project_id, event_name, timestamp, event_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY
SETTINGS index_granularity = 8192

-- Phase 17: an hourly rollup of `events`, kept current by a materialized view.
-- See SPEC.md 5.2 and 6.14. Differences from the original 5.2 sketch:
--   * HOURLY, not daily: trends bucket by the project's timezone, and a UTC-day
--     rollup can only answer for UTC projects. Hour buckets can be re-bucketed
--     into local days for any timezone whose offset is a whole number of hours.
--   * `events` is exact, so counts from the rollup equal counts from raw.
--   * Unique users are an APPROXIMATE sketch (uniqCombined64 with precision 15,
--     roughly 0.4 percent mean error, with a narrow band of up to about 3.5
--     percent near 80 thousand distinct users where the sketch changes mode) over the
--     same identity the engine's unique_users measure uses (user_id once
--     identified, otherwise anonymous_id). An exact state was tried and was
--     3 to 20 times slower than scanning raw events, because merging exact user
--     sets costs more than the scan. The engine therefore uses this sketch only
--     when the window is large enough that exact raw would be slow or refused.
--   * An explicit target table (TO event_hourly), so it can be backfilled,
--     verified and rebuilt directly.
-- `events` must be a SimpleAggregateFunction: in an AggregatingMergeTree a plain
-- column keeps an arbitrary row's value when parts merge instead of summing.
-- The migration runner splits this file on semicolons, so none may appear in a
-- comment or string.

CREATE TABLE event_hourly
(
    org_id      UUID,
    project_id  UUID,
    event_name  LowCardinality(String),
    hour        DateTime('UTC'),
    events      SimpleAggregateFunction(sum, UInt64),
    users_state AggregateFunction(uniqCombined64(15), String)
)
ENGINE = AggregatingMergeTree
PARTITION BY (org_id, toYYYYMM(hour))
ORDER BY (org_id, project_id, event_name, hour)
TTL hour + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW mv_event_hourly TO event_hourly AS
SELECT
    org_id,
    project_id,
    event_name,
    toStartOfHour(toDateTime(timestamp, 'UTC'), 'UTC') AS hour,
    count() AS events,
    uniqCombined64State(15)(if(user_id != '', user_id, anonymous_id)) AS users_state
FROM events
GROUP BY org_id, project_id, event_name, hour;

INSERT INTO event_hourly
SELECT
    org_id,
    project_id,
    event_name,
    toStartOfHour(toDateTime(timestamp, 'UTC'), 'UTC') AS hour,
    count() AS events,
    uniqCombined64State(15)(if(user_id != '', user_id, anonymous_id)) AS users_state
FROM events
GROUP BY org_id, project_id, event_name, hour

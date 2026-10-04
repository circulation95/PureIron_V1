-- pureiron:no-transaction
-- baseline epoch 1: 1분 continuous aggregate. WITH NO DATA로 만들고 채우기는 refresh policy에 맡긴다.
-- 트랜잭션 밖에서 구문별로 실행되므로 각 구문은 재실행에 안전해야 한다.
CREATE MATERIALIZED VIEW IF NOT EXISTS tag_history_1m
WITH (timescaledb.continuous) AS
SELECT
    tag_id,
    time_bucket(INTERVAL '1 minute', time) AS bucket,
    avg(value_num)        AS avg,
    min(value_num)        AS min,
    max(value_num)        AS max,
    last(value_num, time) AS last,
    count(*)              AS samples
FROM tag_history
GROUP BY tag_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'tag_history_1m',
    start_offset => INTERVAL '1 day',
    end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

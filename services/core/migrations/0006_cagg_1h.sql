-- pureiron:no-transaction
-- epoch 2 (r1): 1시간 continuous aggregate(ADR-021). 1분 CAGG와 같은 열을 원본 tag_history에서
-- 직접 집계한다(1분 평균을 다시 평균하면 표본 수 가중이 틀어진다). 압축된 chunk도 그대로 읽는다.
-- WITH NO DATA로 만들고 채우기는 0007의 refresh policy에 맡긴다.
CREATE MATERIALIZED VIEW IF NOT EXISTS tag_history_1h
WITH (timescaledb.continuous) AS
SELECT
    tag_id,
    time_bucket(INTERVAL '1 hour', time) AS bucket,
    avg(value_num)        AS avg,
    min(value_num)        AS min,
    max(value_num)        AS max,
    last(value_num, time) AS last,
    count(*)              AS samples
FROM tag_history
GROUP BY tag_id, bucket
WITH NO DATA;

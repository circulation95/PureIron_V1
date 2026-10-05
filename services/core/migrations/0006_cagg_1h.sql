-- pureiron:no-transaction
-- epoch 2 (r1): 1시간 continuous aggregate(ADR-021). 1분 CAGG와 같은 열을 원본 tag_history에서
-- 직접 집계한다 — 1분 CAGG 위에 계층으로 쌓으면 1시간 refresh가 1분 policy 범위(1일)에 묶인다.
-- 압축된 chunk도 그대로 읽는다. samples = 행 수(count(*), value_num이 NULL인 bool·BAD 행 포함)로
-- avg(value_num)의 분모와 다르다 — 가중 롤업에 쓰지 않는다.
-- WITH NO DATA로 만들고 채우기는 0007의 refresh policy에 맡긴다(업그레이드 이전 이력 backfill은 M3a).
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

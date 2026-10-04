-- baseline epoch 1: 이력 hypertable(1일 chunk), 2일 지난 chunk 압축(ADR-021).
-- 보존 기간은 프로젝트 설정(historian.retention_days)이 정하므로 여기서 retention 정책을 두지 않는다
-- (legacy 027_add_tag_history_retention.sql의 하드코딩 대체).
-- quality: collector.proto Quality 값(1 GOOD, 2 BAD, 3 UNCERTAIN).
CREATE TABLE tag_history (
    time       timestamptz      NOT NULL,
    tag_id     integer          NOT NULL,
    value_num  double precision,
    value_bool boolean,
    quality    smallint         NOT NULL,
    UNIQUE (tag_id, time)
);

SELECT create_hypertable('tag_history', by_range('time', INTERVAL '1 day'));

ALTER TABLE tag_history SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tag_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('tag_history', INTERVAL '2 days');

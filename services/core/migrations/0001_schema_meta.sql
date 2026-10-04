-- baseline epoch 1: TimescaleDB extension + 스키마 메타(현재 epoch 등).
-- schema_migrations(이력)는 migrate runner가 직접 만든다(이 파일을 기록해야 하므로).
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS schema_meta (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- epoch 3(r2): historian 설정 저장과 CAGG 실시간 집계.
-- project_state.historian: project.yaml `historian`(정규형, 스키마 기본값 생략). NULL = 전부 기본값.
-- apply-definitions가 장비·태그 정의와 같은 트랜잭션에서 쓰고, 바뀌면 definitions_rev를 올린다.
-- retention_days 적용(retention 정책)은 Updater `succeeded` 부수 효과(M5) — 여기서는 값만 둔다.
ALTER TABLE project_state ADD COLUMN historian jsonb;

-- 실시간 집계: 아직 materialize되지 않은 최근 구간은 원본 tag_history에서 바로 집계해 합친다
-- (refresh policy 주기만큼 최근 데이터가 1m/1h 조회에서 빠지지 않게). 뷰 정의·데이터는 그대로다.
-- 0006/0007의 "업그레이드 이전 이력 1h backfill"은 필요 없다: r0·r1에는 historian이 없어
-- 어떤 현장에도 tag_history 행이 없다(첫 기록은 이 epoch의 core부터).
ALTER MATERIALIZED VIEW tag_history_1m SET (timescaledb.materialized_only = false);

ALTER MATERIALIZED VIEW tag_history_1h SET (timescaledb.materialized_only = false);

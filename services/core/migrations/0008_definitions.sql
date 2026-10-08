-- epoch 3(r2): 장비·태그 정의 원문(project-tools 정규형, 스키마 기본값 생략)을 jsonb로 둔다.
-- `pureiron-core apply-definitions`가 path 기준 upsert·retire로 채우고 core는 이 값으로 AcquisitionPlan을
-- 만든다. 정의가 없는(NULL) 행은 plan에서 빠진다. 정식 project apply(M5)가 이 경로를 감싼다.
ALTER TABLE devices ADD COLUMN definition jsonb;

ALTER TABLE tags ADD COLUMN definition jsonb;

-- AcquisitionPlan.plan_rev. apply-definitions가 실제로 바뀐 것이 있을 때만 +1(리셋 없음).
ALTER TABLE project_state ADD COLUMN definitions_rev bigint NOT NULL DEFAULT 0;

-- epoch 4(r3): 알람 정의·저널(M3b-2, plan D6·D7). 정의는 고객 project `alarms/*.yaml`을 apply-definitions가
-- alarm_id 기준으로 upsert 하고, 빠진 정의는 retired_at만 표시한다(태그·장비와 같은 규칙).
-- definition: 정규형 JSON(스키마 기본값 생략 — 소비자가 deadband 0·on_delay_s 0·priority medium을 채운다).
CREATE TABLE alarms (
    alarm_id   text        PRIMARY KEY,
    tag_path   text        NOT NULL,
    definition jsonb       NOT NULL,
    retired_at timestamptz
);

-- 알람 상태 전이 기록(현재 상태가 아니라 이력·감사 — 현재 상태는 core 알람 엔진). 추가만 한다.
-- from_state·to_state: NORMAL · ACTIVE_UNACKED · ACTIVE_ACKED · CLEARED_UNACKED · RETIRED(정의에서 빠짐).
-- username: ACK한 사용자(조건 변화로 생긴 전이는 NULL). core는 기동 시 알람별 마지막 to_state로 복원한다.
CREATE TABLE alarm_journal (
    id         bigserial   PRIMARY KEY,
    time       timestamptz NOT NULL DEFAULT now(),
    alarm_id   text        NOT NULL,
    tag_path   text        NOT NULL,
    from_state text        NOT NULL,
    to_state   text        NOT NULL,
    value      float8,
    username   text,
    note       text
);

CREATE INDEX alarm_journal_alarm_id_time_idx ON alarm_journal (alarm_id, time DESC);

CREATE INDEX alarm_journal_time_idx ON alarm_journal (time DESC);

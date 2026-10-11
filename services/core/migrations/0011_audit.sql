-- epoch 4(r3): 감사 기록(M3b-1, plan D12). 추가만 하고 고치거나 지우지 않는다(보존 정책은 범위 밖).
-- username: 행위자(Updater·CLI처럼 로그인 사용자가 없으면 NULL, 실패한 로그인은 입력한 이름).
-- source: api · cli · updater. detail에는 비밀번호·토큰을 절대 넣지 않는다.
CREATE TABLE audit_log (
    id       bigserial   PRIMARY KEY,
    time     timestamptz NOT NULL DEFAULT now(),
    username text,
    action   text        NOT NULL,
    target   text,
    detail   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    source   text        NOT NULL
);

CREATE INDEX audit_log_time_idx ON audit_log (time DESC);

CREATE INDEX audit_log_username_time_idx ON audit_log (username, time DESC);

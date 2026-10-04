-- baseline epoch 1: 현장 프로젝트 적용본 상태(단일 행, ADR-025).
-- site_rev: 현장 PATCH마다 단조 증가(리셋 없음). base_site_rev: 마지막 적용 시점의 site_rev.
-- edit_lock_*: Updater 편집 잠금(잡혀 있으면 PATCH 423).
CREATE TABLE project_state (
    id              boolean     PRIMARY KEY DEFAULT TRUE CHECK (id),
    project_id      text,
    version         text,
    content_hash    text,
    base_site_rev   bigint      NOT NULL DEFAULT 0,
    site_rev        bigint      NOT NULL DEFAULT 0,
    edit_lock_owner text,
    edit_lock_gen   bigint,
    applied_at      timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

INSERT INTO project_state (id) VALUES (TRUE) ON CONFLICT DO NOTHING;

-- baseline epoch 1: control-db(PostgreSQL 16) 스키마 메타 + 중앙 표(plan M4 C6, foundation §6.4).
-- schema_migrations(이력)는 migrate runner가 직접 만든다(이 파일을 기록해야 하므로).
CREATE TABLE IF NOT EXISTS schema_meta (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 사람·CI 토큰(C5). CLI `pureiron-control token create --role`로만 발급, 원문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text        NOT NULL,
    role         text        NOT NULL CHECK (role IN ('admin', 'ci', 'viewer')),
    token_sha256 text        NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS api_tokens_active_name ON api_tokens (name)
    WHERE revoked_at IS NULL;

-- 등록된 현장과 그 현재 상태. last_seq = 마지막으로 반영한 report_seq(heartbeat·보고 공용 카운터).
-- current·last_attempt는 report_seq가 last_seq보다 큰 메시지만 바꾼다(C7). last_seen은 온라인
-- 메시지(token·서명 확인)를 받은 중앙 시각 — 폐쇄망 반입은 바꾸지 않는다(그 현장을 "본" 것이 아니다).
CREATE TABLE IF NOT EXISTS sites (
    site_id             text        PRIMARY KEY,
    registered_at       timestamptz NOT NULL,
    last_seq            bigint      NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    last_seen           timestamptz,
    current             jsonb,
    current_reported_at timestamptz,
    current_source      text        CHECK (current_source IN ('online', 'offline')),
    updater_phase       text,
    disk_state          text,
    last_attempt_id     bigint
);

-- site 공개키(PD-3). 현장당 유효 키는 하나, rekey는 구 키에 revoked_at을 찍는다.
CREATE TABLE IF NOT EXISTS site_keys (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id     text        NOT NULL REFERENCES sites (site_id),
    public_key  text        NOT NULL,
    fingerprint text        NOT NULL,
    created_at  timestamptz NOT NULL,
    revoked_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS site_keys_active ON site_keys (site_id)
    WHERE revoked_at IS NULL;

-- 현장 온라인 bearer token(SHA-256만 저장). 재발급은 구 토큰에 revoked_at을 찍는다.
CREATE TABLE IF NOT EXISTS site_tokens (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id      text        NOT NULL REFERENCES sites (site_id),
    token_sha256 text        NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS site_tokens_active ON site_tokens (site_id)
    WHERE revoked_at IS NULL;

-- 게시된 번들(PD-15: gen은 현장별 단조 증가, gen ≤ 최신 게시는 409). 파일은 디스크
-- `<data>/bundles/<site>/g<gen>.pib`, 여기에는 sha256·크기와 manifest의 표시용 값만 둔다.
CREATE TABLE IF NOT EXISTS bundles (
    site_id         text        NOT NULL REFERENCES sites (site_id),
    generation      integer     NOT NULL CHECK (generation >= 1),
    sha256          text        NOT NULL,
    size_bytes      bigint      NOT NULL CHECK (size_bytes > 0),
    product_version text        NOT NULL,
    project_id      text        NOT NULL,
    project_version text        NOT NULL,
    fleet_commit    text        NOT NULL,
    published_at    timestamptz NOT NULL,
    published_by    bigint      REFERENCES api_tokens (id),
    PRIMARY KEY (site_id, generation)
);

-- 폐쇄망 반입 1회(`site-reports` 봉투 한 파일).
CREATE TABLE IF NOT EXISTS offline_imports (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id       text        NOT NULL REFERENCES sites (site_id),
    first_seq     bigint      NOT NULL,
    last_seq      bigint      NOT NULL,
    exported_at   text        NOT NULL,
    file_sha256   text        NOT NULL,
    applied_total integer     NOT NULL DEFAULT 0,
    history_total integer     NOT NULL DEFAULT 0,
    imported_at   timestamptz NOT NULL,
    imported_by   bigint      REFERENCES api_tokens (id)
);

-- ApplyAttempt 보고 이력. 받은 보고는 모두 남기고 outcome으로 반영 여부를 적는다.
CREATE TABLE IF NOT EXISTS apply_attempts (
    id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id           text        NOT NULL REFERENCES sites (site_id),
    report_seq        bigint      NOT NULL,
    source            text        NOT NULL CHECK (source IN ('online', 'offline')),
    outcome           text        NOT NULL CHECK (outcome IN ('applied', 'history')),
    generation        integer,
    result            text        NOT NULL,
    payload           jsonb       NOT NULL,
    received_at       timestamptz NOT NULL,
    offline_import_id bigint      REFERENCES offline_imports (id)
);
CREATE INDEX IF NOT EXISTS apply_attempts_site_seq ON apply_attempts (site_id, report_seq);
ALTER TABLE sites
    ADD CONSTRAINT sites_last_attempt_fk FOREIGN KEY (last_attempt_id) REFERENCES apply_attempts (id);

-- heartbeat 이력(온라인 전용이지만 같은 seq 규칙 함수를 지나므로 source·outcome을 같이 둔다).
CREATE TABLE IF NOT EXISTS heartbeats (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id     text        NOT NULL REFERENCES sites (site_id),
    report_seq  bigint      NOT NULL,
    source      text        NOT NULL CHECK (source IN ('online', 'offline')),
    outcome     text        NOT NULL CHECK (outcome IN ('applied', 'history')),
    payload     jsonb       NOT NULL,
    received_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS heartbeats_site_received ON heartbeats (site_id, received_at);

-- 현장 드래프트(M5-2 `draft.tar`, site-draft 봉투로 검증). 파일은 `<data>/drafts/<site>/<sha256>.tar`.
CREATE TABLE IF NOT EXISTS drafts (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id       text        NOT NULL REFERENCES sites (site_id),
    site_rev      bigint      NOT NULL,
    base_site_rev bigint      NOT NULL,
    content_hash  text        NOT NULL,
    exported_at   text        NOT NULL,
    sha256        text        NOT NULL,
    size_bytes    bigint      NOT NULL,
    received_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_site_received ON drafts (site_id, id);

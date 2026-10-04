-- baseline epoch 1: 장비·태그 정의(적용본). tag id는 path 기준으로 안정적이고 재사용하지 않는다(ADR-021).
-- 삭제는 retired_at 표시만 한다.
CREATE TABLE devices (
    id         text        PRIMARY KEY,
    protocol   text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz
);

CREATE TABLE tags (
    id         serial      PRIMARY KEY,
    path       text        NOT NULL UNIQUE,
    device_id  text        REFERENCES devices (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz
);

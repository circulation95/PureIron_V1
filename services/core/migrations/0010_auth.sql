-- epoch 4(r3): 사용자·세션(M3b-1, plan D2·D3·D4·D5). 기본 계정은 만들지 않는다 — `pureiron-core users add`로만.
-- role: 0 viewer · 1 operator · 2 engineer · 3 admin(수준 비교). password_hash: `scrypt$n$r$p$salt_b64$hash_b64`.
-- 삭제 대신 enabled = false(감사·세션 기록의 username이 계속 가리킬 수 있게).
CREATE TABLE users (
    user_id       bigserial   PRIMARY KEY,
    username      text        NOT NULL UNIQUE,
    display_name  text,
    role          smallint    NOT NULL CHECK (role BETWEEN 0 AND 3),
    password_hash text        NOT NULL,
    enabled       boolean     NOT NULL DEFAULT TRUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 불투명 세션 토큰은 sha256만 둔다(DB가 새어도 토큰을 되살릴 수 없다). 유휴 만료 = last_seen_at 기준,
-- 절대 만료 = expires_at. 로그아웃·비활성화는 revoked_at으로 즉시 무효.
CREATE TABLE sessions (
    token_sha256 bytea       PRIMARY KEY,
    user_id      bigint      NOT NULL REFERENCES users (user_id),
    created_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);

CREATE INDEX sessions_user_id_idx ON sessions (user_id);

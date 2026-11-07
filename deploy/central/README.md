# deploy/central — 중앙 compose(M4-3)

control-db(PostgreSQL 16) + control-api + control-web(Caddy: HTTPS·basic auth·프록시). PoC 위치는 개발 PC WSL(M4 C3), 클라우드 VM·공인 인증서는 M6/첫 계약(PD-1).

| 경로 | basic auth | 넘기는 곳 | 비고 |
|---|---|---|---|
| `/api/v1/*` | 없음 | control-api 그대로 | API 자체 인증이 관문(C5): 현장 = site token + 서명, 사람·CI = admin·ci·viewer 토큰. 현장 Updater는 여기로 HTTPS polling(G3) |
| `GET /ui/fleet` | 필요 | control-api `GET /api/v1/fleet` | **viewer 토큰을 붙이는 유일한 경로**(★G1). 브라우저는 토큰을 갖지 않는다 |
| 그 밖의 `/ui/*` | 필요 | 없음(404) | 토큰 없음, 프록시 안 함 |
| `/`, 정적 파일 | 필요 | control-web 정적(SPA) | 읽기 전용 표 1장 |

모든 응답에 보안 헤더 4개(site web nginx와 같은 값): `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`.

## 비밀 파일

`PUREIRON_CENTRAL_SECRETS_DIR`(저장소 밖, 디렉터리 0700) 아래 파일 3개. 컨테이너 안에서는 `/run/secrets/<이름>`.

| 파일 | 내용 | 만드는 법 |
|---|---|---|
| `control_db_password` | control-db 비밀번호 | `openssl rand -base64 24 > control_db_password` |
| `ui_basic_auth` | 한 줄 `<사용자> <bcrypt 해시>` — Caddyfile이 `import`한다 | `docker run --rm -it --entrypoint caddy pureiron/control-web:<release> hash-password` → `echo 'ops <해시>' > ui_basic_auth`(해시의 `$`가 셸에서 풀리지 않게 작은따옴표) |
| `viewer_token` | control-api viewer 토큰 원문 | 아래 첫 기동 3단계(CLI로만 발급, C5) |

컨테이너 사용자가 읽을 수 있어야 한다(control-api는 UID 10001): 파일은 0644, 보호는 디렉터리 권한으로. viewer 토큰을 바꾸면(`token revoke` → `create`) 파일만 고친다 — Caddy가 요청마다 파일을 읽는다(재시작 불필요).

## 첫 기동

```bash
cd deploy/central && cp .env.example .env    # 값 수정(.env는 커밋되지 않는다)
C="docker compose -p pureiron-central -f compose.yml"
S=/home/<user>/pureiron-central/secrets      # .env의 PUREIRON_CENTRAL_SECRETS_DIR과 같은 값
PUREIRON_RELEASE=<release> bash ../build/build_images.sh control-api control-web   # 저장소 루트 기준 스크립트
$C run --rm control-api migrate up                          # 1. 스키마(기동 때 자동 실행하지 않는다)
$C run --rm control-api token create --role admin --name ops       # 2. 운영자 토큰(원문은 이때 한 번만)
$C run --rm -T control-api token create --role viewer --name control-web \
  > "$S/viewer_token"                                       # 3. 화면용 viewer 토큰 → 비밀 파일
$C up -d --wait                                             # 4. 기동
```

현장 등록은 CLI 또는 `POST /api/v1/sites`(admin): `$C run --rm -T control-api site register /dev/stdin < enrollment.json` → 현장 token 원문(1회).

업그레이드: 새 release 이미지 빌드 → `.env`의 `PUREIRON_CENTRAL_RELEASE` 변경 → `migrate up` → `up -d --wait`.

## 이미지 신원

`pureiron/control-api`·`control-web`은 로컬 이미지만 쓴다(`pull_policy: never`, 현장 compose와 같은 규칙). 신원 정본은 release 기록 `deploy/releases/central/<release>.json`의 config digest다(`python deploy/build/release_record.py <release> --central`, 이미지 tar는 image store에 보관). control-db는 업스트림 `postgres:16`을 digest로 고정한다(`deploy/compose/control.dev.yml`과 같은 값). 중앙 이미지는 현장 번들·manifest에 들어가지 않는다.

## TLS와 루트 CA 내보내기

PoC는 Caddy `tls internal`(Caddy 로컬 CA)이다. 인증서 이름은 `PUREIRON_CENTRAL_HOST`(여러 개면 쉼표). 루트 CA는 `caddy-data` 볼륨에 있고, 볼륨을 지우면 바뀐다.

```bash
$C cp control-web:/data/caddy/pki/authorities/local/root.crt ./pureiron-central-root.crt
openssl x509 -in pureiron-central-root.crt -noout -subject -fingerprint -sha256   # 지문을 따로 전달해 대조
curl --cacert pureiron-central-root.crt https://localhost:28443/api/v1/healthz
```

브라우저는 이 CA를 신뢰 저장소에 넣거나 경고를 감수한다. 현장 Updater(시스템 CA, urllib)가 이 CA를 신뢰하게 하는 설정은 M4 마감 VM 항목에서 정한다(M4-3 범위 밖).

## 첫 계약 전 강화 항목

- **admin·ci 경로를 바깥에서 막기**(G3·R2): 지금은 `/api/v1/*` 전체가 HTTPS 포트로 열려 있고 토큰 인증만 관문이다. 첫 계약 전에 `POST /sites`·rekey·token·`PUT /bundles`·offline-imports를 로컬(또는 publish runner) 출처로만 받게 Caddy matcher(`remote_ip`)나 별도 listener로 제한한다.
- 공인 인증서(ACME)·클라우드 VM(M6, PD-1). `tls internal` 루트 CA 교체 절차.
- 사람 관문을 basic auth → SSO 또는 control-api 사람 세션으로(범위 밖으로 둔 결정).
- Caddy 컨테이너는 업스트림 기본(root)으로 돈다 — 비root 실행·읽기 전용 루트 파일시스템.
- heartbeats 보존 정책(M4-0 백로그), 중앙 백업(control-db·`control-data` 볼륨).

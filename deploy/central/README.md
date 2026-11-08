# deploy/central — 중앙 compose(M4-3)

control-db(PostgreSQL 16) + control-api + control-web(Caddy: HTTPS·basic auth·프록시). PoC 위치는 개발 PC WSL(M4 C3), 클라우드 VM·공인 인증서는 M6/첫 계약(PD-1).

| 경로 | basic auth | 넘기는 곳 | 비고 |
|---|---|---|---|
| `/api/v1/*` | 없음 | control-api 그대로 | API 자체 인증이 관문(C5): 현장 = site token + 서명, 사람·CI = admin·ci·viewer 토큰. 현장 Updater는 여기로 HTTPS polling(G3) |
| `GET /ui/fleet` | 필요 | control-api `GET /api/v1/fleet` | **viewer 토큰을 붙이는 유일한 경로**(★G1). 브라우저는 토큰을 갖지 않는다 |
| 그 밖의 `/ui/*` | 필요 | 없음(404) | 토큰 없음, 프록시 안 함 |
| `/`, 정적 파일 | 필요 | control-web 정적(SPA) | 읽기 전용 표 1장 |
| 설정에 없는 `Host` | - | 없음(421) | `PUREIRON_CENTRAL_HOST`에 없는 이름으로 온 요청 |

Caddy 핸들러를 지나는 모든 응답에 보안 헤더 4개(site web nginx와 같은 값): `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`. 예외는 Go HTTP 서버가 핸들러 전에 400으로 답하는 요청이다(빈 `Host:`, HTTP/1.1 Host 없음, 쉼표나 `@`가 든 Host). 브라우저가 만들 요청이 아니다.

## 비밀 파일

`PUREIRON_CENTRAL_SECRETS_DIR`(저장소 밖, 디렉터리 0700) 아래 파일 3개. 컨테이너 안에서는 `/run/secrets/<이름>`.

| 파일 | 내용 | 만드는 법 |
|---|---|---|
| `control_db_password` | control-db 비밀번호 | `openssl rand -base64 24 > control_db_password` |
| `ui_basic_auth` | 한 줄 `<사용자> <bcrypt 해시>` — Caddyfile이 기동 때 `import`한다. 파일이 없으면 Caddy가 뜨지 않지만, **빈 파일**(빈 줄·주석만 있는 파일 포함)이면 경고("Import file is empty")만 남기고 떠서 화면 요청이 모두 401이다 — 화면이 401뿐이면 이 파일부터 본다 | `docker run --rm -it --entrypoint caddy pureiron/control-web:<release> hash-password` → `echo 'ops <해시>' > ui_basic_auth`(해시의 `$`가 셸에서 풀리지 않게 작은따옴표) |
| `viewer_token` | control-api viewer 토큰 원문 | 아래 첫 기동 3단계(CLI로만 발급, C5) |

컨테이너 사용자가 읽을 수 있어야 한다(control-api는 UID 10001): 파일은 0644, 보호는 디렉터리 권한으로.

교체·변경 절차는 아래 "비밀 파일 교체" 절.

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

업그레이드: 새 release 이미지 빌드(위 `build_images.sh`) → release 기록(저장소 루트, 커밋된 깨끗한 트리에서 `uv run python deploy/build/release_record.py <release> --central`) → `.env`의 `PUREIRON_CENTRAL_RELEASE` 변경 → `$C run --rm control-api migrate up` → `$C up -d --wait`. 기록은 이미지가 있어야 만들 수 있으므로 `.env`를 바꾸기 전에 빌드와 기록을 끝낸다. 첫 기동도 빌드 뒤에 기록한다.

release 이름은 release 기록이 있는 이름(`c0`, `c1`, … — `deploy/releases/central/<이름>.json`)만 쓴다. **`0.1.0-dev`는 CI·개발용 태그라 운영에 쓰지 않는다**: 같은 docker 데몬을 쓰는 CI(integration-compose·control-web-e2e)가 이 태그로 PR 코드를 다시 빌드해 덮어쓰므로, 다음 재시작(`restart: unless-stopped`·재부팅) 때 중앙이 PR 코드로 바뀐다.

## 비밀 파일 교체

명령은 `deploy/central`에서 실행한다. `$C`·`$S`는 위 첫 기동 절과 같다.

**비밀 파일은 같은 파일에 덮어쓴다(`cat 새파일 > 비밀파일`). `mv`·`cp --remove-destination`·편집기의 "새 파일로 저장 후 바꾸기"로 교체하지 않는다.** compose `secrets:`는 파일 하나를 bind mount하므로 컨테이너는 처음 파일(inode)을 계속 본다. Docker Desktop(WSL)에서 실측한 결과(M4-3 수정 r1, Caddy 2.11.7): 같은 파일 덮어쓰기는 재시작 없이 바로 보인다. `mv`로 바꾸면 컨테이너는 옛 내용을 계속 보고, 그 상태에서 `restart`·`up -d`는 `error mounting … no such file or directory`로 **컨테이너가 뜨지 않는다**(현장 polling도 Caddy를 거치므로 중앙 전체가 끊긴다. 재부팅 뒤 `restart: unless-stopped`도 같다).

- 실수로 `mv`했을 때 복구: `$C up -d --force-recreate <서비스>`(viewer_token·ui_basic_auth → `control-web`, control_db_password → `control-db control-api`). 컨테이너를 새로 만들어 지금 파일을 다시 마운트한다.
- `ui_basic_auth`는 기동 때만 읽는다 — 덮어쓴 뒤 `$C restart control-web`.
- `control_db_password`는 덮어쓰기만으로 값이 바뀌지 않는다 — 아래 "control_db_password 변경".

### viewer 토큰 교체

새 토큰을 먼저 쓰고 옛 토큰은 마지막에 끊는다. 토큰은 해시로만 저장되어 지운 원문을 되살릴 수 없다.

```bash
$C run --rm control-api token list            # 유효한 토큰 이름 확인(원문은 나오지 않는다)
OLD=<위 목록에서 화면이 지금 쓰는 viewer 토큰 이름>
NEW=control-web-$(date +%Y%m%d)               # 유효한 토큰 사이에서 유일한 이름(같은 날 또 하면 뒤에 -2)
```

아래 네 줄은 한 명령이다. 어느 단계든 실패하면 거기서 멈추고 `viewer_token`은 그대로다.

```bash
$C run --rm -T control-api token create --role viewer --name "$NEW" > "$S/viewer_token.new" &&  # 1. 새 토큰(다른 파일에)
  test "$(wc -l < "$S/viewer_token.new")" -eq 1 && test -s "$S/viewer_token.new" &&             # 2. 한 줄·비어 있지 않음
  cat "$S/viewer_token.new" > "$S/viewer_token" &&                                              # 3. 같은 파일에 덮어쓰기(mv 금지)
  rm "$S/viewer_token.new"
```

4단계는 화면을 새로고침해 표가 뜨는지 보는 것이다. Caddy는 요청마다 파일을 읽으므로 재시작은 필요 없다. 표를 확인한 뒤에만 옛 토큰을 끊는다(5단계).

```bash
$C run --rm control-api token revoke --name "$OLD"
```

- 1단계가 실패하면(이름 중복, DB 인증 실패, 이미지 없음) 비어 있는 `viewer_token.new`만 남는다. 원인을 고치고 이름을 확인한 뒤 다시 실행하면 덮어쓴다. 멈춰 있는 control-db는 `compose run`이 다시 띄우므로 실패 원인이 아니다.
- 2단계가 실패하면 새 토큰은 이미 유효하다. `viewer_token.new`를 지우고 `$C run --rm control-api token revoke --name "$NEW"`로 끊은 뒤 다시 한다.
- 3단계에서 파일이 비는 구간은 몇 바이트를 쓰는 동안뿐이고, 그 사이 요청은 401(fail-closed)이다.

### control_db_password 변경

postgres는 이 파일을 데이터 디렉터리를 처음 만들 때(initdb)만 읽는다. **파일만 바꾸면 DB 비밀번호는 그대로이고 control-api만 새 값을 쓰므로, 재시작 뒤 `/ui/fleet`은 503이고 `/api/v1/healthz`는 200이며 컨테이너도 healthy다.** healthz로는 드러나지 않는다. 그래서 DB 쪽을 먼저 바꾼다.

```bash
openssl rand -base64 24 > "$S/control_db_password.new" &&                      # 새 값(다른 파일에)
  test -s "$S/control_db_password.new" &&
  printf "ALTER ROLE pureiron PASSWORD '%s';\n" "$(cat "$S/control_db_password.new")" |
  $C exec -T control-db psql -v ON_ERROR_STOP=1 -U pureiron -d pureiron_control &&  # 1. DB 먼저
  cat "$S/control_db_password.new" > "$S/control_db_password" &&                 # 2. 같은 파일에 덮어쓰기(mv 금지)
  rm "$S/control_db_password.new" &&
  $C restart control-api                                                         # 3. 새 값으로 다시 연결
```

이 블록은 한 명령이다. 새 값 만들기나 `psql`이 실패하면 거기서 멈추고 `control_db_password`는 그대로다(`.new`만 지운다). `ALTER ROLE` 뒤에는 control-api가 이미 연 DB 연결만 계속 쓰이고 새 연결(CLI 포함)은 파일을 바꿀 때까지 실패하므로, 세 단계를 이어서 한다. 재시작 동안 `/ui/fleet`은 502다(실측 약 2초).

파일을 먼저 바꿔 버렸으면 DB를 파일 값에 맞춘다(컨테이너 안 psql은 비밀번호를 묻지 않는다). 값은 위 `openssl` 출력처럼 작은따옴표가 없어야 한다.

```bash
test -s "$S/control_db_password" &&
  printf "ALTER ROLE pureiron PASSWORD '%s';\n" "$(cat "$S/control_db_password")" |
  $C exec -T control-db psql -v ON_ERROR_STOP=1 -U pureiron -d pureiron_control &&
  $C restart control-api
```

## 이미지 신원

`pureiron/control-api`·`control-web`은 로컬 이미지만 쓴다(`pull_policy: never`, 현장 compose와 같은 규칙). 신원 정본은 release 기록 `deploy/releases/central/<release>.json`의 config digest다(`python deploy/build/release_record.py <release> --central`, 이미지 tar는 image store에 보관). control-db는 업스트림 `postgres:16`을 digest로 고정한다(`deploy/compose/control.dev.yml`과 같은 값). 중앙 이미지는 현장 번들·manifest에 들어가지 않는다.

## TLS와 루트 CA 내보내기

PoC는 Caddy `tls internal`(Caddy 로컬 CA)이다. 인증서 이름은 `PUREIRON_CENTRAL_HOST`(여러 개면 쉼표). 루트 CA는 `caddy-data` 볼륨에 있고, 볼륨을 지우면 바뀐다.

**IP 주소로 운영할 때**(현장 VM·브라우저가 `https://192.168.0.10:28443`으로 접속): IP로 접속하는 클라이언트(브라우저·curl·Python `ssl`)는 SNI를 보내지 않는다. 그래서 IP를 `PUREIRON_CENTRAL_HOST`에 넣는 것만으로는 안 되고 `PUREIRON_CENTRAL_DEFAULT_SNI`도 그 IP로 둔다. 기본값(`localhost`)이면 IP 접속은 TLS 핸드셰이크에서 끊긴다(alert 80, 리뷰 실측).

```bash
PUREIRON_CENTRAL_HOST="localhost, 192.168.0.10"
PUREIRON_CENTRAL_DEFAULT_SNI=192.168.0.10
PUREIRON_CENTRAL_BIND=0.0.0.0
```

DNS 이름(hosts 파일 포함)으로 접속하면 SNI가 가므로 `DEFAULT_SNI`는 필요 없다. 둘 이상의 IP로 접속받으면 `DEFAULT_SNI`는 하나만 되므로 나머지는 DNS 이름으로 접속한다.

```bash
$C cp control-web:/data/caddy/pki/authorities/local/root.crt ./pureiron-central-root.crt
openssl x509 -in pureiron-central-root.crt -noout -subject -fingerprint -sha256   # 지문을 따로 전달해 대조
curl --cacert pureiron-central-root.crt https://localhost:28443/api/v1/healthz
```

브라우저는 이 CA를 신뢰 저장소에 넣거나 경고를 감수한다. 현장 Updater(시스템 CA, urllib)가 이 CA를 신뢰하게 하는 설정은 M4 마감 VM 항목에서 정한다(M4-3 범위 밖).

## 첫 계약 전 강화 항목

- **admin·ci 경로를 바깥에서 막기**(G3·R2): 지금은 `/api/v1/*` 전체가 HTTPS 포트로 열려 있고 토큰 인증만 관문이다. 첫 계약 전에 `POST /sites`·rekey·token·`PUT /bundles`·offline-imports를 로컬(또는 publish runner) 출처로만 받게 제한한다. 이때 **경로 deny 목록(admin 경로를 골라 막기)으로 만들지 않는다** — 바깥에서 받을 경로의 **allow 목록**(현장 polling·보고 경로만)이나 **별도 listener**(admin용 포트를 로컬에만 bind)로 만든다. Caddy는 정규화한 경로로 handle을 고르고 upstream에는 원래 경로를 넘긴다(리뷰 실측: `/ui/../api/v1/sites` → `/api/v1/*`로 판정, control-api에는 원문 그대로). 그래서 Caddy와 control-api(Starlette)의 경로 해석이 다르면 deny 목록은 dot-segment(`..`)·`%2F` 인코딩으로 우회될 수 있다. `/api/v1/*`에서 원문 경로의 dot-segment·`%2F`를 거부하는 matcher도 함께 검토한다.
- 공인 인증서(ACME)·클라우드 VM(M6, PD-1). `tls internal` 루트 CA 교체 절차.
- 사람 관문을 basic auth → SSO 또는 control-api 사람 세션으로(범위 밖으로 둔 결정).
- Caddy 컨테이너는 업스트림 기본(root)으로 돈다 — 비root 실행·읽기 전용 루트 파일시스템.
- heartbeats 보존 정책(M4-0 백로그), 중앙 백업(control-db·`control-data` 볼륨).

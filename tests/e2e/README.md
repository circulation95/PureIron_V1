# e2e — Hyper-V VM 실측 (PD-24)

현장 호스트(Ubuntu 24.04)를 개발 PC의 Hyper-V VM으로 만들고, 실제 설치 번들·현장 번들·Updater를
돌려 plan M2 종료 조건을 확인한다. 단위·통합 테스트(fake 세계, compose)가 다루지 못하는 것 —
폐쇄망 부트스트랩, classic image store, systemd 재시작, 전원 리셋, 실제 마이그레이션·롤백 시간 — 이 대상이다.

## 파일 위치

| 위치 | 내용 |
|---|---|
| `m2d/run.py` | M2d 시나리오 진입점(단계 순서·CLI·결과 항목 39개). 현장 `SITE_A`(site-a, 10.77.0.10) 하나 |
| `gates/harness/` | 공용 하네스(M6-1, G2 nightly와 같이 쓴다): `release_pair`(`ReleasePair`·지금 쌍 `DEFAULT`·nightly 쌍 `nightly()`), `sites`(`SITE_A`·`SITE_B`·VM 준비), `site`(`Site`·원격 조작·시뮬레이터), `judges`(순수 판정), `trees`(트리 기대값·G2 100태그 트리), `context`(`Context`·상태 파일·`InfraError`), `buildhost`(WSL 호출·HEAD 빌드), `hyperv`·`golden`·`vm`, WSL 스크립트 `bundles.py`·`central.py` |
| `gates/` (G2) | `nightly.py`(실행기), `pytest.ini`·`conftest.py`(G2 pytest 틀), `test_g2_install.py`(G2-1), `quarantine.yaml`, 하네스 `g2`(세션)·`failures`(분류)·`report`(junit·이력·status)·`flake`·`preflight`·`github` |
| `site/` | VM에 복사하는 스크립트(`probe.py`·`crash_at.py`·`site_api.py`) |
| `infra/setup-host.ps1` | 1회 호스트 준비 |
| `tests/` | 단위 시험(G1 `unit`) |

## 1회 준비 (사용자, 관리자 PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File tests\e2e\infra\setup-host.ps1
```

- 현재 사용자를 **Hyper-V Administrators**에 넣는다 → **로그아웃 후 다시 로그인**해야 적용된다.
- 내부 전용 vSwitch `pureiron-int`(외부 NIC 없음)와 호스트 쪽 `10.77.0.1/24`를 만든다. `New-VMSwitch`가
  `0x800700B7`(내부 미니포트가 이미 있음 — 이전에 지워진 같은 이름의 흔적)로 실패하면 다른 이름으로
  `-Switch pureiron-e2e`를 주고, 실행할 때 `PUREIRON_E2E_SWITCH=pureiron-e2e`를 맞춘다. 호스트는 이 망을
  다른 곳으로 전달하지 않는다 — VM은 인터넷에 닿지 않는다.
- C: 여유: golden 2.1 GB + VM 차분 디스크 수 GB + 번들(회당 약 0.74 GB × 실행 수). 작업 디렉터리는
  `C:\PureIron_e2e`(`PUREIRON_E2E_ROOT`).
- 메모리: VM 3 GiB(`PUREIRON_E2E_VM_GIB`, 기본 3 — M6-0 실측 최저 여유 2.1 GB·OOM 0) + 체크포인트 복원. WSL VM(번들 빌드·Docker Desktop)이 페이지 캐시를 늦게 돌려줘
  복원이 `0x8007000E`로 실패할 수 있어, 하네스는 VM 시작·복원·리셋 전에 WSL 캐시를 비운다(`wsl -u root`).
- 한국어 등 지역화된 Windows: Hyper-V 통합 서비스 이름이 번역되므로 하네스는 서비스 ID로 고른다.

빌드 호스트(WSL runner)에는 `~/pureiron/keys/test`(test root·release key, `deploy/keys/README.md`),
image store(`~/pureiron/images`, `PUREIRON_IMAGE_STORE`)의 BASE·NEXT tar(`<이름>/{core,collector,web,site-db}.tar`),
`~/m2d-tools/qemu-img`(sudo 없이 `apt-get download qemu-utils liburing2 libaio1t64` → `dpkg -x`)가 있어야
한다. 저장소에는 `deploy/releases/<BASE·NEXT>.json`이 있어야 한다(`prepare`가 WSL 사본으로 동기화해
`version`·`schema.epoch`를 `release_pair.py`와 대조한다). `pureiron/<svc>:<NEXT>` 이미지가 WSL Docker에
없거나 config digest가 NEXT 기록과 다르면 `fault-release`가 store tar에서 `docker load`한다.

## release 쌍

실측할 release 쌍은 `gates/harness/release_pair.py`의 `ReleasePair`(지금 쌍 `DEFAULT`) 한 곳에 있다 — 설치
g1 = `base`(`BASE`), 업그레이드 `BASE`→`NEXT`, 헬스 실패 롤백 `FAULT`(`NEXT` 파생), 기대 epoch `epochs`. 판정
함수는 쌍을 인자로 받는다. 결과 항목 39개(`run.expected_items()`) 이름도
여기서 나온다(S4까지 30개, M5 마감에서 M5 확인 4개 + 전환기 경보 해제 1개를 더했고, M4 마감에서 경보 해제
자리를 "롤백 뒤 재시도"로 바꾸고 온라인 4개를 더했다).

| 실측 | BASE | NEXT | FAULT | c0 | 업그레이드 마이그레이션 |
|---|---|---|---|---|---|
| M2(run 20261005T055523Z, 30/30) | r0 `2026.10.0` (epoch 1) | r1 `2026.10.1` (epoch 2) | `r1f` `2026.10.1-fault1` | epoch 1 + 압축 chunk | 0006·0007(1h CAGG) |
| S3(run 20261009T231040Z, 30/30) | r1 `2026.10.1` (epoch 2) | r2 `2026.10.2` (epoch 3) | `r2f` `2026.10.2-fault1` | epoch 2(1h CAGG 있음) + 압축 chunk | 0008·0009(정의 jsonb, CAGG 실시간 집계, `project_state.historian`) |
| S4(run 20261017T132857Z, 30/30) | r2 `2026.10.2` (epoch 3) | r3 `2026.10.3` (epoch 4) | `r3f` `2026.10.3-fault1` | epoch 3(1m·1h CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, epoch 4 테이블 없음 | 0010~0012(`users`·`sessions`, `audit_log`, `alarms`·`alarm_journal`과 인덱스) |
| M5 마감(run 20261101T035728Z, 35/35) | r3 `2026.10.3` (epoch 4) | r4 `2026.11.0` (epoch 5) | `r4f` `2026.11.0-fault1` | epoch 4(0010~0012 테이블·인덱스, 1m·1h CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, `project_files` 없음 | 0013(`project_files`, 이름 붙은 인덱스 없음) + 첫 프로젝트 트리 적용 |
| **M4 마감(현재)** | r4 `2026.11.0` (epoch 5) | r5 `2026.11.1` (epoch 5) | `r5f` `2026.11.1-fault1` | epoch 5(0010~0013 테이블·인덱스, 1m·1h CAGG 실시간 집계, 0008·0009 열) + fixture 트리 + 압축 chunk | **없음**(같은 epoch — r4..r5 core 변경은 `migrate_lint.py`뿐) |

항목 이름 중 release가 들어가는 것은 쌍을 따라간다: `install bundle bootstrap + g1(r4) on offline VM`,
`r4→r5 on compressed epoch-5 DB`, `r5 schema after upgrade`, `rollback within 5 min to r4 digests`,
`r5 project tree applied …`, `r5' health failure → rollback (no alarm, r4 tree and definitions back)`,
`r5 retry after r5' rollback → succeeded`, `online: central-published r4 g2 → …`(M5 마감에서는 `g1(r3)`·
`r3→r4 … epoch-4`·`r4 schema`·`r4' health failure → rollback (transition, E_DEFINITIONS_LEFT)`·`to r3`·
`r4 tree bundle after transition rollback → succeeded, alarm cleared`).

스키마 판정은 이름 집합으로 비교한다(`ReleasePair`의 `caggs`·`base_columns`·`base_tables`·`base_indexes`·
`next_tables`, probe `db`의 `caggs`·`realtime_caggs`·`tables`·`indexes`·`columns`). c0는 BASE epoch 산출물이
있어야 하고, 업그레이드 뒤에도 BASE 산출물이 남아야 한다. NEXT가 epoch를 올리는 쌍이면 c0에 NEXT 테이블이
없고(`next_absent`) 업그레이드 뒤 생겨야 한다(`next_tables`). **M4 마감 r4→r5는 같은 epoch라 `next_tables`·
`next_indexes`가 빈 집합이고 두 판정을 뺐다**(빈 집합 검사는 늘 참이라 아무것도 확인하지 않는 항목이 남는다).
0013 `project_files`는 `base_tables`로 옮겼다. 쌍을 바꿀 때는 새 NEXT epoch의 migration SQL을 읽고 이
집합들을 함께 바꾼다(지난 NEXT 산출물은 BASE로 옮긴다).

### M4 마감에서 바뀐 판정

- **마이그레이션 없는 업그레이드**: `r4→r5`·crash `migrating` 2건은 `migrating` phase를 지나가지만(Updater는
  `migrate_to`가 없어도 phase를 기록한다) SQL은 돌지 않는다. c0와 r5 업그레이드 뒤 probe `db`는 실측에서
  바이트까지 같다(`test_harness.py` `C0_DB`·`NEXT_DB`) — 스키마 판정의 교차 실패는 지난 쌍의 c0(epoch 4,
  `R3_C0_DB`)로 고정한다.
- **NEXT·FAULT 트리(수정 r1, 리뷰 H1·M1)**: c0는 fixture 트리다. core는 같은 트리 재적용을 no-op
  (`changed=False`)으로 건너뛰므로, 업그레이드가 fixture를 실으면 정의를 다시 쓰지 않아 c0 시드 장비·태그가
  남고 ⓐ가 실패한다(로컬 실측 — `fixtures/m4c-tree-db.json` `next_same_tree` = c0). 그래서 NEXT 번들
  (업그레이드·재시도)은 **NEXT 트리**, FAULT 번들은 **FAULT 트리**를 싣는다 — fixture 사본에서 태그
  `line1/temp_sp`(`ReleasePair.tree_tag`, ⓑ PATCH 대상과 겹치지 않음)의 description만 `… (r5)`·`… (r5f)`로
  바꾼 것(`bundles.release_tree`, `buildhost.release_tree`가 run 번들 디렉터리 `trees/<release>`에 만들고 WSL 경로로 넘긴다).
  `judge_tree`는 트리 기대값에 더해 c0 시드 retire(`seed_retired`)와 그 태그의 DB 정의 값(`definition`)을 본다 —
  같은 epoch에서 늘 참인 항목이 되지 않게. crash·온라인 ②·ⓒ·ⓓ 번들은 fixture 경로 그대로다(트리를 판정하지
  않거나 fixture 기준 판정).
- **FAULT = 일반 롤백**: r4도 트리 release라 Updater 백업에 트리가 있고 롤백이 트리·정의까지 되돌린다 — 경보
  없음, 메트릭 `updater_failed_manual 0`, 보고 `code` `E_HEALTH`. FAULT 트리(c0에는 드래프트가 없어 게이트
  통과)를 실어서, 롤백 뒤 `GET /project` content_hash·DB 태그 정의 값이 fixture(c0)로 돌아왔는지
  (`judge_fixture_tree`)를 본다. 전환기 경보 `E_DEFINITIONS_LEFT`·`judge_attempt(expect_alarm=…)`는 단위
  시험이 계속 고정한다(r3 현장 경로).
- **롤백 뒤 재시도**(M5 마감의 경보 해제 자리): 그 상태에서 정상 r5(NEXT 트리) → `succeeded`, 경보 없음,
  트리·정의 값 = NEXT 트리(롤백된 fixture 위라 정의를 다시 쓴다).
- **온라인 4개**(`online` 단계, 아래 절).

### M5 마감에서 바뀐 판정

- **전환기 롤백**: r4는 프로젝트 트리를 적용하는 첫 release다. `c0`(r3)에서 `FAULT`(r4f)는 r4 이미지가
  트리를 적용한 뒤 헬스에서 실패하고, 직전 r3 core는 트리를 모르므로 롤백은 project_state만 되돌린다 —
  설계상 `failed` + 경보 `g<gen>: E_HEALTH E_DEFINITIONS_LEFT`, 보고 `failed_manual` false·`logs`에
  `E_DEFINITIONS_LEFT`, 메트릭 `updater_failed_manual 1`(updater README runbook). `judge_attempt`의
  `expect_alarm`·`check_metric`이 이를 본다(제품 결함이 아니다). 이어서 정상 r4 트리 번들을 적용하면
  `succeeded`이고 경보가 지워져야 한다(트리 release 성공만 지운다, 리뷰 N-1). crash의 `rolling_back` 2건은
  r4 위의 r4f라 트리 백업이 있어 경보가 없어야 한다(기존 판정 그대로).
- **M5 확인 4개**(업그레이드 직후 r4 위, 낮은 gen 거부 다음):
  ⓐ 트리 적용 — `GET /api/v1/project`의 version·content_hash = 번들 트리, `site_rev == base_site_rev`,
  DB `project_files` 행 수 = 트리 파일 수(fixture 4), retire되지 않은 장비·태그·알람 = 트리(c0 시드 장비·
  태그는 트리 교체로 retire). ⓑ 현장 PATCH — core 컨테이너에서 `pureiron-core users add e2e-admin --role admin`
  (비밀번호는 VM에서 만든 파일, 호스트로 나가지 않는다) → `site/site_api.py`가 web 경유로 로그인(세션 쿠키)·
  `PATCH /api/v1/project/tags/line1/temp`(record)·`/alarms/temp-high`(setpoint), 비안전 요청마다
  `Origin`(= web)·`Sec-Fetch-Site: same-origin`(M3c-0 OriginGuard), 로그인 1회(throttle) → site_rev +2,
  base 그대로, DB 정의 = PATCH 값. ⓒ 드래프트 게이트 — absorb하지 않은 repo(`bundles.py m5-project`: fixture
  사본에서 `absorbs`만 뺀 것. fixture의 `absorbs: {site-a: 3}`은 VM site_rev 2보다 커서 그대로면 게이트가
  통과한다)로 만든 번들 → exit 1·`E_DRAFT`·core `StartedAt`·이미지 불변. ⓓ export→absorb→재적용 —
  VM `pureiron-site project export --out` + `state/enrollment.json`을 호스트로 scp → WSL `bundles.py absorb`
  (`pureiron-project absorb --draft --site-key` → `gate --draft`) → 그 디렉터리로 번들(`site --project-dir`) →
  `succeeded`, content_hash = 흡수한 트리, `base_site_rev == site_rev`(= 2), PATCH 값 유지, 편집 잠금 해제.
- probe가 `metrics`(`state/metrics/updater.prom`)와 `db`의 `project_files`·`device_ids`·`tag_paths`·
  `alarm_ids`·`tag_defs`·`alarm_defs`를 더 낸다. gen 배정은 `run.scenario_gens()`.

## 온라인 단계 (M4 마감 ★D5)

`crash` 뒤·`report` 앞. `c0`(BASE g1, 폐쇄망 상태)로 복원한 VM을 WSL의 중앙 c0에 붙여 온라인 경로를 실측한다.
`report`의 외부 접속 판정이 이 구간까지 누적한다 — VM → `10.77.0.1`은 내부 전용 망이라 `ext_out_v4` 제외
대상(`golden.OUT_RULES`의 `{cidr}`)이고 IP 직접 접속이라 DNS 질의가 없다(F6). 새 규칙을 더하지 않는다.

1. **중앙**: `gates/harness/central.py up`(WSL, 저장소 사본에서) — `deploy/central` compose를 프로젝트
   `pureiron-e2e-central`·release `c0`(이미지 config digest = `deploy/releases/central/c0.json`, 없으면 image
   store `central/c0/*.tar`에서 적재)로 **새로** 띄운다(볼륨째 내리고 다시). 바인드·인증서 이름·SNI
   `10.77.0.1`, HTTPS 28443, `PUREIRON_CONTROL_POLL_S=10`. `migrate up`(기동 때 자동 실행 안 함) → CLI로
   admin·ci·viewer 토큰을 **파일로만** 발급(`~/pureiron-e2e-central/secrets`, 0700, 저장소·artifacts 밖) →
   `up -d --wait` → 루트 CA 내보내기·SHA-256 지문 → 그 CA로 HTTPS `healthz` 200.
2. **현장(VM)**: CA를 `/usr/local/share/ca-certificates/` + `update-ca-certificates`(D8, VM에서 지문을 다시
   재서 중앙 값과 대조), `updater.env`에 `PUREIRON_CONTROL_API=https://10.77.0.1:28443/api/v1`·
   `PUREIRON_POLL_INTERVAL_S=10`, VM `state/enrollment.json`을 호스트로 받아 `central.py register`(현장 token은
   WSL 비밀 파일에만) → `central.py site-token`의 출력을 **ssh stdin으로만** `pureiron-site secret set
   control_api_token`에 넘긴다(명령 줄·파일·결과·로그에 남기지 않는다) → Updater 재시작.
3. 항목:
   - ① `online: HTTPS heartbeat …` — `/fleet`(Caddy HTTPS, viewer 토큰) connection `online`, heartbeat 수위 > 0,
     current = g1(BASE), CA 지문 일치, 등록 site_id. 결과의 `ca_fingerprint`·`registered`는 하네스가 만든 값끼리의
     비교라 전송 손상 확인일 뿐이다 — VM이 사설 CA를 실제로 믿는지는 `connection_online`·`heartbeat_seq`(Updater가
     HTTPS로 보낸 heartbeat)가 판정한다.
   - ② `online: central-published r4 g2 …` — 하네스가 BASE g2(fixture 트리)를 만들어 ci 토큰으로 `PUT` → 201,
     `/fleet` desired·max_published_gen = 2 → Updater가 polling으로 받아 적용 `succeeded`(기존 `judge_attempt`,
     경보·메트릭 0). **gen 2인 이유**: 15분 실측의 fleet PR이 site-a gen 2 → 3이어야 check-gen의
     `new == old + 1`·`new > max_published_gen`을 함께 만족한다.
   - ③ `online: ApplyAttempt report …` — `/fleet` current = g2·BASE·v1(`source` online), last_attempt = g2
     `succeeded`(`source` online).
   - ④ `online: central web down …` — control-web(Caddy)만 `compose stop`(현장이 닿는 유일한 입구, control-api
     직접 포트 없음) → `3 x poll + 15 s` 동안 현장 core ready·실행 이미지·core `StartedAt` 불변, Updater
     active·재시작 없음·경보 없음·새 시도 없음 → control-api 컨테이너 안에서 본 `/fleet`(`central.py fleet
     --direct`)이 `stale` → `compose start` 뒤 `last_seen`이 더 새로운 `online`.
4. 판정 fixture는 실측이다(`tests/fixtures/m4c-fleet-rows.json` — 워커가 같은 `central.py`로 띄운 중앙에 VM
   Updater와 같은 서명 heartbeat·보고를 보내 단계별로 남긴 `/fleet` 응답). 각 판정은 자기 단계 행에서 통과하고
   다른 단계 행에서 실패한다(`test_online.py`).

중앙은 단계가 끝나면 내린다. `--keep-central`이면 남긴다 — VM은 r4 g2·online 상태, 중앙 DB에 site-a 등록·g2
게시가 있어 15분 실측(fleet PR gen 3 → publish → VM r4→r5)이 그대로 이어 쓴다. 남긴 중앙의 값:
`central.py status`(URL·CA 파일·지문·토큰 파일 경로, 비밀값 없음 — `results.json` 온라인 ① `ca_sha256`와
`state.json` `central`에도 있다). 정리: `run.py central-down` 또는 WSL `central.py down`(볼륨·비밀 디렉터리
삭제).

r3부터 core API에 사용자 세션 인증이 있다. 하네스(probe)가 읽는 HTTP 경로 `/api/v1/readyz`·`GET /api/v1/project`는
무인증이라 그대로 쓴다(Updater의 편집 잠금은 Updater 토큰).

## 실행

```bash
uv run python tests/e2e/m2d/run.py all             # prepare → vm → install → scenarios → crash → online → report
uv run python tests/e2e/m2d/run.py all --keep-central   # online의 중앙 c0를 남긴다(15분 실측이 이어 쓴다)
uv run python tests/e2e/m2d/run.py crash           # 한 단계만(앞 단계 context: C:\PureIron_e2e\state.json)
uv run python tests/e2e/m2d/run.py all --from scenarios
uv run python tests/e2e/m2d/run.py central-down    # 남긴 중앙 c0 정리(볼륨·비밀 디렉터리 삭제)
uv run python tests/e2e/m2d/run.py destroy         # VM·차분 디스크 삭제(golden 유지)
```

부분 재실행(`<step>`, `--from`)은 VM이 켜져 있고 SSH가 되어야 한다(복원·리셋 직전에 카운터를 읽는다) — VM이
꺼졌거나 `state.json`이 이 하네스 이전 형식이면(`seed_rows` 0 → 이력 보존 판정이 거짓 FAIL) `--from vm`으로
다시 시작한다.

결과는 `artifacts/e2e/m2d-<시각>/results.json`(항목별 PASS/FAIL·측정값)과 `bundle-metrics.json`
(번들별 크기·이미지 크기·빌드/검증/로드 시간·phase 시간).

## 단계별 확인

| 단계 | 확인 |
|---|---|
| `prepare` | WSL 사본에서 FAULT(`r3f`: NEXT 이미지에 collector가 세션을 맺지 않는 파생 → `/readyz` 503. 로컬 NEXT 이미지 digest가 기록과 다르면 store tar를 다시 load, 파생 뒤 core·web digest == NEXT 기록 확인), release 기록 version·epoch·앱 envelope [epoch, epoch + 1] 대조, 설치 번들 **실빌드**(`fetch_debs.sh`·`build_updater.sh`, manifest release = BASE), g1(BASE) |
| `vm` | golden(클라우드 이미지 → 64 GiB 동적 VHDX, 읽기 전용) 위 차분 디스크, seed ISO(cloud-init: 사용자·고정 IP·OS phone-home 끄기·nftables 감시 테이블) |
| `install` | runbook 2단계(메타데이터만 전용 디렉터리에 추출 → `bootstrap-verify.sh` sha256·root 지문 대조) → `install.sh --offline-time-confirmed` → enroll → g1 → 서비스 시작. `/readyz` 200, overlay2 + classic store, 실행 이미지 `.Id` == BASE config digest. 압축 chunk 이력 시드, c0 기반(DB epoch == BASE epoch·BASE 산출물·압축 chunk·이력 행, NEXT가 epoch를 올리는 쌍이면 NEXT 테이블 없음) 확인 후 체크포인트 `c0` |
| `scenarios` | `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1, 같은 gen 재빌드는 no-op exit 0(R-7 멱등) — 모두 core `StartedAt`·실행 이미지 불변 / `c0`에서 BASE→NEXT(압축 DB 위, r4→r5는 마이그레이션 없음) 성공 + NEXT 스키마(epoch 5·0010~0013 테이블·인덱스·1m/1h CAGG와 실시간 집계·0008/0009 열·압축 chunk 유지) → 낮은 gen `E_GEN` 거부 → M5 확인 ⓐ~ⓓ(ⓐ = NEXT 트리·정의 값·c0 시드 retire) / `c0`에서 FAULT(FAULT 트리) → 5분 안에 BASE digest + `failed`, 경보 없음, BASE 트리·정의 값 복원 → 정상 NEXT(NEXT 트리)로 재시도 `succeeded`(정의 값 = NEXT) |
| `crash` | phase(`fetched`~`rolling_back`)마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Type Reset -Force` 각 1회 → 10분 안에 수렴(결과·실행 이미지·편집 잠금 해제) |
| `online` | `c0` 복원 → WSL 중앙 c0(`central.py up`) → VM CA 신뢰·`updater.env`·등록·현장 token·재시작 → ① HTTPS heartbeat `/fleet` online ② 중앙 게시 BASE g2 다운로드·적용 ③ 온라인 보고 current·last_attempt ④ control-web 중지 → 현장 정상·`/fleet` stale → 재기동 뒤 online. `--keep-central`이 아니면 중앙을 내린다 |
| `report` | nftables 외부 접속 카운터(`ext_out_v4`·`dns_out`·`ext_out_v6`·`ext_fwd`)의 **누적** 합 0 (`vm` 단계 기준값도 따로 0이어야 한다). 선언 장비 폴링 `field_fwd`는 판정에서 빼고 누적값만 증거로 남긴다. online 구간 포함. `results.json`의 `missing`은 39개 중 기록되지 않은 항목 |

거부 보고는 번들 sha256 12자로 찾는다(`reports/<started>-g<gen>-<sha12>.json`) — 커밋 전 거부는 서명을
믿기 전이라 gen이 `unknown`이다. 낮은 gen 번들은 적용된 gen보다 **뒤에** 빌드한다 — 먼저 만든 번들은
cert 단계의 `max_seen_created_at` 역행(`E_CERT`)에 먼저 걸린다.

### crash 주입 방식

폐쇄망 현장에서 실제로 일어나는 흐름 그대로다. 운영자가 창(window)이 조금 뒤(150 s)에 열리는 번들을
`pureiron-site apply`로 스테이징하면 시도는 `verified`로 대기하고, 창이 열리면 **서비스**가 이어서 적용한다.
VM 쪽 `site/crash_at.py`가 상태 파일(원자적 rename)을 inotify로 보다가 목표 phase에 들어간 순간 실행 중인
프로세스를 `SIGSTOP`으로 얼려 그 phase에 붙잡아 두고, `kill`이면 `systemctl kill -s KILL`, `reset`이면
호스트가 `Restart-VM -Type Reset -Force`(전원 리셋)를 건다. `fetched`(번들 검증)는 CLI가 실행하므로 CLI를 대상으로 한다.
`migrating`은 실제 마이그레이션이 일어나도록 `c0`(BASE epoch)에서 BASE→NEXT로, `rolling_back`은 FAULT로 일으키고,
나머지 phase는 NEXT↔BASE를 번갈아 간다(순서는 `run.crash_plan()`).

서비스 tick 주기는 e2e에서 `PUREIRON_POLL_INTERVAL_S=10`(기본 60)으로 줄인다.

판정(모두 참이어야 통과, 결과의 `checks`): 결과(`succeeded`/`failed`) · **경보 없음·보고 `failed_manual`
false**(`last_result`는 `failed_manual`도 `failed`로 접는다) · `applied_gen`(성공이면 새 gen, 실패면 그대로) ·
실행 이미지 digest · 편집 잠금 해제 · 이력 행 보존 · 감시자 종료 코드 0(얼린 프로세스 있음) · 주입 효과
(reset이면 boot id 변경, 서비스 kill이면 `NRestarts` 증가) · service 대상이면 CLI 스테이징이 창 열림 전에 끝남
(아니면 CLI가 phase를 직접 실행해 쉬는 서비스를 죽이는 거짓 통과가 된다).

창 여유 flake(E10, M6-2a): CLI 스테이징이 창이 열린 뒤에 끝났지만(`margin_s <= 0`, `rc` 0) 그 밖의
판정이 모두 참이면 그 건은 `infra`로 보고 1회 다시 한다(`judges.judge_window_flake`) — FAULT가 아니면
crash 없이 `prev` release로 먼저 되돌리고(gen 하나), from_c0 건은 c0 복원 뒤 같은 gen으로. 첫 시도는
결과의 `e10.first`에 남고 항목 이름·개수는 그대로다. 재시도도 같으면 실패로 남는다.

crash 결과의 `stage`는 스테이징 진단 기록이다: `target`, 쓴 ssh 클라이언트 경로(`ssh`), 마지막 `apply`의 `rc`,
시도 횟수 `tries`(exit 2 = 서비스 tick과 잠금 경쟁이면 2 s 뒤 다시, 최대 30회), 창까지 남은 초 `margin_s`(음수 = 창 뒤),
시도별 `attempts`(`rc`·걸린 초 `s`·그때 감시자 ssh가 살아 있었는지 `watcher_alive`). `staged_before_window`가
거짓이면 마지막 시도의 `stdout_tail`·`stderr_tail`도 남는다. CLI 대상(`fetched`)은 감시자가 죽인 `apply`의 `rc`만 있다.

crash 지점의 실제 의미: 감시자는 상태 파일이 그 phase로 **저장된 직후** 서비스 cgroup 전체를 `SIGSTOP`한다.
컨테이너(dockerd 소유)는 계속 돌기 때문에 `migrating`(약 2 s)은 마이그레이션 SQL 도중이 아니라 "phase 기록 뒤,
다음 기록 전"을 끊는다 — SQL 중간 중단은 core 통합 테스트(0006·0007 crash 상태)가 맡는다. `fetched`는 서명 검증
뒤 gen이 기록된 다음(이미지 적재 중), `verified`는 CLI가 스테이징을 마치고 창을 기다리는 동안이다.

### 외부 접속 카운터

체크포인트 복원은 VM 메모리(카운터 포함)를 c0 시점으로, 전원 리셋은 0으로 되돌린다. 그래서 하네스는 복원·리셋
**직전**마다 카운터를 읽어 누적하고(`nft_mark`), 직후 기준을 c0 값·0으로 다시 잡는다. 외부 DNS 패킷은 모두
systemd-resolved가 보내므로(컨테이너 질의도 Docker 내장 DNS → resolved), seed가 resolved를 디버그 로그로 두고
하네스가 표시마다 조회 이름을 모은다(`dns_lookups`) — 카운터가 0이 아니면 그 이름이 출처 증거다.
`dns_lookups`의 이름 자체는 판정 대상이 아니다: `core` 같은 컨테이너 서비스 이름은 그 컨테이너가 없는 동안
(crash 구간) Docker 내장 DNS가 resolved로 넘긴 것이고, resolved가 로컬에서 답하면(`dns_out` 0) 외부 접속이 아니다.

`field_fwd`: 번들 트리가 선언한 현장 장비(`devices/*.yaml`의 modbus TCP host·port, 하네스가 fixture에서 읽어
seed에 넘긴다)로 가는 eth0 전달은 `ext_fwd` **앞** 규칙이 세고 `accept`로 이 감시 체인만 빠져나간다(docker 체인은
그대로 거친다). collector의 장비 폴링은 제품 의도대로의 동작이라 외부 접속이 아니다 — r4 트리(fixture `acme`의
`plc-1` = 10.0.0.5:502)부터 생긴다(VM 1차 실측 `ext_fwd` 216). 정확히 선언된 host:port TCP만 빠지고 그 밖의
eth0 전달은 여전히 `ext_fwd`다. `field_fwd`는 0 초과를 요구하지 않는다.

## G2 nightly (M6-2a)

밤마다 PC가 master HEAD를 빌드해 현장 VM 2대에 설치하고 결과를 GitHub에 남긴다. 2a는 시나리오 G2-1
하나와 수동 실행(`.github/workflows/nightly.yml`, `workflow_dispatch`)만 있다 — cron은 M6-2b.

- 현장: site-a(10.77.0.10) = **BASE**(git 태그가 있는 최신 `deploy/releases/r*.json`), site-b
  (10.77.0.11) = **NEXT**(HEAD 빌드 `n<sha7>`, 버전 `<BASE 다음 번호>-nightly.<sha7>`). NEXT는 WSL
  `bundles.py nightly-release`가 HEAD git 사본에서 `build_images.sh`로 빌드하고 `LABEL
  pureiron.nightly=<sha>` 파생을 얹어 WSL nightly 작업 디렉터리(`~/m2d-e2e/work-nightly/{releases,store}`
  — `run.py`의 `work/`와 나눈다, BASE image store는 같은 실제 store를 가리킨다)에만 기록한다.
  나누기 전에 `nightly-release`를 `work/`에서 돌린 적이 있다면 `~/m2d-e2e/work/store/n*`와 그 이미지
  태그(`pureiron/*:n*`)는 이제 어느 정리도 보지 않는다 — 한 번 손으로 지운다(`rm -rf
  ~/m2d-e2e/work/store/n*`, `docker image ls 'pureiron/*' | grep ' n'`).
- 장비: 각 VM 안에 dummy 인터페이스 `10.0.0.5`(fixture 장비 주소)와 시뮬레이터 컨테이너
  (`e2e-simulator`, host network, :502, 제품 compose 밖). g1 트리 = fixture 사본 + 시뮬레이터 태그
  100개(`trees.g2_tree`).
- G2-1(현장마다): 설치(ready·overlay2·classic) · 실행 이미지 == 그 현장 release digest · c0 뒤 5분 안에
  100태그 전부 GOOD(`GET /tags`, viewer 로그인) · 장비 CONNECTED(`GET /devices`) · 부팅 이후 외부 접속
  카운터 0. "5분 안에"는 각 현장 `bring_up`이 c0 직후부터 live를 읽어 증명한다(직렬 모드 포함 — 판정이
  다른 현장 부팅 뒤에 시작해도 다시 읽지 않는다). 기록 `live_s`(c0부터 초)가 300보다 크면
  `live_in_time` 거짓으로 실패다. collector(compose bridge) → `10.0.0.5`는 VM 자신의 주소라 input으로 들어와 `ext_out_v4`·
  `ext_fwd`·`field_fwd` 어디에도 세지지 않고, 시뮬레이터 응답은 목적지가 docker bridge(172.16/12)라
  제외 대상이다(`golden` 모듈 docstring).
- 실패 분류(`harness/failures.py`): **호출 자체**의 실패 = `infra`(실패한 시험만 1회 다시) —
  `InfraError`, ssh·scp 실패(종료 코드 255), wsl.exe 호출 실패(스크립트가 시작되지 않음·wsl.exe 오류·
  스크립트가 알린 환경 탓 exit 75, bash 범위 0~255 밖의 종료 코드·"instance has terminated" = 도중에
  WSL VM이 죽음), Hyper-V, 시간 초과. **호출된 제품 스크립트**의 실패 = `product`(다시
  돌리지 않음) — 원격 명령의 0 아닌 종료(`install.sh`·`pureiron-site` 등, `RemoteCommandError`), WSL
  빌드·번들 스크립트의 0 아닌 종료(`WslScriptError`), HEAD 빌드 실패, 단언 실패. junit `failure_class`
  속성과 `summary.json`에 남는다. 원격 명령이 스스로 255로 끝나면 ssh 실패와 구분되지 않아 infra다.
- 알림: product가 2회 연속이면 이슈(라벨 `g2-nightly`), **같은 시험이 2회 연속 infra**(재시도 뒤)여도
  이슈(라벨 `g2-nightly-infra`) — 시간 초과로 드러난 제품 멈춤이 "error"로만 쌓이지 않게. pytest
  전체가 시간 제한에 죽거나 크래시해 junit이 없으면 시험 id `pytest`(infra) 한 줄로 이력에 남으므로 이
  경우도 이틀째에 이슈가 된다(`summary.json`의 `pytest_rc`, -1 = 시간 초과). 시간 초과였으면 그날은
  다시 돌리지 않는다(멈춘 제품은 재시도로 낫지 않는다). 크래시면 전체를 1회 다시 돌린다.
- 시간 예산: workflow `timeout-minutes: 120`을 넘기면 Actions가 job을 죽여 status·이력이 남지 않는다.
  그래서 준비 10분 + run 90분(preflight 시작부터 첫 실행·복구·재시도 — pytest 시간 제한(첫 75분·재시도
  30분)을 남은 시간으로 줄이고, 10분도 남지 않으면 재시도를 건너뛴다) + 뒷정리 15분(collect 명령당 60초
  × 10·publish·cleanup)으로 나눈다(`nightly.py` 상수, 합이 120분 미만인지 시험이 본다).
- preflight: foundation-ci 대기를 먼저 하고, WSL echo가 3번 모두 답하지 않으며 foundation-ci가 돌지
  않는 것이 확인될 때만 WSL을 재시작한다(재시도 전 복구도 같다). Actions 안에서 foundation-ci 상태를
  못 읽으면(`gh run list` 실패) infra로 멈춘다(로컬은 `warnings`에만).

```powershell
$env:PUREIRON_E2E_SWITCH = "pureiron-e2e"   # setup-host.ps1 -Switch 값
uv run python tests/e2e/gates/nightly.py preflight --judge-only   # 판단만(조치 없음)
uv run python tests/e2e/gates/nightly.py all --dry-run            # GitHub에 쓰지 않음(VM·결과 정리는 한다)
uv run pytest -c tests/e2e/gates/pytest.ini tests/e2e/gates -m g2 --collect-only
```

결과: `C:\PureIron_e2e\results\g2-<run>\`(junit.xml·summary.json·preflight.json·publish.json·
`<site>\`의 updater.json·메트릭·journal·compose 로그, 14일 뒤 preflight가 지운다). 번들
`bundles\<run>\`은 run 끝에 지운다. 환경변수: `PUREIRON_G2_MIN_MEM_GB`(8)·`PUREIRON_G2_MIN_DISK_GB`
(40)·`PUREIRON_G2_CI_WAIT_S`(1800)·`PUREIRON_G2_SERIAL=1`(VM 2대를 차례로). G2 시험은
`gates/pytest.ini`로만 수집된다(G1 `unit`에는 수집 자체가 빠진다).

`--dry-run`은 GitHub 쓰기(이력·status·이슈·임시 브랜치 삭제)만 출력으로 바꾼다 — `preflight`·`all
--dry-run`도 남은 `pureiron-site-a`·`pureiron-site-b` VM(run.py의 site-a VM과 이름이 같다)과 14일 넘은
결과는 **실제로 지운다**. `all`이 예상 밖 예외로 멈추면 시험 id `runner`(infra)를
`runner-summary.json`에 남기고 exit 2다 — 그 run의 `summary.json`이 이미 있으면(collect·publish·
cleanup에서 멈춤) 덮어쓰지 않고 그것으로 publish를 다시 시도한다(이력에 이미 들어간 run은 다시 쓰지
않는다).

## 단위 테스트

`tests/e2e/tests/` — 공용화 불변 조건(`test_harness_shared.py` — 항목 39개 이름·순서, `ReleasePair` 기본값 = 옛 상수, 현장 둘의 상태 파일·이름 분리, 롤백 5분 경계, VM 3 GiB)·phase 순서·상태 읽기·nft 규칙(현장 장비 `field_fwd` 규칙 위치·끝점 추출·외부 접속 판정)·seed ISO 왕복·PowerShell 인용·수렴 판정·release 쌍(BASE·NEXT 기록 대조 필수·crash 순서·항목 39개·단계 순서·c0/NEXT 스키마 집합 판정(같은 epoch 실측값, 지난 쌍 c0 교차)·전환기 경보·FAULT 트리 복원·M5 확인 판정(트리·PATCH·absorb·재적용)·gen 배정·NEXT/FAULT 트리 판정(로컬 실측 `fixtures/m4c-tree-db.json` — 같은 트리 재적용 no-op에서 ⓐ 실패, NEXT 트리에서 통과, 롤백·재시도 정의 값)·업그레이드/FAULT/재시도 배선(가짜 `apply`))·온라인 판정(`test_online.py` — 실측 `/fleet` 행으로 단계별 통과·교차 실패, CA 지문 표기, `central.py` 값·토큰을 출력에 내지 않음, 화면 비밀번호·현장 token이 명령 줄에 없고 stdin으로만 감 — `basic_auth_line`·`site_online`·`Vm.run(input=)`)·store symlink 다시 연결·`ensure_image` 적재 조건·스테이징 진단 기록(`stage_for_window`)·G2(`test_harness_g2.py` — 현장별 디렉터리·seed 바이트 동일·
`nft_mark` site id·E10 재시도, `test_nightly_pair.py`, `test_trees_g2.py`, `test_frame_g2.py`,
`test_nightly_runner.py`, 실패 분류 `test_failure_split.py`). G1 `unit`에 포함된다.

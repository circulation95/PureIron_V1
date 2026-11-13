# e2e — Hyper-V VM 실측 (PD-24)

현장 호스트(Ubuntu 24.04)를 개발 PC의 Hyper-V VM으로 만들고, 실제 설치 번들·현장 번들·Updater를
돌려 plan M2 종료 조건을 확인한다. 단위·통합 테스트(fake 세계, compose)가 다루지 못하는 것 —
폐쇄망 부트스트랩, classic image store, systemd 재시작, 전원 리셋, 실제 마이그레이션·롤백 시간 — 이 대상이다.

## 파일 위치

| 위치 | 내용 |
|---|---|
| `m2d/run.py` | M2d 시나리오 진입점(단계 순서·CLI·결과 항목 39개). 현장 `SITE_A`(site-a, 10.77.0.10) 하나 |
| `gates/harness/` | 공용 하네스(M6-1, G2 nightly와 같이 쓴다): `release_pair`(`ReleasePair`·지금 쌍 `DEFAULT`·nightly 쌍 `nightly()`), `sites`(`SITE_A`·`SITE_B`·VM 준비), `site`(`Site`·원격 조작·시뮬레이터), `judges`(순수 판정), `trees`(트리 기대값·G2 100태그 트리), `context`(`Context`·상태 파일·`InfraError`), `buildhost`(WSL 호출·HEAD 빌드), `hyperv`·`golden`·`vm`, WSL 스크립트 `bundles.py`·`central.py` |
| `gates/` (G2) | `nightly.py`(실행기 — CLI·단계 순서·시간 예산), `pytest.ini`·`conftest.py`(G2 pytest 틀·실행 순서·메모리 감시), 시험 `test_g2_install.py`(G2-1)·`test_g2_rollback.py`(G2-4 + G2-6 ③)·`test_g2_faults.py`(G2-6 ②)·`test_g2_no_external.py`(맨 끝), `quarantine.yaml`, 하네스 `g2`(세션·c0 복원)·`scenarios`(시나리오)·`order`(실행 순서 표)·`memwatch`(세션 최저 메모리)·`failures`(분류)·`report`(junit·이력·status)·`collect`·`publish`·`runner`(실행기 보호)·`flake`·`preflight`·`github` |
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

## G2 nightly (M6-2)

밤마다 PC가 master HEAD를 빌드해 현장 VM 2대에 설치하고 시나리오(G2-1·G2-4·G2-5·G2-6 ②~⑤·G2-8 PR
전·외부 접속 0, 일요일에는 crash 16건 더)를 판정해 결과를 GitHub에 남긴다. 요약은 [testing SOT 12절](../../docs/wiki/sot/testing.md), 이 절이
세부의 정본이다.

- 언제(M6-3 ★H13): `.github/workflows/nightly.yml`은 **`workflow_dispatch`만** 받는다(입력 `ref` 기본
  master·`suite` 기본 g2). 개발 PC의 **Windows 작업 스케줄러**가 매일 **03:17(PC 로컬 시각 = KST)**에
  `gh workflow run nightly.yml --ref master -R circulation95/PureIron_V1 -f ref=master -f suite=<묶음>`을
  부른다 — 일요일은 `weekly`, 나머지 날은 `g2`. GitHub schedule(cron)은 지웠다: 이 저장소의 schedule
  이벤트는 매번 3시간 30분~7시간 30분 늦게 만들어져(foundation-ci·nightly 실측) 2시간 늦은 시작 가드가
  매일 그 밤을 건너뛰었다. PC가 꺼져 있으면 실행이 아예 없으므로(놓친 예약을 켠 뒤 실행하지 않게
  등록한다) 늦은 시작 가드(`harness/schedule.py`)도 지웠다.
- 작업 스케줄러 등록(사용자가 1회, 일반 사용자 PowerShell — 관리자 필요 없음):

  ```powershell
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1                 # 등록
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -Dispatch -WhatIf -Date 2026-11-15   # 요일 → suite 확인(gh 안 부름)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -Unregister     # 해제
  ```

  등록은 스크립트를 `<E2E 루트>\bin\nightly-dispatch.ps1`로 복사하고(브랜치를 바꿔도 작업이 깨지지
  않게) 그 사본을 `-Dispatch`로 부르는 작업 `PureIron G2 nightly`를 만든다. 조건: **사용자 로그온
  세션에서만**(gh가 Windows 자격 증명 관리자 keyring의 토큰을 쓴다 — 로그오프 상태에서는 돌지 않는다),
  놓친 예약은 나중에 실행하지 않음, 동시에 하나만, 10분 제한. 저장소·gh 계정·ref·시각은 인자
  (`-Repo`·`-GhUser`·`-Ref`·`-At`, 기본 `circulation95/PureIron_V1`·`circulation95`·`master`·`03:17`).
  `-Dispatch`는 `gh auth token -u <계정>`을 그 프로세스 환경변수(`GH_TOKEN`)에만 두고 끝에 지운다 —
  토큰을 파일·로그·명령 줄에 쓰지 않는다. 실행 기록은 `<E2E 루트>\logs\nightly-dispatch.log`(시각·
  suite·명령·종료 코드·gh 출력 한 줄). `<E2E 루트>` = `PUREIRON_E2E_ROOT`(기본 `C:\PureIron_e2e`).
- 묶음(suite, `harness/suites.py` — 환경변수 `PUREIRON_G2_SUITE`로 nightly.py에 간다): **`g2`**(평일)
  = `-m "g2 and not weekly"` — 주간 표지가 없는 시험 전부(crash는 `reset@switching`·`kill@migrating`
  2건). **`weekly`**(일요일) = `-m g2` — 평일 시험 전부 + crash 16건(표지 `weekly`). 이력 줄·
  summary·status 설명에 `suite`가 남는다(status 설명 `G2 weekly …`). 주간 run은 평일 시간 판정
  (run 90분)에서 이것으로 뺀다 — `suite`가 없는 예전 줄은 `g2`로 읽는다(`report.suite_of`).
- vSwitch: workflow는 저장소 변수 **`PUREIRON_E2E_SWITCH`**를 `PUREIRON_E2E_SWITCH` 환경변수로 넘긴다.
  `setup-host.ps1 -Switch`로 만든 이름과 같아야 하고, 변수가 없으면 기본 `pureiron-int`다. 맞지 않으면
  Hyper-V 점검에서 멈춘다(infra — 아래 실패 분류).
- 브랜치 수동 실행: `gh workflow run nightly.yml --ref <브랜치> -f ref=<브랜치>`(주간 묶음은
  `-f suite=weekly`를 더한다). `--ref`가 고르는 것은
  **workflow 파일**(그 브랜치의 `nightly.yml`·환경변수)이고 `-f ref`가 빌드·시험할 커밋이다 — 둘을 같이
  줘야 브랜치의 수정이 그대로 돈다. master merge 없이 수정·리허설을 돌릴 수 있다.

- 현장: site-a(10.77.0.10) = **BASE**(git 태그가 있는 최신 `deploy/releases/r*.json`), site-b
  (10.77.0.11) = **NEXT**(HEAD 빌드 `n<sha7>`, 버전 `<BASE 다음 번호>-nightly.<sha7>`). NEXT는 WSL
  `bundles.py nightly-release`가 HEAD git 사본에서 `build_images.sh`로 빌드하고 `LABEL
  pureiron.nightly=<sha>` 파생을 얹어 WSL nightly 작업 디렉터리(`~/m2d-e2e/work-nightly/{releases,store}`
  — `run.py`의 `work/`와 나눈다, BASE image store는 같은 실제 store를 가리킨다)에만 기록한다.
  나누기 전에 `nightly-release`를 `work/`에서 돌린 적이 있다면 `~/m2d-e2e/work/store/n*`와 그 이미지
  태그(`pureiron/*:n*`)는 이제 어느 정리도 보지 않는다 — 한 번 손으로 지운다(`rm -rf
  ~/m2d-e2e/work/store/n*`, `docker image ls 'pureiron/*' | grep ' n'`).
  빌드 단계마다 시간 제한이 있다(M6-3b N3, `bundles.py` 상수): `build_images.sh` 40분, 파생
  `docker build` 5분, `docker save` 10분, git 60초. 시간 초과 때 Docker가 답하면 product(HEAD 빌드
  초과는 HEAD가 빌드에서 멈춘 것 — `HeadBuildError`, 그 뒤 단계는 제품 스크립트 실패), 답하지 않으면
  infra다(git 초과는 사본 문제라 infra). 각 한도는 멈춘 한 단계를 잡는 것이고 전체는 Windows 쪽 WSL
  호출 한도(3600초 — 넘기면 infra)다.
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
  Hyper-V 호스트 점검(`sites.check_host` — 권한 없음·vSwitch 없음)은 PC 설정 문제라 `InfraError`다.
- 이력(M6-3b): 브랜치 `e2e-history`의 **월별 파일 `history-YYYY-MM.jsonl`**(run 날짜의 달)에 시험마다
  한 줄(`date`·`run`·`suite`·`product_sha`·`test_id`·`outcome`·`class`·`seconds`·`quarantined`)을
  쓴다 — contents API는 1 MB가 넘는 파일의 내용을 비워 돌려주므로 한 파일에 계속 쌓으면 읽기가 조용히
  빈 이력이 된다. 읽기(2연속·7일 flake)는 예전 `history.jsonl`(리허설·M6-3a 줄 — 옮기지 않는다) +
  지난달 + 이번 달이다. 격리(`quarantine.yaml`) 중이던 실패는 `quarantined: true`로 남고 "2연속"
  이슈 판단에서 빠진다(실행 판정에서 빠지는 것과 같다).
- 알림: product가 2회 연속이면 이슈(라벨 `g2-nightly`), **같은 시험이 2회 연속 infra**(재시도 뒤)여도
  이슈(라벨 `g2-nightly-infra`) — 시간 초과로 드러난 제품 멈춤이 "error"로만 쌓이지 않게. "2연속"은
  두 가지를 함께 본다(어느 하나면 이슈, M6-3b fix-r1 M2): ① 실행 단위 — 시작한 실행 중 가장 최근 두
  개(product는 시험이 달라도 된다, infra는 같은 시험) ② 시험 단위 — 이번에 실패한 시험마다 그 시험을
  돌리는 묶음의 직전 실행(이력에서 그 시험 줄이 나온 `suite`의 실행)에서도 같은 분류로 실패했는가.
  ②가 없으면 일요일에만 도는 crash 16건은 직전 실행(토요일 평일 묶음)에 그 시험이 없어 매주 실패해도
  이슈가 열리지 않는다. 평일 시험은 두 묶음 모두에 있으므로 지금처럼 바로 앞 실행과 잇는다. pytest
  전체가 시간 제한에 죽거나 크래시해 junit이 없으면 시험 id `pytest`(infra) 한 줄로 이력에 남으므로 이
  경우도 이틀째에 이슈가 된다(`summary.json`의 `pytest_rc`, -1 = 시간 초과). 시간 초과였으면 그날은
  다시 돌리지 않는다(멈춘 제품은 재시도로 낫지 않는다). 크래시면 전체를 1회 다시 돌린다.
- 시작하지 못한 실행: preflight에서 멈춘 run은 시험 id `preflight`(infra) 한 줄만 이력에 남는다
  (`report.not_started`). 이력 줄·commit status `error`·exit 2는 그대로 남지만 **이슈는 열지 않는다** —
  PC를 끄거나 앱을 켜 둔 밤(메모리 부족)과 같은 종류다. "최근 두 실행"을 고를 때도 건너뛰므로 product →
  preflight 멈춤 → product는 2연속이고, 가장 최근 실행이 preflight에서 멈췄으면 그 밤은 이슈를 열지도
  댓글을 달지도 않는다. 7일 flake 비율의 분모·분자에서도 빠진다. 시작한 뒤 깨진 실행(시험 id
  `runner`·`pytest`)은 그대로 센다(이틀 연속이면 infra 이슈).
- 시간 예산(묶음마다, `harness/suites.py`): workflow 시간을 넘기면 Actions가 job을 죽여 status·이력이
  남지 않는다. 그래서 준비 10분 + run 예산(preflight 시작부터 첫 실행·복구·재시도 — pytest 시간 제한을
  남은 시간으로 줄이고, 10분도 남지 않으면 재시도를 건너뛴다) + 뒷정리 15분(collect 명령당 60초 ×
  10·publish·cleanup)으로 나눈다. **g2**: workflow 120분 · run 90분 · 첫 pytest 75분 · 재시도 30분.
  **weekly**: workflow 270분 · run 240분 · 첫 pytest 200분 · 재시도 40분. 첫 pytest는 추정 125~171분
  (crash 한 건 5~7분 — c0 복원·번들 빌드·창 대기 150초·phase 진행·수렴 — × 18건 = 90~126분 + 나머지
  평일 몫 35~45분)의 상한에 약 1.17배를 둔다. 넘기면 junit이 없어 그 주 결과가 `pytest` 한 줄로만
  남으므로 재시도보다 첫 pytest에 몫을 몬다. 첫 주간 실측으로 다시 맞춘다. yml `timeout-minutes`는
  가장 긴 묶음 값(270)이고 묶음별 한도는 nightly.py가 건다(합이 묶음의 workflow 시간 미만인지 시험이
  본다).
- preflight: foundation-ci 대기를 먼저 하고, WSL echo가 3번 모두 답하지 않으며 foundation-ci가 돌지
  않는 것이 확인될 때만 WSL을 재시작한다(재시도 전 복구도 같다). Actions 안에서 foundation-ci 상태를
  못 읽으면(`gh run list` 실패) infra로 멈춘다(로컬은 `warnings`에만). 메모리가 기준 미만이고 WSL이
  답하면 WSL 페이지 캐시를 비우고 최대 120초(10초 간격) 다시 재서 판단한다(재시도 전 복구도 같다 —
  WSL은 캐시를 Windows에 늦게 돌려준다). `--judge-only`는 회수하지 않고 `measure`에 vmmemWSL·WSL
  페이지 캐시 크기와 "회수하면 통과할 수 있음" 메모를 남긴다.
- VM 시작 직전 메모리: preflight의 8 GB는 HEAD 빌드·번들 **전** 값이다(그 사이 WSL 페이지 캐시가 다시
  찬다 — M6-2b 리허설에서 두 번째 VM이 `0x800705AA`). 그래서 VM을 시작하기 직전(`g2.make_room`)에 다시
  회수하고, Windows 가용 메모리가 "지금 시작할 VM 메모리 합 + 2 GB"(`VM_START_HEADROOM_GB` — 3 GiB
  2대면 8 GB, 직렬 모드는 VM마다 5 GB) 이상이 될 때까지 preflight와 같이 최대 120초 기다린다. 두 대
  몫이 끝내 모자라도 **한 대 몫이 되면 직렬로 자동 전환**한다(한 대를 그 몫으로 다시 재서 띄우고,
  다음 VM도 다시 잰다). 그것도 모자라면 VM을 시작하지 않고 그 현장 시험이 infra로 실패한다(현장마다
  따로 만든 `InfraError`). `PUREIRON_G2_SERIAL=1`은 처음부터 직렬이다. 부팅 방식은 G2-1 기록 `boot`,
  회수 전·후 값·기다린 시간은 `<site>/vm-memory.json`(확인마다 한 항목, 배열)과 현장 로그에 남는다.
- c0 복원 직전 메모리(M6-3a): 시나리오는 c0 복원으로 시작한다(`g2.restore_c0`). 복원 직전에도 같은
  회수·대기를 하고, 필요한 양은 복원 대상 VM이 **켜져 있으면 2 GB**(`Restore-VMSnapshot`이 그 VM을 먼저
  꺼 메모리를 돌려주고 `Start-VM`이 같은 양을 다시 잡으므로 VM 몫은 이미 쓰는 셈 — 실행 중 여유
  `VM_START_HEADROOM_GB`만), **꺼져 있으면 VM 한 대 몫 + 2 GB**(새로 시작하는 것과 같다)다. 모자라면
  복원하지 않고 infra. 복원 직전에 외부 접속 카운터를 표시하고(VM이 꺼져 있어 못 하면 그 구간을 잃은
  것으로 센다 — 맨 끝 외부 접속 0 시험이 실패한다) 복원 뒤 기준을 c0 값으로 되돌린다. 기록은
  `<site>/restore-memory.json`(복원마다 한 항목)과 현장 로그. `run.py`의 `site.restore_c0`는 그대로다
  (회수만 하고 기다리지 않는다).
- 세션 최저 메모리(M6-3a): G2 pytest 세션 내내 20초마다 Windows 가용 메모리를 재서(PowerShell 한 번
  약 0.4초) 최저값·시각·표본 수를 `windows-memory.json`에 남기고(infra 재시도 세션과 합친다)
  `summary.json`의 `windows_memory`에 싣는다.
- Hyper-V 점검(M6-3a): 권한·vSwitch 점검(`sites.check_host`)은 preflight 판단에 들어간다 — 실패하면
  preflight에서 멈춘다("시작하지 못한 실행", 이슈 없음). pytest 세션 안의 점검은 로컬 실행용으로
  남는다.

### 시나리오 (M6-3a·M6-3b)

시나리오 = `(g2.Session, 현장) → 기록`(`harness/scenarios.py`). 계약: ① 시작할 때 그 현장을 c0로
복원한다 ② 다른 시나리오가 남긴 VM 상태·기록에 기대지 않는다 — infra 재시도는 남은 VM을 지우고 실패한
시험 id만 새 pytest로 돌리므로 시험 하나만 골라도 통과해야 한다 ③ 판정은 `judges`의 순수 함수,
원격·WSL·Hyper-V 호출은 `scenarios.Remote`로 주입한다(단위 시험이 가짜로 바꾼다) ④ 부팅·설치가 실패한
현장이면 그 예외를 다시 던진다(분류는 예외 종류). 판정 여러 개를 내는 시나리오는 module fixture가
한 번 돌리고 시험들이 항목별로 단언한다. 시나리오 기록은 그 현장 `state.json`의 `results`에 남는다.

| 시험(실행 순서) | 현장 | 흐름 | 판정(모두 참) |
|---|---|---|---|
| `test_g2_1_install_and_100_tags_good[site-a·site-b]` | 둘 | 부팅 → 설치 → 시뮬레이터 → ready → 이력 시드(`site.seed_history`) → c0 기반 측정 → viewer → c0 | 설치·digest·overlay2·classic · c0 기반(압축 chunk > 0·`tag_history` 행 > 0·DB epoch == 그 release epoch) · c0 뒤 5분 안에 100태그 GOOD·장비 CONNECTED · 그 시점 외부 접속 0 · site-b는 D9 ⓒ(c0 이름 집합 5종·epoch == 기대 스키마) |
| `test_g2_4_rollback` | site-a | c0 → c0 스냅샷(행 수 4종 users·audit_log·alarm_journal·tag_history, site_rev·base_site_rev, 스키마 집합) → FAULT-h g2(FAULT-h 트리) → 수렴 → NEXT g3(NEXT 트리) → 수렴 | `failed`·보고 `E_HEALTH`·실행 이미지 == BASE·`rolling_back` 구간 ≤ 300초·행 수 4종 ≥ 이전·site_rev/base_site_rev 보존·BASE 트리(content_hash)와 정의 값 복귀·경보 없음·메트릭 `updater_failed_manual` 0·edit-lock 해제. 재시도: `succeeded`·이미지 == NEXT·NEXT 트리·D9 ⓐ(== 기대)·ⓑ(c0 ⊂)·압축 chunk 유지 |
| `test_g2_6_3_health_failure` | site-a | 위와 같은 FAULT-h 시도(module fixture 하나) | 10분 안에 `failed`로 수렴(보고 `E_HEALTH`) · 시도 잔류 없음(`attempt` 없음 — 못 읽으면 거짓) · 직전 정상 이미지(BASE)로 ready · edit-lock 해제 |
| `test_g2_5_tampered_before_install[tampered_bootstrap·tampered_deb]` | site-b | 부팅 직후·실제 설치 전(`preinstall.py`, 새 VM·추가 복원 없음): 실제 설치용으로 올린 설치 번들을 VM 안 전용 디렉터리(`/root/e2e-preinstall`)에 복사해 멤버 하나만 바꾸고(`site/tamper.py`) runbook을 따라간다 → 디렉터리 삭제 → 실제 설치 | ① `bootstrap-verify.sh`만 바꾼 사본: runbook sha256 대조에서 멈춤(읽은 sha256 ≠ runbook)·스크립트 실행 안 함 ② `debs/*.deb` 하나만 바꾼 사본: 정품 스크립트(sha256 = runbook)가 payload 대조에서 exit 1(`payload sha256 mismatch`). 둘 다 docker 미설치·탐침 디렉터리 삭제. 탐침 예외는 그 시험에만 남고(분류 유지) 설치를 막지 않는다. site-a에는 하지 않는다 |
| `test_g2_5_rejected[…]` | site-b | c0 → 거부 재료(WSL `reject-materials`) → c0에서 `E_SITE`(현장 `site-x`)·`E_DIGEST`(payload `images/core.tar*` 한 바이트)·`E_SIG`(root가 인증하지 않은 release key — 다른 root가 발급한 cert, payload는 정상)·`E_SCHEMA`(NEXT 이미지 그대로에 스키마만 NEXT epoch + 2인 가짜 기록)·`E_UPDATER`(`--min-updater` = 설치된 Updater 주 번호 + 1)·`same_gen`(g1 재빌드) → `E_CERT` 재료(g3)를 **먼저** 만든 뒤 `next_g2`(NEXT g2, NEXT 트리 적용) → `E_CERT`(생성 시각 역행)·`E_GEN`(g2 뒤에 만든 g1) | 거부마다: CLI 종료 코드(no-op은 0)·보고의 거부 코드(no-op은 없음)·core StartedAt 불변·실행 이미지 불변·edit-lock 해제·applied gen 불변. `next_g2`: `succeeded`·이미지 == NEXT·applied g2·NEXT 트리. 기대와 다른 코드(검증 순서 때문에 앞 단계에 걸림)는 재료를 고친다 — 판정을 느슨하게 하지 않는다 |
| `test_g2_6_2_migrate_failure` | site-b | c0 → c0 스냅샷(스키마 집합 5종·epoch·행 수) → FAULT-m g2(FAULT-m 트리) → 수렴 | 10분 안 `failed`·보고 `E_MIGRATE`·실행 이미지 == NEXT·DB epoch와 Updater `schema_epoch` 그대로·이름 집합 5종이 전과 정확히 같음·`e2e_fault_marker` 없음·행 수 4종 ≥ 이전·경보 없음·edit-lock 해제 |
| `test_g2_8_draft_recall[patch·draft_gate·absorb·reapply·rollback_keeps_drafts]` | site-b | c0 → admin 로그인 → 운영 값 PATCH **3회**(태그 `line1/temp`·알람 `temp-high`·`temp-low`) → 미흡수 repo = **G2 트리 그대로**(`absorbs`에 site-a 키만) 번들 g2 → `pureiron-site project export` → 호스트 → WSL `absorb`(`--repo` G2 트리)·`gate` → 흡수 트리 g3 → FAULT-h g4(FAULT 트리) | site_rev +3·PATCH 응답 200과 DB 정의 · `E_DRAFT`(거부 판정 6항목) · absorb 결과 `absorbs[site-b] … -> 3`·gate 통과 · 재적용 `succeeded`·`base_site_rev == site_rev == 3`·PATCH 값 셋 유지·content_hash == absorb 출력 · FAULT-h 롤백(`failed`·`E_HEALTH`·이미지 == NEXT) 뒤 site_rev·base_site_rev(3, 3)·PATCH 값·content_hash 그대로(3a 리뷰 L1 — c0 값 0에서는 못 잡는 "0으로 되돌림"). G2 트리를 쓰는 이유: 게이트가 다른 현장 키(site-a: 3)를 잘못 읽으면 3 == 3으로 통과해 `E_DRAFT`가 나오지 않는다 |
| `test_g2_6_crash[<mode>@<phase>]` | site-a | 9 phase(`fetched`~`rolling_back`) × `kill`·`reset`, **매 건 c0 복원** → BASE→NEXT g2(NEXT 트리, `rolling_back`은 BASE→FAULT-h, `fetched`는 CLI 대상) — 창이 150초 뒤에 열리는 번들을 CLI로 먼저 스테이징하고 `site/crash_at.py`가 그 phase에서 얼린 뒤 kill / `Restart-VM -Force`(`crash.py`). 평일 2건 `reset@switching`·`kill@migrating`, 나머지 16건 표지 `weekly` | run.py `crash_once`와 같은 항목(결과·실행 이미지·applied gen·경보 없음·edit-lock·`hit`·재부팅 또는 서비스 재시작·스테이징이 창 전에 끝남) + 행 수 4종 ≥ c0 + 주입 뒤 **10분 안** 수렴(시도 잔류 없음). E10(제품 판정은 모두 참인데 스테이징이 창 뒤에 끝남)은 c0부터 1회 다시, 두 번째도 E10이면 infra. run.py의 crash 경로는 바꾸지 않았다 |
| `test_g2_no_external[site-a·site-b]` | 둘 | 맨 끝(모든 시나리오 뒤) | 부팅 이후 누적 외부 접속 카운터 0(`field_fwd`는 따로)·카운터를 잃은 복원 없음 |

- "10분 안"(G2-6 ②③): 시도 시간(기록 `seconds`)은 **스테이징 직전부터 수렴 확인 끝까지**다.
  `pureiron-site apply`는 동기라 시도 전체(검증·적용·health 대기·롤백)가 스테이징 안에서 끝나고, 수렴
  확인은 probe 한 번(기록 `converge_s`)이다. 시나리오 스테이징의 원격 시간 제한은 900초(한도 600초
  + 300초)라 600~900초에 끝난 시도는 시간 초과(infra)가 아니라 판정 거짓(product)으로 잡힌다. 900초도
  넘으면 시간 초과 → infra다. `run.py`의 스테이징 시간 제한(600초)은 그대로다.
- `rolling_back` 구간: Updater 보고 `phase_durations_s.rolling_back`(진입부터 다음 phase 진입까지).
  Updater는 직전 이미지를 띄우고 ready를 확인한 뒤에야 `failed`로 들어가므로 이 구간이 "롤백 시작부터
  직전 이미지 + ready까지"다. 전체 적용 시간(스테이징부터 수렴까지)으로 대신하지 않는다.
- infra 재시도 묶음(`order.retry_closure`): 재시도는 새 세션(새 VM, c0 새로)이라, 한 현장의 시나리오
  시험이 다시 돌면 그 현장의 `test_g2_no_external[site]`를 함께 돌리고(다시 돈 시나리오의 외부 접속을
  본다), `test_g2_no_external[site]`가 다시 돌면 그 현장의 시나리오 시험 전부를 함께 돌린다(시나리오
  없는 세션의 카운터로 통과하지 않게). 처음 대상 기준으로 한 번만 더하고(연쇄 없음), 현장은 순서 표에서
  읽는다. 합친 결과에서 다시 돈 시험은 재시도 결과로 바뀐다 — 첫 세션에서 통과한 외부 접속 0도
  재시도에서 실패하면 실패로 남는다.
- 실행 순서: `harness/order.py`의 표 하나가 정한다(G2-1 → site-a 롤백 → site-b 거부·② 마이그레이션
  실패·draft → crash(site-a — 주간 16건이 길어 site-b 뒤로) → 외부 접속 0). 표에 없는 `test_g2_*.py`는
  수집 오류다.
- 올리는 현장(M6-3b, 3a 리뷰 L3): 세션은 그 pytest에서 돌 시험(표지·노드 id로 고른 뒤)이 쓰는 현장만
  올린다(`order.sites_needed` — crash 재시도는 site-a만). 올리지 않은 현장의 시험이 돌면 `E2EError`다.
- 재시도 전 첫 세션 기록(3a 리뷰 L2): 재시도 세션이 덮어쓰지 않게 `<결과>/<site>/`를
  `<결과>/first/<site>/`로 옮긴다(summary `retry.first_session`).
- FAULT 2종(nightly 쌍, `release_pair.nightly`): `fault` = `n<sha7>h`(버전 `<NEXT 버전>.fh`, epoch =
  NEXT) — collector만 `sleep`(세션을 맺지 않음 → `/readyz` 503 → `E_HEALTH`), core·web은 NEXT 이미지
  그대로(digest 같음). `fault_migrate` = `n<sha7>m`(`.fm`) — core = `FROM pureiron/core:<NEXT>` + NEXT
  이미지에서 꺼낸 migration 사본 + `NNNN_e2e_fault_marker.sql`(다음 번호, 표지 테이블을 만든 뒤
  `SELECT 1 / 0`, 트랜잭션) + `epochs.yaml`에 NEXT epoch + 1 한 줄(`load_catalog`로 다시 읽어 확인).
  기록 schema는 epoch·app_expected = NEXT + 1, app_max_compatible = NEXT + 2. collector·web은 NEXT
  그대로. NEXT epoch는 상수가 아니다 — HEAD가 migration을 더하면 FAULT-m도 따라 오른다. WSL
  `bundles.py nightly-faults`가 `work-nightly/{releases,store}`에만 만들고, prepare가 파생 규칙(바뀌는
  서비스 하나만 NEXT와 digest가 다름·envelope)을 다시 확인한다. nightly prune은 NEXT와 파생(h·m)을 한
  묶음으로 지운다(store 디렉터리·docker 태그).
- 트리: NEXT·FAULT-h·FAULT-m 번들은 G2 100태그 트리에서 태그 `line1/temp_sp`의 description만 바꾼
  트리(`bundles.release_tree(source=…)`)를 싣는다. 판정 기대값(`trees.tree_expect`)도 같은 디렉터리에서
  읽는다.
- 스키마 자동 판정(D9): prepare가 WSL `bundles.py expected-schema`로 임시 TimescaleDB(그 run의 site-db
  이미지, 신뢰 인증, **호스트 포트 없음** — 같은 PC의 CI가 25432·25433을 쓴다)를 띄우고 NEXT core로 빈
  DB를 끝까지 migrate한 뒤, VM probe와 **같은 SQL**(`site/probe.py` `DB_SQL` — 기대 스키마도 이것을
  import한다)로 이름 집합 5종(tables·columns·indexes·caggs·realtime_caggs)과 epoch를 읽어 결과
  디렉터리 `expected-schema.json`에 남긴다. 컨테이너(DB·이름 붙은 migrate 클라이언트)·네트워크는
  성공·실패 모두 지우고, 시작할 때 표지 label이 붙은 남은 것 중 **만든 프로세스(이름 끝 pid)가 끝난
  것만** 지운다 — 같은 PC에서 동시에 도는 다른 expected-schema를 지우지 않는다(3a 리뷰 L4). 시간 제한: DB 기동 180초, migrate 600초, 전체 1200초. 분류: NEXT가 빈
  DB에 migrate되지 않으면 product(`SchemaBuildError`), Docker 무응답·DB가 끝내 받지 않음은 infra.
  `run.py`·`DEFAULT`의 수동 집합(`judge_c0`·`judge_next_schema`)은 그대로다.
- 결과 파일(현장 디렉터리 밖): `expected-schema.json`, `windows-memory.json`. 현장 디렉터리:
  `state.json`(시나리오 기록 포함), `vm-memory.json`, `restore-memory.json`.

```powershell
$env:PUREIRON_E2E_SWITCH = "pureiron-e2e"   # setup-host.ps1 -Switch 값
uv run python tests/e2e/gates/nightly.py preflight --judge-only   # 판단만(조치 없음)
uv run python tests/e2e/gates/nightly.py all --dry-run            # GitHub에 쓰지 않음(VM·결과 정리는 한다)
uv run pytest -c tests/e2e/gates/pytest.ini tests/e2e/gates -m "g2 and not weekly" --collect-only   # 평일 묶음, 실행 순서대로
uv run pytest -c tests/e2e/gates/pytest.ini tests/e2e/gates -m g2 --collect-only   # 주간 묶음(+ crash 16건)
```

결과: `C:\PureIron_e2e\results\g2-<run>\`(junit.xml·summary.json·preflight.json·publish.json·
`<site>\`의 updater.json·메트릭·journal·compose 로그, 14일 뒤 preflight가 지운다). 번들
`bundles\<run>\`은 run 끝에 지운다. 환경변수: `PUREIRON_G2_MIN_MEM_GB`(8)·`PUREIRON_G2_MIN_DISK_GB`
(40)·`PUREIRON_G2_CI_WAIT_S`(1800)·`PUREIRON_G2_SERIAL=1`(VM 2대를 차례로). G2 시험은
`gates/pytest.ini`로만 수집된다(G1 `unit`에는 수집 자체가 빠진다). 묶음은 `PUREIRON_G2_SUITE`
(`g2`·`weekly`, 기본 `g2`).

`--dry-run`은 GitHub 쓰기(이력·status·이슈·임시 브랜치 삭제)만 출력으로 바꾼다 — `preflight`·`all
--dry-run`도 남은 `pureiron-site-a`·`pureiron-site-b` VM(run.py의 site-a VM과 이름이 같다)과 14일 넘은
결과는 **실제로 지운다**. `all`이 예상 밖 예외로 멈추면 시험 id `runner`(infra)를
`runner-summary.json`에 남기고 exit 2다 — 그 run의 `summary.json`이 이미 있으면(collect·publish·
cleanup에서 멈춤) 덮어쓰지 않고 그것으로 publish를 다시 시도한다(이력에 이미 들어간 run은 다시 쓰지
않는다).

Actions 실행 화면의 종료 코드는 product와 infra를 가르지 못한다 — workflow의 `shell: powershell`
(`powershell -Command`)이 0·1 밖의 종료 코드를 1로 바꾸므로 infra(exit 2)도 `exit code 1`로 보인다.
구분은 commit status `g2/nightly`(`failure` = product, `error` = infra)와 이력(`history-YYYY-MM.jsonl`)의
`class`로 한다.

**리허설 실측(2026-11-11, M6-2b).** 위 "브랜치 수동 실행"으로 Actions에서 돌렸다. 구성: self-hosted
runner `e2e`, VM 3 GiB × 2(정식 구성), 낮 시간·데스크톱 앱이 켜진 상태. preflight 뒤 Windows 가용
8.5 GB.

- green 1회(nightly run `20261111T021650Z`, 커밋 `7599764`): job **7분 9초** = checkout·uv·preflight
  29초 + HEAD 빌드·번들 3분 52초(빌드 캐시 적중) + VM 2대 부팅·설치·G2-1 2분 30초 + 수집·publish·정리
  18초. 설치 58~66초, 100태그 GOOD까지 c0 뒤 0.3~0.5초. 2/2 통과.
- VM 시작 직전 메모리(`<site>/vm-memory.json`, 4회): 빌드 뒤 가용 1.7~1.8 GB → 회수 뒤 8.0~9.3 GB, 기다린
  시간 11~32초(기준 8.0 GB). Windows 최저 여유 1.65 GB(빌드 중)·1.69 GB(VM 시작 뒤).
- 다른 결말의 소요 시간: preflight 멈춤 2분 52초(회수 대기 120초 포함), infra 재시도로 통과 6분 24초,
  product 실패 11분 15초·11분 31초(100태그 GOOD 5분 대기 포함).
- 실제 동작이 확인된 것: `e2e-history` 빈 뿌리 브랜치 생성과 줄 추가, commit status
  `success`·`failure`·`error`, product 2회 연속에서 이슈 1개 생성(라벨 `g2-nightly`는 처음 쓸 때
  자동으로 만들어진다)과 그 뒤 댓글, 7일 넘은 `e2e-tmp/*` 브랜치 삭제, Actions 안의 foundation-ci 상태
  읽기, infra 재시도 1회, product 재시도 0회.
- schedule 경로(빈 입력·늦은 시작 판단)는 M6-3b에서 지웠다(★H13 — 위 "언제"). 시작은 작업
  스케줄러의 workflow_dispatch뿐이다.
- 운영 메모: 데스크톱 앱이 켜진 상태에서는 회수 뒤 가용이 기준(8.0 GB)에 걸친다(8.0~9.3 GB). 앱을 더
  켜 둔 밤은 preflight나 VM 시작 직전 확인에서 infra로 멈출 수 있다.

## 단위 테스트

`tests/e2e/tests/` — 공용화 불변 조건(`test_harness_shared.py` — 항목 39개 이름·순서, `ReleasePair` 기본값 = 옛 상수, 현장 둘의 상태 파일·이름 분리, 롤백 5분 경계, VM 3 GiB)·phase 순서·상태 읽기·nft 규칙(현장 장비 `field_fwd` 규칙 위치·끝점 추출·외부 접속 판정)·seed ISO 왕복·PowerShell 인용·수렴 판정·release 쌍(BASE·NEXT 기록 대조 필수·crash 순서·항목 39개·단계 순서·c0/NEXT 스키마 집합 판정(같은 epoch 실측값, 지난 쌍 c0 교차)·전환기 경보·FAULT 트리 복원·M5 확인 판정(트리·PATCH·absorb·재적용)·gen 배정·NEXT/FAULT 트리 판정(로컬 실측 `fixtures/m4c-tree-db.json` — 같은 트리 재적용 no-op에서 ⓐ 실패, NEXT 트리에서 통과, 롤백·재시도 정의 값)·업그레이드/FAULT/재시도 배선(가짜 `apply`))·온라인 판정(`test_online.py` — 실측 `/fleet` 행으로 단계별 통과·교차 실패, CA 지문 표기, `central.py` 값·토큰을 출력에 내지 않음, 화면 비밀번호·현장 token이 명령 줄에 없고 stdin으로만 감 — `basic_auth_line`·`site_online`·`Vm.run(input=)`)·store symlink 다시 연결·`ensure_image` 적재 조건·스테이징 진단 기록(`stage_for_window`)·G2(`test_harness_g2.py` — 현장별 디렉터리·seed 바이트 동일·
`nft_mark` site id·E10 재시도·VM 시작 직전 메모리 확보(동시·직렬·자동 직렬·현장마다 새 오류), `test_nightly_pair.py`, `test_trees_g2.py`, `test_frame_g2.py`,
M6-3a 시나리오 `test_scenarios_g2.py`(실행 순서 표·실제 수집 순서·c0 복원 직전 메모리·카운터·시작 계약·c0 기반·probe 키)·`test_rollback_g2.py`(G2-4·G2-6 ③ 흐름과 판정, 한 시험만 골라도 module fixture가 도는지)·`test_faults_g2.py`(G2-6 ②)·`test_schema_g2.py`(D9 ⓐⓑⓒ·SQL 한 곳·임시 DB 수명·분류)·`test_nightly_faults.py`(FAULT 2종 이름·버전·epoch·파생 digest·migration 형식·prune·트리)·`test_memory_g2.py`(세션 최저 메모리·preflight Hyper-V 점검), 공용 가짜 `g2_fakes.py`,
M6-3b `test_rejections_g2.py`(G2-5 재료·변조 도구·거부 판정 6항목·흐름·설치 전 탐침)·`test_draft_g2.py`
(G2-8 흐름·PATCH 3회·absorbs[site-b]·재적용·롤백 뒤 기준점)·`test_crash_g2.py`(계획 18건·평일 2건·매 건
c0·E10 1회)·`test_suites_g2.py`(묶음 표지·시간 예산·등록 스크립트 내용·yml에 schedule 없음),
`test_nightly_runner.py`(이력 월별 파일·격리 줄·suite), 실패 분류 `test_failure_split.py`(빌드 시간
초과 분류), 시작(묶음)·호스트 점검 분류·시작하지 못한 실행 `test_nightly_cron.py`). G1 `unit`에 포함된다.

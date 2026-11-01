# e2e — Hyper-V VM 실측 (PD-24)

현장 호스트(Ubuntu 24.04)를 개발 PC의 Hyper-V VM으로 만들고, 실제 설치 번들·현장 번들·Updater를
돌려 plan M2 종료 조건을 확인한다. 단위·통합 테스트(fake 세계, compose)가 다루지 못하는 것 —
폐쇄망 부트스트랩, classic image store, systemd 재시작, 전원 리셋, 실제 마이그레이션·롤백 시간 — 이 대상이다.

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
- 메모리: VM 4 GiB + 체크포인트 복원. WSL VM(번들 빌드·Docker Desktop)이 페이지 캐시를 늦게 돌려줘
  복원이 `0x8007000E`로 실패할 수 있어, 하네스는 VM 시작·복원·리셋 전에 WSL 캐시를 비운다(`wsl -u root`).
- 한국어 등 지역화된 Windows: Hyper-V 통합 서비스 이름이 번역되므로 하네스는 서비스 ID로 고른다.

빌드 호스트(WSL runner)에는 `~/pureiron/keys/test`(test root·release key, `deploy/keys/README.md`),
image store(`~/pureiron/images`, `PUREIRON_IMAGE_STORE`)의 BASE·NEXT tar(`<이름>/{core,collector,web,site-db}.tar`),
`~/m2d-tools/qemu-img`(sudo 없이 `apt-get download qemu-utils liburing2 libaio1t64` → `dpkg -x`)가 있어야
한다. 저장소에는 `deploy/releases/<BASE·NEXT>.json`이 있어야 한다(`prepare`가 WSL 사본으로 동기화해
`version`·`schema.epoch`를 `release_pair.py`와 대조한다). `pureiron/<svc>:<NEXT>` 이미지가 WSL Docker에
없거나 config digest가 NEXT 기록과 다르면 `fault-release`가 store tar에서 `docker load`한다.

## release 쌍

실측할 release 쌍은 `m2d/release_pair.py` 한 곳에 있다 — 설치 g1 = `BASE`, 업그레이드 `BASE`→`NEXT`,
헬스 실패 롤백 `FAULT`(`NEXT` 파생), 기대 epoch `EPOCHS`. 결과 항목 35개(`run.expected_items()`) 이름도
여기서 나온다(S4까지 30개, M5 마감에서 M5 확인 4개 + 전환기 경보 해제 1개를 더했다).

| 실측 | BASE | NEXT | FAULT | c0 | 업그레이드 마이그레이션 |
|---|---|---|---|---|---|
| M2(run 20261005T055523Z, 30/30) | r0 `2026.10.0` (epoch 1) | r1 `2026.10.1` (epoch 2) | `r1f` `2026.10.1-fault1` | epoch 1 + 압축 chunk | 0006·0007(1h CAGG) |
| S3(run 20261009T231040Z, 30/30) | r1 `2026.10.1` (epoch 2) | r2 `2026.10.2` (epoch 3) | `r2f` `2026.10.2-fault1` | epoch 2(1h CAGG 있음) + 압축 chunk | 0008·0009(정의 jsonb, CAGG 실시간 집계, `project_state.historian`) |
| S4(run 20261017T132857Z, 30/30) | r2 `2026.10.2` (epoch 3) | r3 `2026.10.3` (epoch 4) | `r3f` `2026.10.3-fault1` | epoch 3(1m·1h CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, epoch 4 테이블 없음 | 0010~0012(`users`·`sessions`, `audit_log`, `alarms`·`alarm_journal`과 인덱스) |
| **M5 마감(현재)** | r3 `2026.10.3` (epoch 4) | r4 `2026.11.0` (epoch 5) | `r4f` `2026.11.0-fault1` | epoch 4(0010~0012 테이블·인덱스, 1m·1h CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, `project_files` 없음 | 0013(`project_files`, 이름 붙은 인덱스 없음) + 첫 프로젝트 트리 적용 |

항목 이름 중 release가 들어가는 것은 쌍을 따라간다: `install bundle bootstrap + g1(r3) on offline VM`,
`r3→r4 on compressed epoch-4 DB`, `r4 schema after upgrade`, `rollback within 5 min to r3 digests`,
`r4 project tree applied …`, `r4' health failure → rollback (transition, E_DEFINITIONS_LEFT)`,
`r4 tree bundle after transition rollback → succeeded, alarm cleared`(S4에서는 `g1(r2)`·`r2→r3 … epoch-3`·
`r3 schema`·`r3' health failure → rollback`·`to r2`).

스키마 판정은 이름 집합으로 비교한다(`release_pair.py`의 `CAGGS`·`BASE_COLUMNS`·`BASE_TABLES`·`BASE_INDEXES`·
`NEXT_TABLES`, probe `db`의 `caggs`·`realtime_caggs`·`tables`·`indexes`·`columns`). c0는 BASE epoch 산출물이
있고 NEXT 테이블이 없어야 하며, 업그레이드 뒤에는 NEXT 테이블이 생기고 BASE 산출물이 남아야 한다. 0013은
이름 붙은 인덱스를 만들지 않아 `NEXT_INDEXES`는 빈 집합이고 판정 항목에서 뺐다(빈 집합 포함 검사는 늘
참). 쌍을 바꿀 때는 새 NEXT epoch의 migration SQL을 읽고 이 집합들을 함께 바꾼다(지난 NEXT 산출물은
BASE로 옮긴다).

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

r3부터 core API에 사용자 세션 인증이 있다. 하네스(probe)가 읽는 HTTP 경로 `/api/v1/readyz`·`GET /api/v1/project`는
무인증이라 그대로 쓴다(Updater의 편집 잠금은 Updater 토큰).

## 실행

```bash
uv run python tests/e2e/m2d/run.py all             # prepare → vm → install → scenarios → crash → report
uv run python tests/e2e/m2d/run.py crash           # 한 단계만(앞 단계 context: C:\PureIron_e2e\state.json)
uv run python tests/e2e/m2d/run.py all --from scenarios
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
| `install` | runbook 2단계(메타데이터만 전용 디렉터리에 추출 → `bootstrap-verify.sh` sha256·root 지문 대조) → `install.sh --offline-time-confirmed` → enroll → g1 → 서비스 시작. `/readyz` 200, overlay2 + classic store, 실행 이미지 `.Id` == BASE config digest. 압축 chunk 이력 시드, c0 기반(DB epoch == BASE epoch·BASE 산출물·NEXT 테이블 없음·압축 chunk·이력 행) 확인 후 체크포인트 `c0` |
| `scenarios` | `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1, 같은 gen 재빌드는 no-op exit 0(R-7 멱등) — 모두 core `StartedAt`·실행 이미지 불변 / `c0`에서 BASE→NEXT(압축 DB 위 NEXT epoch migrate) 성공 + NEXT 스키마(epoch 5·`project_files`·0010~0012 테이블·인덱스·1m/1h CAGG와 실시간 집계·0008/0009 열·압축 chunk 유지) → 낮은 gen `E_GEN` 거부 → M5 확인 ⓐ~ⓓ / `c0`에서 FAULT → 5분 안에 BASE digest + `failed` + 전환기 경보 `E_DEFINITIONS_LEFT` → 정상 NEXT 트리 번들로 경보 해제 |
| `crash` | phase(`fetched`~`rolling_back`)마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Type Reset -Force` 각 1회 → 10분 안에 수렴(결과·실행 이미지·편집 잠금 해제) |
| `report` | nftables 외부 접속 카운터(`ext_out_v4`·`dns_out`·`ext_out_v6`·`ext_fwd`)의 **누적** 합 0 (`vm` 단계 기준값도 따로 0이어야 한다). 선언 장비 폴링 `field_fwd`는 판정에서 빼고 누적값만 증거로 남긴다. `results.json`의 `missing`은 35개 중 기록되지 않은 항목 |

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

## 단위 테스트

`tests/e2e/tests/` — phase 순서·상태 읽기·nft 규칙(현장 장비 `field_fwd` 규칙 위치·끝점 추출·외부 접속 판정)·seed ISO 왕복·PowerShell 인용·수렴 판정·release 쌍(BASE·NEXT 기록 대조 필수·crash 순서·항목 35개·c0/NEXT 스키마 집합 판정·전환기 경보·M5 확인 판정(트리·PATCH·absorb·재적용)·gen 배정)·store symlink 다시 연결·`ensure_image` 적재 조건·스테이징 진단 기록(`stage_for_window`). G1 `unit`에 포함된다.

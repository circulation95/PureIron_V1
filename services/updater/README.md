# pureiron-updater

현장 호스트의 systemd 서비스이자 운영 CLI `pureiron-site`. Python 3.12 venv tarball(`/opt/pureiron/updater/<ver>`)로 배포한다(ADR-027, PD-5). 서명 번들 검증은 `pureiron-bundle-tools`의 `verify_bundle` 한 벌을 쓴다(P3).

## 상태기계 (plan M2)

상태 파일 `/var/lib/pureiron/state/updater.json`(tmp write → fsync → rename → fsync(dir)). 저장된 `attempt.phase`는 **지금 실행 중인 단계**다.

| phase | 동작 | 실패 시 |
|---|---|---|
| `fetched` | 번들 사본(`bundles/`) 확보 — poll은 Range로 이어 받고 크기·sha256 확인 | 다음 poll에서 재시도 |
| `verified` | `verify_bundle`(추출본) → 창 형식·site-db digest 불변 → `compose/g<gen>/` 작성 → `compose config` 이미지 == manifest 태그 → 없는 이미지만 load + `.Id` 확인 | `rejected`(거부 코드) / `failed`(`E_LOAD`) — generation 미기록, 번들 sha256만 건너뜀 |
| `committed` | 창 안에서만. `last_attempted_gen=gen`, `last_result=pending`, trust 상태 저장 | — |
| `locked` | `POST edit-lock{gen}` → 반환 site_rev + `GET /project` base로 드래프트 게이트 재검사, GOOD 비율 baseline | `rejected(E_DRAFT)` / core 없음 → `verified`로 대기 |
| `backed_up` | `backups/g<gen>/`: `pg_dump -Fc`(tag_history·Timescale 내부 제외), **직전 core 이미지**로 project export(epoch ≥ 5 core면 `--with-state` → `project/{state.json, project.tar}` 현재 유효 트리, 아니면 state JSON만), 직전 compose, secret 이름 | `failed(E_BACKUP)` |
| `migrating` | `up --wait site-db`(DB 준비 대기) → 새 core 이미지로 `migrate up --to <target>` | `rolling_back(E_MIGRATE)` |
| `switching` | 태그 `.Id` 재확인 → `up -d --wait --remove-orphans` → 실행 컨테이너 `.Image` 확인 → **새 core 이미지**로 `project apply --tar`(번들 `project/project.tar`의 컨테이너 uid 소유 0400 사본을 읽기 전용 mount, 정의·project_files·state 한 트랜잭션, 같은 트리 재적용 no-op). target이 epoch 4 이하 core면 메타만 기록하는 이전 `project apply` | `rolling_back(E_SWITCH)` |
| `checking` | `/readyz` ready + collector 세션(+ baseline > 0이면 GOOD 비율 ≥ baseline − 2%p), 제한 90 s | `rolling_back(E_HEALTH)` |
| `rolling_back` | 직전 compose `up -d` + `.Id` 확인 + **백업을 만든 직전 core 이미지**로 project restore(트리 백업이면 `--from <dir>` = 트리 재적용 + state, 아니면 `project.json` state만; site_rev·base 포함, 백업의 컨테이너 uid 소유 0400 사본을 mount — 서비스 `UMask=0027` 백업은 컨테이너가 못 읽음) + ready | 2회 실패 → `failed_manual`(경보). 성공했어도 트리 이전 백업 + 트리 release target이면 `failed` + `E_DEFINITIONS_LEFT` 경보(아래 runbook) |
| 종료 | 편집 잠금 해제(HTTP 실패 시 site-db `psql`로 updater 잠금만 해제 — core 이미지 무관) → 보고 → 확정 | 해제 실패 → 종료 phase로 남아 다음 tick에 재시도 |

- docker 데몬이 응답하지 않으면(재부팅 직후 등) `advance`·`recover`는 아무것도 바꾸지 않고 다음 tick을 기다린다 — 일시 장애를 영구 실패로 분류하지 않는다.
- 대기 중 시도(generation을 아는 커밋 전·후 모두)는 새 번들을 먼저 부작용 없이 검증해 **더 높은 generation일 때만** 대체한다 — poll과 CLI(`apply`)가 같은 규칙이다. 커밋된(pending) 시도는 `failed(E_SUPERSEDED)`로 보고하고 잠금을 푼 뒤 대체한다(못 풀면 거부). 거부될 번들·더 낮은 gen·같은 gen 재빌드는 대기 시도를 건드리지 않고 새 번들만 `rejected`(같은·낮은 gen은 `E_GEN`)로 보고한다. 커밋 후 번들 사본이 사라지면 `failed(E_BUNDLE)`.
- 서비스 loop는 매 tick 복구 규칙을 적용한다 — `apply`가 서비스 영향 전 단계에서 죽고 창이 닫히면 `verified`로 되돌린다.
- 최초 설치(적용 이력 없음)는 창·잠금·백업이 없고, 실패해도 되돌릴 대상이 없어 띄운 스택을 진단용으로 남긴다. 최초 설치가 실패한 뒤의 재시도(새 generation)도 적용 이력이 없으므로 다시 최초 설치로 진행한다(보호할 직전 상태가 없다).
- 성공한 적용 뒤 GC(`gc.py`): 방금 적용한 gen과 직전 gen(`from`)의 `state/compose/g*`·`backups/g*`만 남기고, 그 두 정의가 참조하지 않는 `pureiron/*` 이미지 태그를 지운다. 정리 실패(예외 포함)는 확정된 결과·종료 코드에 영향이 없다(로그만, 다음 성공 때 다시). 같은 태그를 다른 이미지로 다시 쓴 경우(release 재빌드) 이전 이미지는 태그 없는(dangling) 상태로 남는다 — release 태그는 재사용하지 않는 것이 전제다.
- poll이 받은 번들 사본 이름은 기대 sha256 기준(`poll-<sha256 16자>.pib`) — 서명 전에는 믿을 수 없는 generation으로 이름을 지으면 대기 시도의 사본과 겹칠 수 있다.
- 프로젝트 트리(M5-0): 적용은 새 이미지, 백업·복원은 직전 이미지가 한다 — 백업을 쓴 이미지가 읽고, 롤백이 실패한 새 이미지에 기대지 않는다. 직전 이미지가 트리를 모르면(epoch 4 이하) 롤백은 state만 되돌린다(첫 트리 release로의 업데이트 실패 시 정의는 남음) — 이 경우 결과는 `failed`이고 보고 `logs`에 `E_DEFINITIONS_LEFT`, `status`에 `ALARM`, `updater_failed_manual 1`이 선다. 근거·한계는 `backup.py` docstring.
- 백업 `project/`는 컨테이너 uid가 쓴 디렉터리라 신뢰하지 않는다: `state.json`(+ `project.tar`) 외 항목, 심볼릭·하드 링크, 일반 파일이 아닌 항목이 있으면 `E_BACKUP`, 롤백 때도 링크를 따르지 않고(`O_NOFOLLOW`) 읽는다.
- 스키마는 전진만 한다. 롤백은 앱 이미지·프로젝트만 직전으로 되돌린다. site-db digest·태그가 바뀌는 번들은 `E_DIGEST`로 거부한다(db-step release는 아직 미지원).
- `recover()` 규칙은 `recover.py` docstring 표. 서비스 영향 전 단계는 창 밖이면 `verified`로, `migrating` 이후는 창과 무관하게 끝까지 간다.
- compose 정의는 번들 payload `compose/site.compose.yml`(서명 대상)이고, `.env`는 manifest 태그에서 만든다. `up`은 `--wait --wait-timeout 180`.
- poll 토큰은 control-api와 같은 출처에만 보낸다(다른 호스트 번들 URL·redirect 제외).

## 결과 기록

- `state/reports/<시작시각>-g<gen>-<번들 sha256 12자>.json` — control-api ApplyAttempt 필드. `report export`가 `report_seq`를 매기고 site key로 서명해 묶은 뒤 `reports/exported/`로 옮긴다(온라인 전송은 M4).
- `state/metrics/updater.prom` — `updater_last_result`, `updater_applied_generation`, `updater_failed_manual`(alarm 유무: `failed_manual` 또는 `E_DEFINITIONS_LEFT`), `updater_phase_duration_seconds{phase}`.

### Runbook: `E_DEFINITIONS_LEFT`(전환기 롤백)

트리를 모르는 직전 release(r3 이하)에서 첫 트리 release(r4)로의 업데이트가 실패하면, 롤백은 직전 앱과 project_state만 되돌리고 실패한 generation의 장비·태그·알람 정의는 site-db에 남는다(직전 앱이 그 정의로 수집). 수동 복구: 직전(적용 중) 프로젝트 디렉터리를 호스트에 두고 직전 core 이미지로 `pureiron-core apply-definitions <dir>`을 실행한다(r3에 있는 명령, path 기준 upsert·retire) → 태그 품질이 돌아왔는지 확인. 경보는 **트리 release(r4 이상) 적용이 성공해야만** 지워진다 — 정의를 실제로 교체한 성공이기 때문이다. 트리 이전 release(r3) 번들의 성공은 메타만 기록하므로 경보를 지우지 않는다(수동 복구 뒤에도 경보는 남는다). 그 사이 `failed_manual`이 나도 이 경보는 함께 남는다(`g3: E_HEALTH; g2: E_HEALTH E_DEFINITIONS_LEFT`). 원인(번들 정의 오류)을 고친 새 트리 release generation을 다시 내는 것이 정상 경로다.

## CLI

```bash
pureiron-site apply <pib>          # 0 성공·대기·no-op, 1 거부·실패, 2 다른 Updater 실행 중, 3 설정 오류
pureiron-site bundle verify <pib> [--kind install]   # 현재 상태 기준 검증만(설치 번들은 부트스트랩 5a)
pureiron-site status [--json]
pureiron-site recover
pureiron-site run                  # 서비스 loop(systemd): recover → poll → advance, 60 s
pureiron-site enroll --site <id> [--re-enroll] [--next-seq N] [--out FILE]
pureiron-site secret set <name> [--generate] [--if-missing]   # 값은 stdin 한 줄
pureiron-site report export --out DIR                          # 폐쇄망 보고 묶음
pureiron-site project export --out draft.tar                   # 서명된 현장 드래프트(M5-2)
```

설정 = 프로세스 환경 + `/etc/pureiron/updater.env`(`PUREIRON_ENV_FILE`, 프로세스 환경 우선) — systemd unit과 운영자 CLI가 같은 값을 본다. 변수 표는 `settings.py` docstring. `PUREIRON_SITE_ID`는 `enroll`이 env 파일에 기록하며, `enroll`·`secret set`·`report export`·`project export`·`bundle verify --kind install`은 `PUREIRON_SITE_ID` 없이 동작한다(`report export`·`project export`는 등록 뒤에만 — 현장 id는 `state/site.json`).

### 등록·secret·보고 (PD-3·PD-4, `enroll.py`·`secret_store.py`)

- `enroll`: `state/site.key`(Ed25519 seed, 0600) + `state/site.json`(`site_id`·지문·`next_report_seq`) + `state/enrollment.json`(`site-enrollment` 서명 봉투, 자기 키 서명). 같은 현장에서 다시 부르면 키는 두고 enrollment만 다시 쓴다. 다른 현장 id·키/상태 불일치는 거부(`--re-enroll` 필요).
- `enroll --re-enroll`: 새 키쌍, `report_seq`는 `site.json`이 남아 있으면 이어서. `site.json`까지 잃었으면 `seq_pending`이 되어 report export를 거부하고, 벤더가 rekey 등록 후 돌려준 `next_seq`를 `enroll --site <id> --next-seq N`(키는 그대로, 카운터만 — 단조 증가)으로 반영한 뒤에 풀린다. 키·`site.json`을 잃었는데 적용·보고 이력이 남은 호스트에서 플래그 없는 신규 enroll은 거부한다(seq 1부터 다시 세면 control-api가 새 보고를 이력으로만 저장한다). 상태 디렉터리를 통째로 잃으면 신규와 구별할 수 없다 — 벤더가 `POST /sites` 중복을 거부하면 `--re-enroll`로 다시 하고 rekey 응답을 `--next-seq`로 반영한다.
- `secret set`: `state/secrets/<name>`(0600, root면 소유자 `PUREIRON_SECRET_UID`=10001 — compose 파일 secret은 bind mount라 컨테이너 서비스 사용자가 읽어야 한다). `install.sh`가 `db_password`·`updater_token`을 `--generate --if-missing`으로 만든다. 온라인 현장은 `control_api_token`을 등록 때 받은 값으로 넣는다.
- `report export`: 저널(`reports/.export.journal`: 첫 seq + 묶을 이름)을 쓰고 카운터를 예약한 뒤 `site-reports` 봉투(`pureiron-reports-<site>-<첫 seq>-<끝 seq>.json`)를 쓰고 원본을 옮긴다. 도중에 죽으면 다음 실행이 저널대로 같은 seq·같은 보고로 끝내고, 그 뒤 쌓인 보고도 같은 호출에서 다음 묶음으로 낸다(seq 재사용 없음). 저널이나 그것이 가리키는 보고가 깨져 export가 멈추면 저널을 지우고 다시 실행한다 — 예약했던 seq 구간은 비게 된다(gap은 수신 측에서 허용, 재사용보다 안전). 봉투 형식·검증은 `pureiron_bundle.sitesign` — 자기 키 검증은 enrollment에서만, 보고는 등록된 키로만 검증한다.
- env 파일: `enroll`은 서비스가 실제로 읽는 env 파일에 `PUREIRON_SITE_ID`가 없으면 항상 기록한다(프로세스 환경에 값이 있어도).

### 현장 드래프트 (`draft.py`, M5-2)

`project export --out draft.tar`: 현장 admin이 REST `PATCH`(알람 setpoint·deadband·on_delay_s·priority, 태그 description·eu·record)로 바꾼 운영 값을 벤더에게 넘기는 파일(USB). 온라인 업로드는 M4.

| 단계 | 동작 | 실패 |
|---|---|---|
| 등록 확인 | `state/site.json` + `site.key` 지문 일치 | 등록 전·불일치 exit 3(core 호출 없음) |
| 적용본 확인 | `updater.json` `applied_gen` | 없음 exit 1 |
| core export | `state/compose/g<applied>/`의 core 이미지로 `project export --draft`(쓰기 mount `state/draft-export/`, 컨테이너 uid 0700) | 이전 실행이 남긴 `state/draft-export`를 지우거나 만들 수 없음(경로 표시)·core 실패(트리 apply 전 등)·출력이 `project.tar`·`draft-meta.json` 두 일반 파일이 아님 exit 1 |
| 서명·묶음 | payload `{site_id, site_rev, base_site_rev, content_hash, exported_at, project_tar_sha256}` → `site-draft` 봉투 `draft.json` + `project.tar` 결정적 tar | — |

- `site_rev == base_site_rev`(흡수할 드래프트 없음)여도 파일은 쓰고 `note: no site drafts …`를 출력한다(exit 0).
- 트리 apply 전 현장(`project_files`가 빈 상태 — 개발 스택·수동 복구 경로)에서 한 PATCH는 드래프트로 회수할 수 없다(export exit 1). 그 드래프트를 버리려면 `absorbs[site] = site_rev`를 단 번들을 보낸다.
- 벤더는 등록된 site 공개키와 현장 id로 `pureiron_bundle.sitesign.open_draft`(서명·payload `site_id`·`project_tar_sha256`)를 확인한 뒤 고객 repo에 회수한다(`absorb`, M5-3). 회수 전 이 현장에 absorbs 없는 번들을 보내면 `E_DRAFT`로 거부된다. 형식은 [data-contracts](../../docs/wiki/sot/data-contracts.md) M5-2 절.
- 다른 Updater 실행과 직렬화한다(`process_lock`).

## 테스트

docker·compose·core는 `tests/updater_fakes.py`의 fake 세계로 대신한다. `test_recover.py`는 시나리오(최초 설치·업데이트·migrate/health/rollback 실패, 트리를 아는 r4→r5의 최초 설치·업데이트·health/migrate 실패)마다 모든 바깥 호출·상태 저장 지점에 crash를 주입하고, 새 프로세스의 `recover()`가 crash 없는 실행과 같은 결과로 수렴하는지(복구 중 재crash, 창 닫힌 뒤 복구 포함) 검사한다. 같은 지점들에서 "호스트 재부팅"(데몬 정지 → DB·core 복구 중 → 복구)도 주입한다. 실제 docker·VM 검증은 M2d.

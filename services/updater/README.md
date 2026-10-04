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
| `backed_up` | `backups/g<gen>/`: `pg_dump -Fc`(tag_history·Timescale 내부 제외), project export, 직전 compose, secret 이름 | `failed(E_BACKUP)` |
| `migrating` | `up --wait site-db`(DB 준비 대기) → 새 core 이미지로 `migrate up --to <target>` | `rolling_back(E_MIGRATE)` |
| `switching` | 태그 `.Id` 재확인 → `up -d --wait --remove-orphans` → 실행 컨테이너 `.Image` 확인 → `project apply` | `rolling_back(E_SWITCH)` |
| `checking` | `/readyz` ready + collector 세션(+ baseline > 0이면 GOOD 비율 ≥ baseline − 2%p), 제한 90 s | `rolling_back(E_HEALTH)` |
| `rolling_back` | 직전 compose `up -d` + `.Id` 확인 + project restore(site_rev·base 포함) + ready | 2회 실패 → `failed_manual`(경보) |
| 종료 | 편집 잠금 해제(HTTP 실패 시 site-db `psql`로 updater 잠금만 해제 — core 이미지 무관) → 보고 → 확정 | 해제 실패 → 종료 phase로 남아 다음 tick에 재시도 |

- docker 데몬이 응답하지 않으면(재부팅 직후 등) `advance`·`recover`는 아무것도 바꾸지 않고 다음 tick을 기다린다 — 일시 장애를 영구 실패로 분류하지 않는다.
- 대기 중 시도(generation을 아는 커밋 전·후 모두)는 새 번들을 먼저 부작용 없이 검증해 **더 높은 generation일 때만** 대체한다 — poll과 CLI(`apply`)가 같은 규칙이다. 커밋된(pending) 시도는 `failed(E_SUPERSEDED)`로 보고하고 잠금을 푼 뒤 대체한다(못 풀면 거부). 거부될 번들·더 낮은 gen·같은 gen 재빌드는 대기 시도를 건드리지 않고 새 번들만 `rejected`(같은·낮은 gen은 `E_GEN`)로 보고한다. 커밋 후 번들 사본이 사라지면 `failed(E_BUNDLE)`.
- 서비스 loop는 매 tick 복구 규칙을 적용한다 — `apply`가 서비스 영향 전 단계에서 죽고 창이 닫히면 `verified`로 되돌린다.
- 최초 설치(적용 이력 없음)는 창·잠금·백업이 없고, 실패해도 되돌릴 대상이 없어 띄운 스택을 진단용으로 남긴다. 최초 설치가 실패한 뒤의 재시도(새 generation)도 적용 이력이 없으므로 다시 최초 설치로 진행한다(보호할 직전 상태가 없다).
- 성공한 적용 뒤 GC(`gc.py`): 방금 적용한 gen과 직전 gen(`from`)의 `state/compose/g*`·`backups/g*`만 남기고, 그 두 정의가 참조하지 않는 `pureiron/*` 이미지 태그를 지운다. 정리 실패는 결과에 영향이 없다(다음 성공 때 다시).
- 스키마는 전진만 한다. 롤백은 앱 이미지·프로젝트만 직전으로 되돌린다. site-db digest·태그가 바뀌는 번들은 `E_DIGEST`로 거부한다(db-step release는 아직 미지원).
- `recover()` 규칙은 `recover.py` docstring 표. 서비스 영향 전 단계는 창 밖이면 `verified`로, `migrating` 이후는 창과 무관하게 끝까지 간다.
- compose 정의는 번들 payload `compose/site.compose.yml`(서명 대상)이고, `.env`는 manifest 태그에서 만든다. `up`은 `--wait --wait-timeout 180`.
- poll 토큰은 control-api와 같은 출처에만 보낸다(다른 호스트 번들 URL·redirect 제외).

## 결과 기록

- `state/reports/<시작시각>-g<gen>-<번들 sha256 12자>.json` — control-api ApplyAttempt 필드. `report export`가 `report_seq`를 매기고 site key로 서명해 묶은 뒤 `reports/exported/`로 옮긴다(온라인 전송은 M4).
- `state/metrics/updater.prom` — `updater_last_result`, `updater_applied_generation`, `updater_failed_manual`, `updater_phase_duration_seconds{phase}`.

## CLI

```bash
pureiron-site apply <pib>          # 0 성공·대기·no-op, 1 거부·실패, 2 다른 Updater 실행 중, 3 설정 오류
pureiron-site bundle verify <pib> [--kind install]   # 현재 상태 기준 검증만(설치 번들은 부트스트랩 5a)
pureiron-site status [--json]
pureiron-site recover
pureiron-site run                  # 서비스 loop(systemd): recover → poll → advance, 60 s
pureiron-site enroll --site <id> [--re-enroll [--next-seq N]] [--out FILE]
pureiron-site secret set <name> [--generate] [--if-missing]   # 값은 stdin 한 줄
pureiron-site report export --out DIR                          # 폐쇄망 보고 묶음
```

설정 = 프로세스 환경 + `/etc/pureiron/updater.env`(`PUREIRON_ENV_FILE`, 프로세스 환경 우선) — systemd unit과 운영자 CLI가 같은 값을 본다. 변수 표는 `settings.py` docstring. `PUREIRON_SITE_ID`는 `enroll`이 env 파일에 기록하며, `enroll`·`secret set`·`report export`·`bundle verify --kind install`은 그 전에도 동작한다.

### 등록·secret·보고 (PD-3·PD-4, `enroll.py`·`secret_store.py`)

- `enroll`: `state/site.key`(Ed25519 seed, 0600) + `state/site.json`(`site_id`·지문·`next_report_seq`) + `state/enrollment.json`(`site-enrollment` 서명 봉투, 자기 키 서명). 같은 현장에서 다시 부르면 키는 두고 enrollment만 다시 쓴다. 다른 현장 id·키/상태 불일치는 거부(`--re-enroll` 필요).
- `enroll --re-enroll`: 새 키쌍, `report_seq`는 이어서. `site.json`까지 잃었으면 벤더 rekey 응답의 `--next-seq`가 필수(1부터 다시 세면 control-api가 새 보고를 이력으로만 저장한다).
- `secret set`: `state/secrets/<name>`(0600, root면 소유자 `PUREIRON_SECRET_UID`=10001 — compose 파일 secret은 bind mount라 컨테이너 서비스 사용자가 읽어야 한다). `install.sh`가 `db_password`·`updater_token`을 `--generate --if-missing`으로 만든다. 온라인 현장은 `control_api_token`을 등록 때 받은 값으로 넣는다.
- `report export`: 대기 보고에 `report_seq`를 매겨 `site-reports` 봉투(`pureiron-reports-<site>-<첫 seq>-<끝 seq>.json`)로 쓰고 카운터 저장 → 원본 이동. 봉투 형식·검증은 `pureiron_bundle.sitesign`(control-api가 같은 코드로 검증).

## 테스트

docker·compose·core는 `tests/updater_fakes.py`의 fake 세계로 대신한다. `test_recover.py`는 시나리오(최초 설치·업데이트·migrate/health/rollback 실패)마다 모든 바깥 호출·상태 저장 지점에 crash를 주입하고, 새 프로세스의 `recover()`가 crash 없는 실행과 같은 결과로 수렴하는지(복구 중 재crash, 창 닫힌 뒤 복구 포함) 검사한다. 같은 지점들에서 "호스트 재부팅"(데몬 정지 → DB·core 복구 중 → 복구)도 주입한다. 실제 docker·VM 검증은 M2d.

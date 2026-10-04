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
| `migrating` | 새 core 이미지로 `migrate up --to <target>`(최초 설치는 site-db 먼저 기동) | `rolling_back(E_MIGRATE)` |
| `switching` | 태그 `.Id` 재확인 → `up -d --wait --remove-orphans` → 실행 컨테이너 `.Image` 확인 → `project apply` | `rolling_back(E_SWITCH)` |
| `checking` | `/readyz` ready + collector 세션(+ baseline > 0이면 GOOD 비율 ≥ baseline − 2%p), 제한 90 s | `rolling_back(E_HEALTH)` |
| `rolling_back` | 직전 compose `up -d` + `.Id` 확인 + project restore(site_rev·base 포함) + ready | 2회 실패 → `failed_manual`(경보) |
| 종료 | 편집 잠금 해제(HTTP 실패 시 `pureiron-core project unlock`) → 보고 → 확정 | 해제 실패 → 종료 phase로 남아 다음 tick에 재시도 |

- 최초 설치(적용 이력 없음)는 창·잠금·백업이 없고, 실패해도 되돌릴 대상이 없어 띄운 스택을 진단용으로 남긴다.
- 스키마는 전진만 한다. 롤백은 앱 이미지·프로젝트만 직전으로 되돌린다. site-db digest가 바뀌는 번들은 `E_DIGEST`로 거부한다(db-step release는 아직 미지원).
- `recover()` 규칙은 `recover.py` docstring 표. 서비스 영향 전 단계는 창 밖이면 `verified`로, `migrating` 이후는 창과 무관하게 끝까지 간다.
- compose 정의는 번들 payload `compose/site.compose.yml`(서명 대상)이고, `.env`는 manifest 태그에서 만든다.

## 결과 기록

- `state/reports/<시작시각>-g<gen>.json` — control-api ApplyAttempt 필드(서명·`report_seq`·전송은 M2c/M4).
- `state/metrics/updater.prom` — `updater_last_result`, `updater_applied_generation`, `updater_failed_manual`, `updater_phase_duration_seconds{phase}`.

## CLI

```bash
pureiron-site apply <pib>          # 0 성공·대기·no-op, 1 거부·실패, 2 다른 Updater 실행 중, 3 설정 오류
pureiron-site bundle verify <pib>  # 현재 상태 기준 검증만
pureiron-site status [--json]
pureiron-site recover
pureiron-site run                  # 서비스 loop(systemd): recover → poll → advance, 60 s
```

환경변수 표는 `settings.py` docstring(`PUREIRON_SITE_ID` 필수). enroll·secret·report export는 M2c 이후.

## 테스트

docker·compose·core는 `tests/updater_fakes.py`의 fake 세계로 대신한다. `test_recover.py`는 시나리오(최초 설치·업데이트·migrate/health/rollback 실패)마다 모든 바깥 호출·상태 저장 지점에 crash를 주입하고, 새 프로세스의 `recover()`가 crash 없는 실행과 같은 결과로 수렴하는지(복구 중 재crash, 창 닫힌 뒤 복구 포함) 검사한다. 실제 docker·VM 검증은 M2d.

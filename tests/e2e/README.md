# e2e — Hyper-V VM 실측 (PD-24)

현장 호스트(Ubuntu 24.04)를 개발 PC의 Hyper-V VM으로 만들고, 실제 설치 번들·현장 번들·Updater를
돌려 plan M2 종료 조건을 확인한다. 단위·통합 테스트(fake 세계, compose)가 다루지 못하는 것 —
폐쇄망 부트스트랩, classic image store, systemd 재시작, 전원 리셋, 실제 마이그레이션·롤백 시간 — 이 대상이다.

## 1회 준비 (사용자, 관리자 PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File tests\e2e\infra\setup-host.ps1
```

- 현재 사용자를 **Hyper-V Administrators**에 넣는다 → **로그아웃 후 다시 로그인**해야 적용된다.
- 내부 전용 vSwitch `pureiron-int`(외부 NIC 없음)와 호스트 쪽 `10.77.0.1/24`를 만든다. 호스트는 이 망을
  다른 곳으로 전달하지 않는다 — VM은 인터넷에 닿지 않는다.
- C: 여유: golden 2.1 GB + VM 차분 디스크 수 GB + 번들(회당 약 0.74 GB × 실행 수). 작업 디렉터리는
  `C:\PureIron_e2e`(`PUREIRON_E2E_ROOT`).

빌드 호스트(WSL runner)에는 `~/pureiron/keys/test`(test root·release key, `deploy/keys/README.md`),
image store의 `r0`·`r1` tar, `~/m2d-tools/qemu-img`(sudo 없이 `apt-get download qemu-utils liburing2
libaio1t64` → `dpkg -x`)가 있어야 한다.

## 실행

```bash
uv run python tests/e2e/m2d/run.py all             # prepare → vm → install → scenarios → crash → report
uv run python tests/e2e/m2d/run.py crash           # 한 단계만(앞 단계 context: C:\PureIron_e2e\state.json)
uv run python tests/e2e/m2d/run.py all --from scenarios
uv run python tests/e2e/m2d/run.py destroy         # VM·차분 디스크 삭제(golden 유지)
```

결과는 `artifacts/e2e/m2d-<시각>/results.json`(항목별 PASS/FAIL·측정값)과 `bundle-metrics.json`
(번들별 크기·이미지 크기·빌드/검증/로드 시간·phase 시간).

## 단계별 확인

| 단계 | 확인 |
|---|---|
| `prepare` | WSL 사본에서 r1′(`r1f`: collector가 세션을 맺지 않는 파생 이미지 → `/readyz` 503), 설치 번들 **실빌드**(`fetch_debs.sh`·`build_updater.sh`), g1(r0) |
| `vm` | golden(클라우드 이미지 → 64 GiB 동적 VHDX, 읽기 전용) 위 차분 디스크, seed ISO(cloud-init: 사용자·고정 IP·OS phone-home 끄기·nftables 감시 테이블) |
| `install` | runbook 2단계(메타데이터만 전용 디렉터리에 추출 → `bootstrap-verify.sh` sha256·root 지문 대조) → `install.sh --offline-time-confirmed` → enroll → g1 → 서비스 시작. `/readyz` 200, overlay2 + classic store, 실행 이미지 `.Id` == r0 config digest. 압축 chunk 이력 시드 후 체크포인트 `c0` |
| `scenarios` | 거부 3종(다른 현장·같은 gen 재빌드·변조) exit 1 + core `StartedAt` 불변 / `c0`에서 r0→r1(압축 DB 위 epoch 2) 성공 / `c0`에서 r1′ → 5분 안에 r0 digest + `failed` |
| `crash` | phase(`fetched`~`rolling_back`)마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Force` 각 1회 → 10분 안에 수렴(결과·실행 이미지·편집 잠금 해제) |
| `report` | nftables 외부 접속 카운터(`ext_out_v4`·`dns_out`·`ext_out_v6`·`ext_fwd`) 0 |

### crash 주입 방식

폐쇄망 현장에서 실제로 일어나는 흐름 그대로다. 운영자가 창(window)이 조금 뒤(150 s)에 열리는 번들을
`pureiron-site apply`로 스테이징하면 시도는 `verified`로 대기하고, 창이 열리면 **서비스**가 이어서 적용한다.
VM 쪽 `site/crash_at.py`가 상태 파일(원자적 rename)을 inotify로 보다가 목표 phase에 들어간 순간 실행 중인
프로세스를 `SIGSTOP`으로 얼려 그 phase에 붙잡아 두고, `kill`이면 `systemctl kill -s KILL`, `reset`이면
호스트가 `Restart-VM -Force`를 건다. `fetched`(번들 검증)는 CLI가 실행하므로 CLI를 대상으로 한다.
`migrating`은 실제 마이그레이션이 일어나도록 `c0`(epoch 1)에서, `rolling_back`은 r1′로 일으킨다.

서비스 tick 주기는 e2e에서 `PUREIRON_POLL_INTERVAL_S=10`(기본 60)으로 줄인다.

## 단위 테스트

`tests/e2e/tests/` — phase 순서·상태 읽기·nft 규칙·seed ISO 왕복·PowerShell 인용·수렴 판정. G1 `unit`에 포함된다.

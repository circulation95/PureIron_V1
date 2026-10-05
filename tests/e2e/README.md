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
image store의 `r0`·`r1` tar, `~/m2d-tools/qemu-img`(sudo 없이 `apt-get download qemu-utils liburing2
libaio1t64` → `dpkg -x`)가 있어야 한다.

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
| `prepare` | WSL 사본에서 r1′(`r1f`: collector가 세션을 맺지 않는 파생 이미지 → `/readyz` 503), 설치 번들 **실빌드**(`fetch_debs.sh`·`build_updater.sh`), g1(r0) |
| `vm` | golden(클라우드 이미지 → 64 GiB 동적 VHDX, 읽기 전용) 위 차분 디스크, seed ISO(cloud-init: 사용자·고정 IP·OS phone-home 끄기·nftables 감시 테이블) |
| `install` | runbook 2단계(메타데이터만 전용 디렉터리에 추출 → `bootstrap-verify.sh` sha256·root 지문 대조) → `install.sh --offline-time-confirmed` → enroll → g1 → 서비스 시작. `/readyz` 200, overlay2 + classic store, 실행 이미지 `.Id` == r0 config digest. 압축 chunk 이력 시드 후 체크포인트 `c0` |
| `scenarios` | `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1, 같은 gen 재빌드는 no-op exit 0(R-7 멱등) — 모두 core `StartedAt`·실행 이미지 불변 / `c0`에서 r0→r1(압축 DB 위 epoch 2) 성공 → 낮은 gen `E_GEN` 거부 / `c0`에서 r1′ → 5분 안에 r0 digest + `failed` |
| `crash` | phase(`fetched`~`rolling_back`)마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Type Reset -Force` 각 1회 → 10분 안에 수렴(결과·실행 이미지·편집 잠금 해제) |
| `report` | nftables 외부 접속 카운터(`ext_out_v4`·`dns_out`·`ext_out_v6`·`ext_fwd`)의 **누적** 합 0 (`vm` 단계 기준값도 따로 0이어야 한다) |

거부 보고는 번들 sha256 12자로 찾는다(`reports/<started>-g<gen>-<sha12>.json`) — 커밋 전 거부는 서명을
믿기 전이라 gen이 `unknown`이다. 낮은 gen 번들은 적용된 gen보다 **뒤에** 빌드한다 — 먼저 만든 번들은
cert 단계의 `max_seen_created_at` 역행(`E_CERT`)에 먼저 걸린다.

### crash 주입 방식

폐쇄망 현장에서 실제로 일어나는 흐름 그대로다. 운영자가 창(window)이 조금 뒤(150 s)에 열리는 번들을
`pureiron-site apply`로 스테이징하면 시도는 `verified`로 대기하고, 창이 열리면 **서비스**가 이어서 적용한다.
VM 쪽 `site/crash_at.py`가 상태 파일(원자적 rename)을 inotify로 보다가 목표 phase에 들어간 순간 실행 중인
프로세스를 `SIGSTOP`으로 얼려 그 phase에 붙잡아 두고, `kill`이면 `systemctl kill -s KILL`, `reset`이면
호스트가 `Restart-VM -Type Reset -Force`(전원 리셋)를 건다. `fetched`(번들 검증)는 CLI가 실행하므로 CLI를 대상으로 한다.
`migrating`은 실제 마이그레이션이 일어나도록 `c0`(epoch 1)에서, `rolling_back`은 r1′로 일으킨다.

서비스 tick 주기는 e2e에서 `PUREIRON_POLL_INTERVAL_S=10`(기본 60)으로 줄인다.

판정(모두 참이어야 통과, 결과의 `checks`): 결과(`succeeded`/`failed`) · **경보 없음·보고 `failed_manual`
false**(`last_result`는 `failed_manual`도 `failed`로 접는다) · `applied_gen`(성공이면 새 gen, 실패면 그대로) ·
실행 이미지 digest · 편집 잠금 해제 · 이력 행 보존 · 감시자 종료 코드 0(얼린 프로세스 있음) · 주입 효과
(reset이면 boot id 변경, 서비스 kill이면 `NRestarts` 증가) · service 대상이면 CLI 스테이징이 창 열림 전에 끝남
(아니면 CLI가 phase를 직접 실행해 쉬는 서비스를 죽이는 거짓 통과가 된다).

crash 지점의 실제 의미: 감시자는 상태 파일이 그 phase로 **저장된 직후** 서비스 cgroup 전체를 `SIGSTOP`한다.
컨테이너(dockerd 소유)는 계속 돌기 때문에 `migrating`(약 2 s)은 마이그레이션 SQL 도중이 아니라 "phase 기록 뒤,
다음 기록 전"을 끊는다 — SQL 중간 중단은 core 통합 테스트(0006·0007 crash 상태)가 맡는다. `fetched`는 서명 검증
뒤 gen이 기록된 다음(이미지 적재 중), `verified`는 CLI가 스테이징을 마치고 창을 기다리는 동안이다.

### 외부 접속 카운터

체크포인트 복원은 VM 메모리(카운터 포함)를 c0 시점으로, 전원 리셋은 0으로 되돌린다. 그래서 하네스는 복원·리셋
**직전**마다 카운터를 읽어 누적하고(`nft_mark`), 직후 기준을 c0 값·0으로 다시 잡는다. 외부 DNS 패킷은 모두
systemd-resolved가 보내므로(컨테이너 질의도 Docker 내장 DNS → resolved), seed가 resolved를 디버그 로그로 두고
하네스가 표시마다 조회 이름을 모은다(`dns_lookups`) — 카운터가 0이 아니면 그 이름이 출처 증거다.

## 단위 테스트

`tests/e2e/tests/` — phase 순서·상태 읽기·nft 규칙·seed ISO 왕복·PowerShell 인용·수렴 판정. G1 `unit`에 포함된다.

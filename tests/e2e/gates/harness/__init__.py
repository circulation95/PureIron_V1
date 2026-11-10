"""e2e 하네스 공용 모듈 — M2d `run.py`와 G2 시나리오(`tests/e2e/gates/test_g2_*.py`)가 같이 쓴다.

- `paths` — 저장소·e2e 작업 디렉터리·WSL 사본 경로
- `release_pair` — `ReleasePair`(release 쌍과 스키마·트리 기대값), 지금 쌍 `DEFAULT`
- `hyperv`·`golden`·`vm` — Hyper-V VM, golden VHDX·seed ISO·nftables 감시 규칙, ssh
- `bundles`·`central` — WSL runner에서 스크립트로 도는 번들 재료·중앙 c0
- `buildhost` — Windows에서 WSL을 부르는 쪽(동기화·번들 빌드·중앙 조작·`/fleet` 대기)
- `context` — `Context`(결과 기록·상태 파일), `E2EError`, `log`
- `site` — `Site`(현장 VM 하나)와 원격 조작(probe·스테이징·수렴·보고·카운터·복원)
- `judges` — 순수 판정 함수(I/O 없음)
- `trees` — 프로젝트 트리 기대값(로컬 트리 디렉터리 읽기)
"""

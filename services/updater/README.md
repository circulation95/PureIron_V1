# pureiron-updater

현장 호스트의 systemd 서비스. Python 3.12 venv tarball(`/opt/pureiron/updater/<ver>`)로 배포한다(ADR-027, PD-5).

- **범위: M2** — DesiredState polling, `.pib` 검증·적용 7단, 백업·롤백, `recover()`.
- M1에서는 uv workspace 멤버와 G1 게이트(ruff·pyright strict·pytest) 대상 자리만 둔다.

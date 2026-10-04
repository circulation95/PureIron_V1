# 새 현장 호스트 설치 (설치 번들, plan M2 B3)

설치 번들 `pureiron-install-<release>.pib`는 현장 번들과 같은 `.pib` 형식·서명 체인이다(계약 `packages/contracts/install-manifest.schema.json`, 검증 `pureiron_bundle.verify.verify_install_bundle`). 인터넷 없이 Ubuntu 24.04 호스트에 Docker·Updater를 깐다.

## 번들 만들기 (빌드 PC, 네트워크 필요)

```bash
bash deploy/install/debs/fetch_debs.sh out/debs       # Docker deb 4종 + debs.json (.deb는 커밋하지 않음)
bash deploy/build/build_updater.sh out/updater        # updater-<ver>.tar.gz (ubuntu:24.04, /opt/pureiron/updater/<ver>)
uv run python deploy/bundle/build_install_bundle.py --release 2026.10.0 \
  --updater-tar out/updater/updater-0.1.0.tar.gz --debs-dir out/debs \
  --release-key ~/pureiron/keys/test/release.key --cert-dir ~/pureiron/keys/test \
  --root-pub ~/pureiron/keys/test/test-root.pub --out out/bundles
```

결과: `pureiron-install-<release>.pib`와 `<같은 이름>.runbook.txt`. runbook 파일의 **root 지문**과 **`bootstrap-verify.sh` sha256**을 종이 runbook(서명된 납품 문서)에 옮겨 인쇄한다 — 현장 운영자가 번들과 다른 경로로 받은 이 두 값으로 번들을 믿는다.

- deb: docker-ce·docker-ce-cli·containerd.io·docker-compose-plugin(amd64). 빌더가 `debs.json`의 sha256·패키지 집합과 compose 최소 버전(`COMPOSE_MIN` = 2.17.0, Updater의 `up --wait-timeout`)을 확인한다.
- 스크립트는 LF만 허용(CR이 있으면 빌드 실패). `.pib` 멤버 권한은 형식상 0644라 스크립트는 `bash <파일>`로 실행한다.

## 현장 설치 (운영자, root)

| 단계 | 명령 | 실패 시 |
|---|---|---|
| 1 | 번들 2개(`pureiron-install-*.pib`, `pureiron-<site>-g<gen>.pib`)를 호스트에 복사 | — |
| 2 | `tar -xf pureiron-install-*.pib bootstrap/ manifest.json manifest.sig release-cert.json release-cert.sig keys/` → `sha256sum bootstrap/bootstrap-verify.sh`를 runbook 값과 대조 → `sudo bash bootstrap/bootstrap-verify.sh --root-fpr <runbook 지문> --bundle pureiron-install-*.pib --dest /root/pureiron-install` | 중단(아무것도 설치되지 않음) |
| 3~5a | `sudo bash /root/pureiron-install/install/install.sh --bundle pureiron-install-*.pib [--offline-time-confirmed]` | 이번 실행이 만든 것을 되돌림 |
| 6 | `sudo pureiron-site enroll --site <id> --out /media/usb/enroll-<id>.json` → (온라인) `sudo pureiron-site secret set control_api_token < token.txt` → `sudo pureiron-site apply pureiron-<site>-g<gen>.pib` → `sudo systemctl start pureiron-updater` | apply 결과는 `pureiron-site status` |

- **bootstrap-verify.sh**(openssl 1회용 경로): root 지문 → PEM 안의 raw 키 == `keys/root.pub` → root가 cert에 한 서명 → release key가 manifest에 한 서명(`kind: install`, `created_at` ∈ cert 유효기간) → 서명된 `files` 경로만 풀고 일반 파일·sha256 대조. 스크립트 자체를 믿지 않으면 runbook 부록의 원시 openssl 명령을 손으로 친다.
- **preflight.sh**: Ubuntu 24.04·x86_64, `/usr/bin/python3.12`, `/var/lib/pureiron` 여유 ≥ 50 GiB, 80/443 미사용, NTP 동기화(폐쇄망은 시계를 직접 확인하고 `--offline-time-confirmed`), Docker 의존 패키지(iptables·libseccomp2) 있음, 충돌 패키지(docker.io·containerd·runc·podman-docker) 없음.
- **install.sh**: `daemon.json`(`containerd-snapshotter: false`, PD-19)을 **deb보다 먼저** 둔다 — 패키지 postinst가 dockerd를 처음 띄울 때 이미 classic image store여야 `.Id` == config digest가 성립한다. 이어 `dpkg -i` → `systemctl enable --now docker` → storage driver `overlay2`·compose ≥ 최소 버전 확인 → Updater venv(`/opt/pureiron/updater/<ver>` + `current` symlink, `/usr/local/bin/pureiron-site`) → `/etc/pureiron/root.pub`·`updater.env` 템플릿 → systemd unit enable(start는 6단계 뒤) → **5a 설치된 Updater로 재검증**(`pureiron-site bundle verify --kind install`) → `db_password`·`updater_token` 생성(없을 때만).
- 이미 있는 `/etc/pureiron/root.pub`·`/etc/docker/daemon.json`이 번들과 다르면 덮어쓰지 않고 실패한다(신뢰 루트·데몬 설정 변경은 의식적인 작업).
- 되돌리기(`install.sh` 실패): `dpkg -r` 4종, venv·symlink·unit·root.pub·daemon.json·env 템플릿 중 이번 실행이 만든 것.

## Updater 교체 (수동 유지보수)

새 설치 번들로 2단계 후 `sudo bash <dest>/install/install.sh --bundle <새 install.pib> --updater-only` — preflight·Docker를 건너뛰고 5·5a만 한다. 재검증에 실패하면 이전 `current`로 되돌린다. 서비스가 돌고 있었으면 재시작한다. 이후 site 번들의 `min_updater`가 이 버전보다 크면 `E_UPDATER`로 거부된다.

## 테스트

`deploy/tests/test_install_bundle.py` — 빌더(결정성·deb/compose 검사), `bootstrap-verify.sh`(통과·지문/payload/manifest/cert 변조), `preflight.sh`·`install.sh`(호스트 명령은 PATH 스텁, 5a는 실제 Updater CLI)와 실패 시 되돌리기. `install.sh` 테스트는 symlink가 필요해 Linux(CI)에서만 돈다. 실제 호스트 설치·네트워크 차단·재부팅은 M2d(Hyper-V VM).

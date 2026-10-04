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
| 2 | **전용 빈 디렉터리**에서 이름을 지정해 메타데이터만 푼다: `install -d -m 700 /root/pib-meta && cd /root/pib-meta && tar --force-local --no-same-owner --no-same-permissions -xf <번들> manifest.json manifest.sig release-cert.json release-cert.sig keys/root.pub keys/root.pem bootstrap/bootstrap-verify.sh` → `sha256sum bootstrap/bootstrap-verify.sh`를 runbook 값과 대조 → `sudo bash bootstrap/bootstrap-verify.sh --root-fpr <runbook 지문> --bundle <번들> --dest /root/pureiron-install` | 중단(아무것도 설치되지 않음) |
| 3~5a | `sudo bash /root/pureiron-install/install/install.sh --bundle pureiron-install-*.pib [--offline-time-confirmed]` | 이번 실행이 만든 것을 되돌림 |
| 6 | `sudo pureiron-site enroll --site <id> --out /media/usb/enroll-<id>.json` → (온라인) `sudo pureiron-site secret set control_api_token < token.txt` → `sudo pureiron-site apply pureiron-<site>-g<gen>.pib` → `sudo systemctl start pureiron-updater` | apply 결과는 `pureiron-site status` |

- **검증 전 추출은 위험 구간이다**: 아직 믿을 수 없는 tar이므로 `/root` 같은 곳에 통째로 풀지 않는다. 변조 번들이 `bootstrap -> .` 링크와 `json.py` 같은 모듈을 심어, 검증 스크립트가 부르는 python이 그것을 import하게 만들 수 있다. 그래서 스크립트는 ⓪ 현재 디렉터리에 위 7개 파일 외의 것이나 symlink가 있으면 즉시 멈추고, python은 항상 격리 모드(`-I`, 현재 디렉터리·환경 경로 미사용)로 부른다.
- **bootstrap-verify.sh**(openssl 1회용 경로): root 지문 → PEM 안의 raw 키 == `keys/root.pub` → root가 cert에 한 서명 → release key가 manifest에 한 서명(`kind: install`, `created_at` ∈ cert 유효기간) → 서명된 `files` 경로만 풀고 일반 파일·sha256 대조. 스크립트 자체를 믿지 않으면 runbook 부록의 원시 openssl 명령을 손으로 친다.
- **preflight.sh**: Ubuntu 24.04·x86_64, `/usr/bin/python3.12`, `/var/lib/pureiron` 여유 ≥ 50 GiB, 80/443 미사용, NTP 동기화(폐쇄망은 시계를 직접 확인하고 `--offline-time-confirmed`), Docker 의존 패키지(iptables·libseccomp2) 있음, 충돌 패키지(docker.io·containerd·runc·podman-docker) 없음.
- **install.sh**: `daemon.json`(`containerd-snapshotter: false`, PD-19)을 **deb보다 먼저** 둔다 — 패키지 postinst가 dockerd를 처음 띄울 때 이미 classic image store여야 `.Id` == config digest가 성립한다. 이어 `dpkg -i` → `systemctl enable --now docker` → storage driver `overlay2`·compose ≥ 최소 버전 확인 → Updater venv(`/opt/pureiron/updater/<ver>` + `current` symlink, `/usr/local/bin/pureiron-site`) → `/etc/pureiron/root.pub`·`updater.env` 템플릿 → systemd unit enable(start는 6단계 뒤) → **5a 설치된 Updater로 재검증**(`pureiron-site bundle verify --kind install`) → `db_password`·`updater_token` 생성(없을 때만).
- 이미 있는 `/etc/pureiron/root.pub`·`/etc/docker/daemon.json`이 번들과 다르면 덮어쓰지 않고 실패한다(신뢰 루트·데몬 설정 변경은 의식적인 작업).
- 이미 깔린 Docker 패키지는 번들과 **같은 버전일 때만** 그대로 쓴다(재실행). 다른 버전이면 아무것도 바꾸기 전에 멈춘다 — 되돌릴 때 원래 버전을 복원할 deb가 없기 때문이다.
- 되돌리기(`install.sh` 실패): **이번 실행이 새로 설치한** Docker 패키지만 `systemctl disable --now docker` 후 `dpkg -r`, 그리고 venv·symlink·unit·root.pub·daemon.json·env 템플릿·새로 만든 secret 중 이번 실행이 만든 것.
- systemd unit은 `ConditionPathExists=/var/lib/pureiron/state/site.json` — 등록 전에는 서비스가 뜨지 않는다(현장 id 없이 재시작을 반복하지 않게).

## Updater 교체 (수동 유지보수)

새 설치 번들로 2단계 후 `sudo bash <dest>/install/install.sh --bundle <새 install.pib> --updater-only` — preflight·Docker를 건너뛰고 5·5a만 한다. unit 파일은 새 릴리스 것으로 바꾼다(신뢰 루트 `root.pub`는 바꾸지 않음). 재검증에 실패하면 이전 `current`와 unit으로 되돌린다. 서비스가 돌고 있었으면 재시작한다. 이후 site 번들의 `min_updater`가 이 버전보다 크면 `E_UPDATER`로 거부된다.

## 테스트

`deploy/tests/test_install_bundle.py` — 빌더(결정성·deb/compose 검사), `bootstrap-verify.sh`(통과·지문/payload/manifest/cert 변조·심어진 모듈/symlink 거부), `preflight.sh`·`install.sh`(호스트 명령은 PATH 스텁, 5a는 실제 Updater CLI)와 실패 시 되돌리기(기존 Docker 보존·다른 버전 거부·`--updater-only` 교체/복원). `install.sh` 테스트는 symlink가 필요해 Linux(CI)에서만 돈다. 실제 호스트 설치·네트워크 차단·재부팅은 M2d(Hyper-V VM).

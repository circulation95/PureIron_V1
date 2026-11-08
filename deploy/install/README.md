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
| 6 | `sudo pureiron-site enroll --site <id> --out /media/usb/enroll-<id>.json` → (온라인, 아래 "온라인 현장") `sudo pureiron-site secret set control_api_token < token.txt` → `sudo pureiron-site apply pureiron-<site>-g<gen>.pib` → `sudo systemctl start pureiron-updater` | apply 결과는 `pureiron-site status` |

- **검증 전 추출은 위험 구간이다**: 아직 믿을 수 없는 tar이므로 `/root` 같은 곳에 통째로 풀지 않는다. 변조 번들이 `bootstrap -> .` 링크와 `json.py` 같은 모듈을 심어, 검증 스크립트가 부르는 python이 그것을 import하게 만들 수 있다. 그래서 스크립트는 ⓪ 현재 디렉터리에 위 7개 파일 외의 것이나 symlink가 있으면 즉시 멈추고, python은 항상 격리 모드(`-I`, 현재 디렉터리·환경 경로 미사용)로 부른다.
- **bootstrap-verify.sh**(openssl 1회용 경로): root 지문 → PEM 안의 raw 키 == `keys/root.pub` → root가 cert에 한 서명 → release key가 manifest에 한 서명(`kind: install`, `created_at` ∈ cert 유효기간) → 서명된 `files` 경로만 풀고 일반 파일·sha256 대조. 스크립트 자체를 믿지 않으면 runbook 부록의 원시 openssl 명령을 손으로 친다.
- **preflight.sh**: Ubuntu 24.04·x86_64, `/usr/bin/python3.12`, `/var/lib/pureiron` 여유 ≥ 50 GiB, 80/443 미사용, NTP 동기화(폐쇄망은 시계를 직접 확인하고 `--offline-time-confirmed`), Docker 의존 패키지(iptables·libseccomp2) 있음, 충돌 패키지(docker.io·containerd·runc·podman-docker) 없음.
- **install.sh**: `daemon.json`(`containerd-snapshotter: false`, PD-19)을 **deb보다 먼저** 둔다 — 패키지 postinst가 dockerd를 처음 띄울 때 이미 classic image store여야 `.Id` == config digest가 성립한다. 이어 `dpkg -i` → `systemctl enable --now docker` → storage driver `overlay2`·compose ≥ 최소 버전 확인 → Updater venv(`/opt/pureiron/updater/<ver>` + `current` symlink, `/usr/local/bin/pureiron-site`) → `/etc/pureiron/root.pub`·`updater.env` 템플릿 → systemd unit enable(start는 6단계 뒤) → **5a 설치된 Updater로 재검증**(`pureiron-site bundle verify --kind install`) → `db_password`·`updater_token` 생성(없을 때만).
- 이미 있는 `/etc/pureiron/root.pub`·`/etc/docker/daemon.json`이 번들과 다르면 덮어쓰지 않고 실패한다(신뢰 루트·데몬 설정 변경은 의식적인 작업).
- 이미 깔린 Docker 패키지는 번들과 **같은 버전일 때만** 그대로 쓴다(재실행). 다른 버전이면 아무것도 바꾸기 전에 멈춘다 — 되돌릴 때 원래 버전을 복원할 deb가 없기 때문이다.
- 되돌리기(`install.sh` 실패): **이번 실행이 새로 설치한** Docker 패키지만 `systemctl disable --now docker` 후 `dpkg -r`, 그리고 venv·symlink·unit·root.pub·daemon.json·env 템플릿·새로 만든 secret 중 이번 실행이 만든 것.
- systemd unit은 `ConditionPathExists=/var/lib/pureiron/state/site.json` — 등록 전에는 서비스가 뜨지 않는다(현장 id 없이 재시작을 반복하지 않게).

## 온라인 현장: 중앙 연결과 사설 CA 신뢰 (M4 마감 D8)

폐쇄망 현장은 해당 없다(`PUREIRON_CONTROL_API`가 없으면 Updater는 polling·송신을 하지 않는다). 온라인 현장은 6단계 뒤 아래를 한 번 한다. PoC 중앙(`deploy/central`)의 HTTPS 인증서는 Caddy 사설 CA(`tls internal`)가 발급하므로, **현장이 그 루트 CA를 OS 신뢰 저장소에 넣어야** Updater가 중앙에 붙는다. Updater(Python `urllib`, 시스템 python3 venv)는 기본 SSL 설정으로 OS 신뢰 저장소(`/etc/ssl/certs`)를 쓴다 — 코드 설정이 없다.

벤더에게서 받는 것: 중앙 기준 URL(`https://<중앙>:28443/api/v1`), 루트 CA 파일 `pureiron-central-root.crt`, 그 **SHA-256 지문**(CA 파일과 **다른 경로**로 — 종이 runbook·전화 등), 현장 token(벤더가 6단계 enrollment로 등록하면 1회 발급).

```bash
openssl x509 -in pureiron-central-root.crt -noout -subject -fingerprint -sha256   # 받은 지문과 한 글자씩 대조 — 다르면 멈춘다
sudo install -m 0644 pureiron-central-root.crt /usr/local/share/ca-certificates/pureiron-central-root.crt
sudo update-ca-certificates                                                      # "1 added"
curl -fsS https://<중앙>:28443/api/v1/healthz                                    # --cacert 없이 200 {"status":"ok",…}
echo 'PUREIRON_CONTROL_API=https://<중앙>:28443/api/v1' | sudo tee -a /etc/pureiron/updater.env
sudo pureiron-site secret set control_api_token < token.txt && shred -u token.txt
sudo systemctl restart pureiron-updater
```

- 지문 대조가 이 절차의 핵심이다. CA 파일과 지문을 같은 경로(같은 메일·같은 USB)로 받으면 대조가 의미 없다 — 그 경로를 바꿔치기한 사람이 둘 다 바꿀 수 있다.
- `SSL_CERT_FILE`·`REQUESTS_CA_BUNDLE`로 CA를 지정하지 않는다 — 그 프로세스가 믿는 대상이 그 파일 하나로 바뀐다.
- 확인: 벤더 쪽 `GET /api/v1/fleet`(control-web 화면)에서 그 현장의 connection이 `online`(마지막 수신 ≤ 3 × poll 주기). 현장 쪽은 `journalctl -u pureiron-updater`에 `heartbeat … not sent`·`CERTIFICATE_VERIFY_FAILED`가 없어야 한다.
- 중앙이 꺼져도 현장은 그대로 돈다(Graceful degradation) — Updater는 다음 tick에 다시 시도하고 경보를 세우지 않는다. 중앙에서는 그 현장이 `stale`로 보인다(M4 마감 VM 온라인 ④).
- 중앙 루트 CA가 바뀌면(중앙 `caddy-data` 볼륨 삭제·재설치) 위 절차를 새 CA로 다시 한다. 옛 파일은 같은 이름에 덮어쓰고 `update-ca-certificates`.
- 공인 인증서(M6, ACME)로 가면 이 CA 절차는 없어진다(OS 기본 신뢰 저장소에 이미 있는 CA).

## Updater 교체 (수동 유지보수)

새 설치 번들로 2단계 후 `sudo bash <dest>/install/install.sh --bundle <새 install.pib> --updater-only` — preflight·Docker를 건너뛰고 5·5a만 한다. unit 파일은 새 릴리스 것으로 바꾼다(신뢰 루트 `root.pub`는 바꾸지 않음). 재검증에 실패하면 이전 `current`와 unit으로 되돌린다. 서비스가 돌고 있었으면 재시작한다. 이후 site 번들의 `min_updater`가 이 버전보다 크면 `E_UPDATER`로 거부된다.

## 테스트

`deploy/tests/test_install_bundle.py` — 빌더(결정성·deb/compose 검사), `bootstrap-verify.sh`(통과·지문/payload/manifest/cert 변조·심어진 모듈/symlink 거부), `preflight.sh`·`install.sh`(호스트 명령은 PATH 스텁, 5a는 실제 Updater CLI)와 실패 시 되돌리기(기존 Docker 보존·다른 버전 거부·`--updater-only` 교체/복원). `install.sh` 테스트는 symlink가 필요해 Linux(CI)에서만 돈다. 실제 호스트 설치·네트워크 차단·재부팅은 M2d(Hyper-V VM).

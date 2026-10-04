# 번들 서명 키 (ADR-023)

신뢰 체인: **root key**(오프라인) → **release cert**(release 공개키·serial·유효기간, root 서명) → **manifest 서명**(release key). 검증은 `packages/bundle-tools` 한 벌이 한다.

> 이 디렉터리에는 **공개 정보만** 둔다(`*.pub`, 공개키 `*.pem`). 개인키(`*.key`·`*.seed`)는 어떤 경우에도 커밋하지 않는다. `.gitignore`는 `*.key`·`*.seed`·`*.pem`을 막고 `deploy/keys/*.pem`(공개키 PEM)만 예외로 두지만, 그것에 기대지 말고 개인키는 애초에 저장소 밖에서 만든다.

## 현재 상태 (결정 D10)

- **운영 root key는 아직 만들지 않았다.** 그래서 `root.pub`도 없다. 첫 계약 전까지는 **test key만** 쓴다.
- test root 공개키(`test-root.pub`)는 runner 호스트에서 생성한 뒤 공개키만 커밋한다(M2 이후 단계).

## 파일 형식

| 파일 | 형식 |
|---|---|
| `*.pub` | Ed25519 공개키 **raw 32바이트**(정본) |
| `*.key` | Ed25519 개인키 seed **raw 32바이트**, 권한 0600, 새 파일로만 생성(덮어쓰기 거부) |
| 지문 | `sha256(raw 32B)` hex — `pureiron-bundle keys fingerprint <pub>` |
| `root.pem` | openssl용 SubjectPublicKeyInfo PEM. raw에서 결정적으로 생성(`keys export-pem`, DER 접두 `302a300506032b6570032100` + raw 32B) |
| `release-cert.json` | JCS 바이트 `{"format":1,"not_after":"…Z","not_before":"…Z","purpose":"release-cert","release_pubkey":"<hex>","serial":N}` |
| `release-cert.sig` | root가 `release-cert.json` 파일 바이트에 한 서명(raw 64바이트) |
| `trust-update.sig` | root가 JCS(`{"min_cert_serial":N,"purpose":"trust-update"}`)에 한 서명 — 번들에 선택적으로 포함. manifest의 `trust_update`는 `{min_cert_serial}`만 갖고, `purpose`는 검증기가 덧붙인다 |

root 서명은 용도별로 도메인을 분리한다(`purpose`). 그래서 cert용 root 서명을 trust_update로, 또는 그 반대로 재사용할 수 없다. cert 서명은 파일 바이트 그대로라 부트스트랩의 `openssl pkeyutl -verify -pubin -inkey keys/root.pem -rawin -in release-cert.json -sigfile release-cert.sig` 경로가 그대로 동작한다.

## root key 의식 (운영 키, 아직 미실시)

1. 네트워크를 끊은 전용 노트북(또는 라이브 USB)에서 `pureiron-bundle keys gen --out <usb> --name root`.
2. `root.key`를 **사본 2부**로 만들어 **서로 다른 장소**에 보관한다(예: 사무실 금고·대표 자택 금고). 분실하면 모든 현장의 신뢰 루트를 다시 심어야 하므로 치명적이다.
3. 같은 자리에서 release key 공개키로 cert를 발급한다: `pureiron-bundle cert issue --root-key root.key --release-pub release.pub --serial <N> --out <dir>` — 유효기간 기본 **180일**, serial은 발급할 때마다 +1.
4. `root.pub`·`root.pem`·지문을 저장소(`deploy/keys/`)와 runbook(종이)에 기록한다. 지문은 새 호스트 부트스트랩에서 운영자가 대조한다.
5. 노트북의 키 파일을 지운다. root key는 cert 갱신·`trust_update` 서명 때만 오프라인으로 꺼낸다.

### cert 갱신과 폐기

- cert는 180일마다 다시 발급한다. 번들 검증은 벽시계가 아니라 **서명된 `created_at`** 으로 cert 유효기간을 본다.
- release key가 유출되면 새 release key + serial을 올린 cert를 발급하고, 다음 번들에 manifest `trust_update: {min_cert_serial: <새 serial>}`과 root가 서명한 `trust-update.sig`를 함께 넣는다. **`min_cert_serial`을 올리는 방법은 이것 하나뿐이다**(서명이 없거나 틀린 `trust_update`는 무시된다). 현장은 그 번들을 받은 뒤 옛 serial의 cert로 서명된 번들을 `E_CERT`로 거부한다.

### runbook: `max_seen_created_at` 오염 복구

유출된 release key로 `created_at`을 cert `not_after` 근처(미래)로 찍은 번들이 현장에서 한 번이라도 검증되면, 그 현장의 `max_seen_created_at`이 그 값으로 올라가 이후 정상 번들(현재 시각의 `created_at`)이 모두 `E_CERT`(역행)로 거부된다. 복구:

1. root로 새 release key의 cert를 **더 높은 serial**로 발급한다(`not_before`는 지금, 유효기간이 오염된 값 이후까지 덮도록).
2. 다음 번들에 `trust_update: {min_cert_serial: <새 serial>}` + `trust-update.sig`를 넣어 유출된 cert를 무효화한다.
3. 그 번들(과 이후 번들)의 `created_at`을 **오염된 값 이상**으로 찍는다(빌더 `--created-at`). 새 cert 유효기간 안이어야 하므로 1단계에서 기간을 그에 맞춘다.
4. 오염된 값이 지나 실제 시각이 따라잡으면 `--created-at` 지정을 멈춘다.

## test key 다루기

- test root 개인키는 **runner 호스트에만** 둔다: WSL `~/pureiron/keys/test/`(디렉터리 0700, 키 0600). 저장소·Actions secret·공유 드라이브에 올리지 않는다.
- 생성 예:

```bash
mkdir -p ~/pureiron/keys/test && chmod 700 ~/pureiron/keys/test
uv run pureiron-bundle keys gen --out ~/pureiron/keys/test --name test-root
uv run pureiron-bundle keys gen --out ~/pureiron/keys/test --name release
uv run pureiron-bundle cert issue --root-key ~/pureiron/keys/test/test-root.key \
  --release-pub ~/pureiron/keys/test/release.pub --serial 1 --out ~/pureiron/keys/test
```

- 번들 빌드: `deploy/bundle/build_bundle.py --release-key ~/pureiron/keys/test/release.key --cert-dir ~/pureiron/keys/test --root-pub ~/pureiron/keys/test/test-root.pub …`(쓴 뒤 같은 검증 코드로 자체 검증).
- 단위 테스트는 실행할 때마다 임시 키를 만들므로 이 키가 필요 없다.

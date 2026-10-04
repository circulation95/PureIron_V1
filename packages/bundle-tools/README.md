# pureiron-bundle-tools

`.pib` 서명 번들의 생성·검증 공용 라이브러리. CI(서명), Updater(검증), control-api(게시 검사)가 같은 코드를 쓴다(ADR-023, P3). 검증 진입점은 `pureiron_bundle.verify.verify_bundle` 하나다.

| 모듈 | 내용 |
|---|---|
| `errors.py` | 거부 코드 `E_SIG`·`E_CERT`·`E_SITE`·`E_GEN`·`E_DIGEST`·`E_SCHEMA`·`E_UPDATER`·`E_DRAFT` + `E_FORMAT`(tar 구조·서명된 메타데이터의 형식 위반), `BundleRejectedError(code, detail)` |
| `keys.py` | Ed25519 raw 32B 공개키·raw 64B 서명·지문 `sha256(raw32)`, 개인키 = raw 32B seed(0600, 덮어쓰기 금지), 결정적 PEM export/import |
| `trust.py` | `release-cert.json`(JCS, `purpose: release-cert`) 발급·검증, 서명된 `created_at` 기준 유효기간, `TrustState`(min_cert_serial·max_seen_created_at), root 서명 `trust-update.sig`(JCS `{purpose: trust-update, min_cert_serial}`), 중복 키·실수 거부 JSON |
| `manifest.py` | 계약 스키마(`schema/` = `packages/contracts` 사본, contracts-drift 검사) 검증, 타입 접근자, JCS 바이트 |
| `pib.py` | 결정적 writer, 스트리밍·적대적 입력 안전 reader(끝 표시·패딩 검사 포함) |
| `tarsafe.py` | 모든 tar 읽기에 쓰는 `BoundedTarInfo`: pax 확장 헤더 ≤ 64 KiB·멤버당 1개, 전역 헤더·GNU long name/link·sparse 거부 |
| `images.py` | docker save tar(.tar/.tar.gz)의 config digest(`manifest.json` 정확히 1개), load 후 `.Id` 대조 helper |
| `sign.py` | payload sha256 → manifest 서명 → `.pib` 쓰기(빌더용) |
| `verify.py` | 고정 순서 ①~⑥ 검증과 순수 판정 규칙(gen·스키마 envelope·Updater 버전·드래프트 게이트) |

## `.pib` 형식

압축하지 않은 POSIX tar(pax). 멤버 순서 고정: `manifest.json`, `manifest.sig`(release key), `release-cert.json`, `release-cert.sig`(root), 선택 `trust-update.sig`(root), 그다음 payload(경로 UTF-8 바이트 순), 끝은 0 블록 2개 + 10240바이트 레코드 경계까지 0 패딩. mtime 0·uid/gid 0·빈 uname/gname·0644·일반 파일만. reader는 절대 경로·`..`·역슬래시·계약 패턴 밖 경로·링크·디렉터리·장치·sparse·중복·순서 위반·1 MiB 초과 메타데이터·헤더 제한 위반·끝 표시 뒤 0이 아닌 바이트나 군더더기를 `E_FORMAT`으로 거부하고, 메타데이터 서명을 확인한 뒤에만 payload를 해시한다.

## 검증 규칙 요약

- ① root→cert→manifest 서명(`E_SIG`), `not_before ≤ created_at ≤ not_after`·`created_at ≥ max_seen_created_at`·`serial ≥ min_cert_serial`(`E_CERT`). manifest `trust_update`는 유효한 `trust-update.sig`가 있을 때만 하한을 올린다(없거나 틀리면 무시).
- ② `site_id`(`E_SITE`) ③ gen: `gen < max(applied, last_attempted)` 거부, `== applied` NO_OP(④~⑥ 생략), `== last_attempted`는 `pending`이면 RESUME 아니면 거부.
- ④ payload sha256·누락·초과(`E_DIGEST`). **해시 먼저, 해석은 나중**: `extract_to`가 있으면 `<name>.part`에 쓰고 sha256이 맞으면 이름을 바꾼 뒤, 추출본에서 `images/<svc>.tar[.gz]`의 config digest == manifest를 확인한다. `extract_to`가 없으면 이미지 tar는 해석하지 않으며, 이미지 신원은 Updater의 load 후 `check_loaded_image`(`.Id`)가 맡는다. 적용은 반드시 추출본으로 하고 `.pib`를 다시 열지 않는다. 거부·예외 시 `extract_to`는 비운다.
- ⑤ envelope 일관성(`max = expected + 1`, `target = expected`), `cur > max` 또는 `expected − cur > 1` 거부, `== 1`이면 MIGRATE, 새 설치(None)는 0부터 MIGRATE. `min_updater ≤ updater`(semver, `E_UPDATER`).
- ⑥ `absorbs[site]`가 있으면 `site_rev ≤ absorbs[site]`, 없으면 `site_rev == base_site_rev`(`E_DRAFT`).

## CLI

```bash
pureiron-bundle keys gen --out DIR --name test-root        # test-root.key(0600) + test-root.pub
pureiron-bundle keys fingerprint DIR/test-root.pub
pureiron-bundle keys export-pem DIR/test-root.pub > root.pem
pureiron-bundle cert issue --root-key DIR/test-root.key --release-pub DIR/release.pub --serial 1 --out DIR
pureiron-bundle verify site.pib --root-pub DIR/test-root.pub --site site-a [--updater-version 0.1.0] [--extract-to DIR]
```

`verify`는 새 설치 상태 기준으로 검증하고 결과 JSON을 출력한다. 거부 시 종료 코드 1, stderr 첫 줄이 `<거부 코드>: <상세>`. 키 보관 규칙은 `deploy/keys/README.md`.

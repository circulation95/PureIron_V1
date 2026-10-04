# pureiron-project-tools

고객 프로젝트 repo(`pureiron-project-<id>`)의 정규화·검증·버전 식별 도구(ADR-025, plan §6.3). **M1 최소판**이다.

```bash
pureiron-project validate <dir>      # 스키마(packages/contracts/project.schema.json) + 교차 참조 검증
pureiron-project fmt [--check] <dir> # 정규형으로 다시 쓰기 / 불일치만 보고(CI)
pureiron-project hash <dir>          # content hash (sha256:…)
```

## 트리

`project.yaml`, `devices/<id>.yaml`, `tags/<device>.yaml`, `alarms/*.yaml`. `screens/`·`assets/`는 예약(비어 있어야 함, `.gitkeep`만 허용). 그 밖의 파일(README, `.github/` 등)은 프로젝트 내용이 아니라 무시한다. 예시는 `fixtures/acme/`.

## 정규형

- YAML 1.2 strict 로더: 중복 키·anchor/alias·명시적 tag·문자열 아닌 키·JSON 밖의 값(timestamp, NaN, ±2^53 초과 정수)을 거부한다.
- 출력: 키 정렬, 2칸 block YAML, 숫자는 RFC 8785(JCS) 표현, 문자열 UTF-8 NFC, LF, 스키마 기본값과 같은 필드 생략. 다시 읽어 같은 문자열이 되는 경우에만 따옴표를 생략한다(YAML 1.1 도구가 bool로 읽는 `yes`·`on` 등은 따옴표).
- 파일 경로는 POSIX 상대 경로로 다룬다(Windows 경로 입력은 정규화, 절대 경로·`..` 거부).

## content hash

`sha256:` + sha256(JCS(`[[posix_relpath, 정규 데이터], …]`, 경로 UTF-8 바이트 순)). 정규 데이터는 NFC·기본값 생략을 적용한 값이라 서식·키 순서·줄바꿈·기본값 명시 여부가 달라도 내용이 같으면 해시가 같다. 버전 이름은 고객 repo 태그 `vN`이다.

## 범위 밖(이후 단계)

- legacy 프로젝트 변환기(지원 항목·미지원 보고)와 드래프트 회수(`absorb`)는 **M5**.
- 고객 repo용 재사용 workflow(`validate-project.yml`)도 **M5**.

`src/pureiron_project/schema/project.schema.json`은 계약 정본의 복사본(생성물)이다 — `bash scripts/g1.sh contracts-gen`으로 갱신하고 `contracts-drift`가 동기를 검사한다.

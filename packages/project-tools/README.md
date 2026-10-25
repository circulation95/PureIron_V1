# pureiron-project-tools

고객 프로젝트 repo(`pureiron-project-<id>`)의 정규화·검증·버전 식별 도구(ADR-025, plan §6.3). **M1 최소판** + legacy 변환기(M5-1)다.

```bash
pureiron-project validate <dir>      # 스키마(packages/contracts/project.schema.json) + 교차 참조 검증
pureiron-project fmt [--check] <dir> # 정규형으로 다시 쓰기 / 불일치만 보고(CI)
pureiron-project hash <dir>          # content hash (sha256:…)
pureiron-project convert-legacy <legacy_dir> --out <dir> --report <json>  # legacy 변환(M5-1)
```

## legacy 변환기

`project.json`, Modbus TCP 연결·태그, 상·하한 알람만 변환하고(PD-12), 나머지(OPC-UA·UDT·scripts·dashboard·Memory/expression/query 태그·shelve·`auth/`·`settings/`·알 수 없는 파일)는 보고서 `unsupported[]`에 빠짐없이 남긴다. 비밀값은 `secret://<device id>` 참조로만 쓰고 값·사용자 정보는 출력·보고서에 옮기지 않는다. 목록이 아닌 값·mapping이 아닌 항목·읽지 않는 최상위 키도 보고하고(조용히 버리지 않음), legacy 값은 pydantic lax 규칙(`"true"`·`1`·`"502"` 등)으로 읽는다. 종료 코드 0 = 미지원 없음, 2 = 변환했지만 미지원 있음(argparse 사용법 오류도 2 — 미지원이면 stdout에 `converted:` 줄이 있다), 1 = 읽을 수 없는 입력·비어 있지 않은 `--out`(출력은 검증한 뒤에만 쓰므로 이때 쓴 것 없음). 매핑 표·보고서 형식은 [data-contracts](../../docs/wiki/sot/data-contracts.md)의 `V1 구현 현황 (M5-1 legacy 변환기)` 절. 예시는 `fixtures/legacy-mini/` → `fixtures/legacy-mini-converted/`.

## 트리

`project.yaml`, `devices/<id>.yaml`, `tags/<device>.yaml`, `alarms/*.yaml`. `screens/`·`assets/`는 예약(비어 있어야 함, `.gitkeep`만 허용). 그 밖의 파일(README, `.github/` 등)은 프로젝트 내용이 아니라 무시한다. 예시는 `fixtures/acme/`.

## 정규형

- YAML 1.2 strict 로더: 중복 키·anchor/alias·명시적 tag·문자열 아닌 키·JSON 밖의 값(timestamp, NaN, ±2^53 초과 정수)을 거부한다.
- 출력: 키 정렬, 2칸 block YAML, 숫자는 RFC 8785(JCS) 표현, 문자열 UTF-8 NFC, LF, 스키마 기본값과 같은 필드 생략. 다시 읽어 같은 문자열이 되는 경우에만 따옴표를 생략한다(YAML 1.1 도구가 bool로 읽는 `yes`·`on` 등은 따옴표).
- 파일 경로는 POSIX 상대 경로로 다룬다(Windows 경로 입력은 정규화, 절대 경로·`..` 거부).

## content hash

`sha256:` + sha256(JCS(`[[posix_relpath, 정규 데이터], …]`, 경로 UTF-8 바이트 순)). 정규 데이터는 NFC·기본값 생략을 적용한 값이라 서식·키 순서·줄바꿈·기본값 명시 여부가 달라도 내용이 같으면 해시가 같다. 버전 이름은 고객 repo 태그 `vN`이다.

## 범위 밖(이후 단계)

- 드래프트 회수(`absorb`)는 **M5**(M5-3).
- 고객 repo용 재사용 workflow(`validate-project.yml`)도 **M5**.

`src/pureiron_project/schema/project.schema.json`은 계약 정본의 복사본(생성물)이다 — `bash scripts/g1.sh contracts-gen`으로 갱신하고 `contracts-drift`가 동기를 검사한다.

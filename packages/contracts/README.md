# packages/contracts — 계약 정본

현장·중앙 서비스 사이의 계약이다. 코드가 아니라 **이 파일들이 정본**이고(불변 규칙 "계약 우선"), 생성물은 여기서 만든다.

| 계약 | 파일 | 소비자 | 호환성 검사(contracts-diff) |
|---|---|---|---|
| collector ↔ core gRPC | `collector.proto` | core(서버), collector(클라이언트) | `buf breaking`(FILE) |
| core HTTP API | `openapi/core.json` | web(`src/api/schema.d.ts`), ai(예정) | `oasdiff breaking --fail-on ERR` |
| control-api HTTP API | `openapi/control-api.json` | control-web(M4), 현장 Updater(M2) | `oasdiff breaking --fail-on ERR` |
| 고객 프로젝트 | `project.schema.json` | project-tools, core 프로젝트 적용 | `tools/schema_diff.py` |
| 번들 manifest | `manifest.schema.json` | bundle-tools(M2) | `tools/schema_diff.py` |
| fleet 현장 파일 | `fleet-site.schema.json` | pureiron-fleet CI(M4) | `tools/schema_diff.py` |

## 생성 (generated ≠ authored, ADR-028)

```bash
bash scripts/g1.sh contracts-gen     # OpenAPI export + proto 생성 + web schema.d.ts
bash scripts/g1.sh contracts-drift   # 재생성 후 git diff 0 확인
bash scripts/g1.sh contracts-diff    # origin/master 대비 비호환 변경 검사
```

- **OpenAPI**는 코드가 원천이다: `tools/export_openapi.py`가 core·control-api 앱에서 결정적으로 뽑아 `openapi/`에 쓴다. API를 바꾸면 export 결과를 같은 PR에 커밋한다.
- **proto 생성**은 uv.lock으로 고정된 `grpcio-tools`(protoc 내장) + `mypy-protobuf`로 한다(`tools/gen_proto.py`). 네트워크 없이 같은 입력에서 같은 바이트를 내고 grpc 런타임과 버전이 함께 잠긴다. `buf`는 lint·breaking 전용이다(`scripts/tools.sh`가 버전·sha256 고정 설치).
- 생성 `_gen/`의 `.py`·`.pyi`는 pyright·ruff 진단에서 빠진다. 타입은 `.pyi`를 정본으로 authored 코드에서 strict 소비한다.
- `buf.yaml` lint 예외는 plan §6.1 이름(`Acquisition`, `Quality.GOOD`, `WriteResult.Status.REJECTED=0`)을 지키기 위한 것이며 이유를 파일에 적어 두었다.

## bootstrap 규칙

`origin/master`에 해당 계약 파일이 아직 없으면(첫 도입 PR) breaking 검사는 notice를 남기고 통과한다. drift 검사는 항상 적용한다.

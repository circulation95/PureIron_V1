---
id: archive-manual-legacy
title: (Archive) Legacy User Manual 포인터
status: archive
audience: human
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/MANUAL.md
last_verified: 2026-06-07
---

# (Archive) Legacy User Manual

원본 사용자 매뉴얼(`MANUAL.md`, 약 1,760줄, 한국어)은 **삭제하지 않고 legacy archive로 유지**한다. 이 문서는 포인터 + 주제→SOT 추출 맵이며 본문을 복사하지 않는다. `status: archive`이므로 `llms-full.md` 생성 입력에서 제외된다.

## 원본

- [MANUAL.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/MANUAL.md) — 17개 섹션 전체 사용자 매뉴얼

## 주제 → SOT 추출 맵

MANUAL.md의 운영 지식 중 정본화가 필요한 부분은 아래 SOT 문서로 요약 이전됐다. 세부 사용법(스크린샷 수준 절차)은 MANUAL.md를 직접 본다.

| MANUAL.md 섹션 | 요약 이전처(SOT) |
|---|---|
| 1. 프로젝트 소개 | [product](../sot/product.md) |
| 2. 시작하기(환경 설정) | [README.md](../../../README.md), [runtime-config](../sot/runtime-config.md) |
| 3. 태그 추가·화면 매핑 | [data-contracts](../sot/data-contracts.md), [frontend](../sot/frontend.md) |
| 4. 알람 설정 | [api-events](../sot/api-events.md), [architecture](../sot/architecture.md) |
| 5. 히스토리 설정·조회 | [api-events](../sot/api-events.md) |
| 6. OPC UA 통신 흐름 | [architecture](../sot/architecture.md), [data-contracts](../sot/data-contracts.md) |
| 7. 알람 실시간 Summary | [architecture](../sot/architecture.md) |
| 8. 실시간 데이터 흐름(WebSocket) | [api-events](../sot/api-events.md), [architecture](../sot/architecture.md) |
| 9. UDT | [data-contracts](../sot/data-contracts.md), [architecture](../sot/architecture.md) |
| 10. AI 멀티에이전트 챗봇 | [product](../sot/product.md), [api-events](../sot/api-events.md), [frontend](../sot/frontend.md) |
| 11. MES | [product](../sot/product.md), [api-events](../sot/api-events.md) |
| 12. RAG 문서 관리 | [product](../sot/product.md), [api-events](../sot/api-events.md) |
| 13. 공정 모니터링 화면 | [frontend](../sot/frontend.md) |
| 14. 시스템 로그·환경 설정 | [runtime-config](../sot/runtime-config.md), [api-events](../sot/api-events.md) |
| 15. REST API 레퍼런스 | [api-events](../sot/api-events.md) (정본은 `/docs`) |
| 16. 주요 파일 가이드 | [architecture](../sot/architecture.md) |
| 17. FAQ | (MANUAL.md 유지) |

## 상태

- MANUAL.md는 운영자용 상세 가이드로 잔존한다. 향후(v2.3) 필요 시 주제별 별도 가이드로 분할 검토.

---
name: llm-wiki-maintainer
description: PureIron 문서 SOT(`docs/wiki/`) 작업 시 사용한다. "Wiki/SOT 문서 정리", "LLM_WIKI 갱신", "docs/wiki 수정", "문서 저장/불러오기", "env/API drift 확인", "MANUAL/README/AGENTS/CLAUDE 정리", "Harness 컨텍스트 문서 갱신" 같은 요청에서 load→save→build→check 절차를 강제한다. 자동화 본체는 `scripts/wiki.py`이며 이 skill은 올바른 절차만 강제한다.
---

# LLM Wiki Maintainer

PureIron의 문서 SOT는 `docs/wiki/`이고, 생성·검증 본체는 `scripts/wiki.py`다. 이 skill은
LLM이 문서/SOT 작업을 할 때 **load → save → build → check** 절차를 따르게 한다.
`/wiki` 명령(`.claude/commands/wiki.md`)도 이 절차를 가리킨다.

## Load mode (불러오기)

1. `docs/LLM_WIKI.md`를 **먼저** 읽는다 (compact projection).
2. 더 필요한 정보가 있으면 **섹션 단위로** 찾는다. SOT 문서는 대부분 10KB 이상이라 전문 Read는 컨텍스트 낭비다.
   - `Grep(pattern=<키워드>, path="docs/wiki/_generated/section-index.tsv")` → 매칭 줄만 반환.
   - 줄 형식: `doc_id · path · anchor · offset · limit · bytes · level · heading` (탭 구분).
   - `Read(path, offset=<offset>, limit=<limit>)`로 **그 절만** 읽는다.
   - 문서 경로/메타데이터만 필요하면 `docs/wiki/_generated/manifest.json`을 본다.
3. **대상 절만** 연다. `MANUAL.md`처럼 큰 원본과 `_generated/llms-full.md`(약 180KB, 외부 ingest 전용)는 열지 않는다.

## Save mode (수정/저장)

1. **SOT 내용**은 `docs/wiki/**`의 canonical 문서에만 저장한다.
2. **투영 문서**(`AGENTS.md`, `CLAUDE.md`, `.claude/commands/wiki.md` 등)는 운영 배선용
   **짧은 포인터만** 수정한다. 사용자가 `README.md`/`AGENTS.md` 자체 수정을 명시 요청하면
   그 요청 범위 안에서만 고친다.
3. 수정 후 반드시:
   ```bash
   python scripts/wiki.py build
   python scripts/wiki.py check
   ```
4. `check`의 drift 경고(env/API/secret)는 **이번 수정 대상이 아니면 보고만** 하고 손대지 않는다.

## 금지사항

- `docs/wiki/_generated/**`, `docs/LLM_WIKI.md`를 **직접 손으로 고치지 않는다** — 항상 `build`로만 재생성.
- secret 값(API 키·토큰 등)을 Wiki 본문에 복사하지 않는다 — drift/security 경고로만 둔다.
- frontmatter `sources`에는 **실재 파일/디렉터리 경로만** 쓴다. glob 패턴은 `source_globs`에만 쓴다.
- `check`를 통과시키려고 소스 코드(`config.py`, 라우터 등)를 임의로 바꾸지 않는다 — drift는 경고일 뿐이다.

## 검증

- `python scripts/wiki.py check` → 구조 오류 0(frontmatter/link/sources/stale), drift는 경고만.
- 재생성물(`docs/LLM_WIKI.md`, `docs/wiki/_generated/*`)이 함께 변경됐는지 `git status`로 확인하고 같이 커밋한다.

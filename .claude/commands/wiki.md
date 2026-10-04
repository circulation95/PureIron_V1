이 프로젝트의 문서 SOT(Source of Truth)는 `docs/wiki/`이고, 생성·검증 본체는 `scripts/wiki.py`다. `/wiki` 워크플로우는 문서/SOT 작업 시 **load → save → build → check** 절차를 강제한다. 절차의 정본은 `skills/llm-wiki-maintainer` skill이며, 이 명령은 그 절차를 가리키는 진입점이다(중복 정의 아님).

## /wiki load <topic>

1. `docs/LLM_WIKI.md`를 **먼저** 읽는다(compact projection).
2. 더 필요하면 `docs/wiki/_generated/section-index.tsv`를 **grep** 해서 필요한 절의 `path`/`offset`/`limit`을 얻고, `Read(path, offset, limit)`으로 **그 절만** 읽는다. 문서 경로만 필요하면 `manifest.json`을 본다.
3. **대상 문서만** 연다. `MANUAL.md`처럼 큰 원본과 `_generated/llms-full.md`는 열지 않고 필요한 SOT 절을 경유한다.

## /wiki save <topic>

1. **SOT 내용**은 `docs/wiki/**`의 canonical 문서에만 저장한다.
2. **투영 문서**(`AGENTS.md`, `CLAUDE.md`, `README.md`, 이 명령 파일)는 사용자가 명시 요청한 범위에서만 짧게 고친다.
3. 수정 후 반드시:
   ```bash
   python scripts/wiki.py build
   python scripts/wiki.py check
   ```
4. `check`의 drift 경고(env/API/secret)는 이번 수정 대상이 아니면 **보고만** 한다.

## /wiki check

```bash
python scripts/wiki.py check
```
구조 오류(frontmatter/link/sources/stale) 0이면 exit 0. drift는 경고만.

## /wiki inventory

```bash
python scripts/wiki.py inventory
```
문서 후보·제외 목록을 분류해 출력(비파괴).

## 금지

- `docs/wiki/_generated/**`, `docs/LLM_WIKI.md` 직접 수정 금지 — 항상 `build`로만 재생성.
- secret 값(API 키·토큰)을 위키 본문에 복사 금지.
- frontmatter `sources`에는 실재 경로만, glob은 `source_globs`에만.
- `check`를 통과시키려고 소스 코드를 임의로 바꾸지 않는다 — drift는 경고일 뿐이다.
- 재생성물(`docs/LLM_WIKI.md`, `docs/wiki/_generated/*`)은 변경분과 함께 커밋한다.

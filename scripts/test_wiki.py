"""wiki.py Stage 2 안전망 테스트.

- agent-rules `## 불변 규칙` 파싱 + fallback
- agent-rules 편집이 LLM_WIKI 투영에 반영
- agent-context.md 가 생성물(generate_outputs/stale)에 포함
- manifest `outputs` 필드
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import wiki  # noqa: E402


def make_doc(rel: str, meta: dict, body: str) -> wiki.WikiDoc:
    """ROOT 상대 경로의 합성 WikiDoc (디스크 미접근)."""
    path = wiki.ROOT / rel
    raw = ("synthetic:" + rel + "\n" + body).encode("utf-8")
    return wiki.WikiDoc(path, meta, body, raw)


def agent_rules_doc(body: str) -> wiki.WikiDoc:
    return make_doc("docs/wiki/sot/agent-rules.md", {"id": "sot-agent-rules"}, body)


AGENT_BODY = """# Agent Rules SOT

intro line.

## 불변 규칙

- No `app.state` — gateway.* 경유
- No ORM — asyncpg raw SQL
- UNIQUE-MARKER-RULE — 테스트 전용 한 줄

> 이 bullet 은 build 가 파싱한다.

## 위키 규율

- 이건 다른 섹션 bullet (수집 금지)
"""


# --------------------------------------------------------------------------- #
# agent_invariants
# --------------------------------------------------------------------------- #
def test_agent_invariants_parses_only_invariant_section():
    docs = [agent_rules_doc(AGENT_BODY)]
    inv = wiki.agent_invariants(docs)
    assert "- No `app.state` — gateway.* 경유" in inv
    assert any("UNIQUE-MARKER-RULE" in line for line in inv)
    # 다음 섹션(## 위키 규율)의 bullet 은 수집하지 않는다
    assert not any("다른 섹션 bullet" in line for line in inv)
    # blockquote(>)는 bullet 이 아니므로 제외
    assert not any("파싱한다" in line for line in inv)


def test_agent_invariants_fallback_when_section_missing():
    docs = [agent_rules_doc("# Agent Rules\n\n본문에 불변 규칙 섹션이 없다.\n")]
    assert wiki.agent_invariants(docs) == wiki.HARDCODED_INVARIANTS


def test_agent_invariants_fallback_when_doc_missing():
    assert wiki.agent_invariants([]) == wiki.HARDCODED_INVARIANTS


# --------------------------------------------------------------------------- #
# build_llm_wiki reflects edits
# --------------------------------------------------------------------------- #
def test_build_llm_wiki_reflects_agent_rules_edit():
    out = wiki.build_llm_wiki([agent_rules_doc(AGENT_BODY)])
    assert "## 핵심 불변 규칙" in out
    assert "UNIQUE-MARKER-RULE" in out  # 편집 반영(v1.5 #4)


def test_build_llm_wiki_uses_fallback_without_agent_rules():
    out = wiki.build_llm_wiki([])
    # 하드코딩 fallback 의 한 항목이 그대로 노출
    assert "No `app.state`" in out


# --------------------------------------------------------------------------- #
# agent-context generation
# --------------------------------------------------------------------------- #
def test_build_agent_context_projects_body():
    out = wiki.build_agent_context([agent_rules_doc(AGENT_BODY)])
    assert "generated projection" in out
    assert "docs/wiki/sot/agent-rules.md" in out
    assert "UNIQUE-MARKER-RULE" in out


def test_generate_outputs_includes_agent_context():
    outputs = wiki.generate_outputs([agent_rules_doc(AGENT_BODY)])
    assert wiki.GEN_AGENT_CONTEXT in outputs
    assert wiki.GEN_AGENT_CONTEXT in wiki.GEN_OUTPUT_PATHS


# --------------------------------------------------------------------------- #
# manifest outputs field
# --------------------------------------------------------------------------- #
def test_manifest_has_outputs_field():
    import json

    doc = make_doc(
        "docs/wiki/sot/agent-rules.md",
        {
            "id": "sot-agent-rules",
            "title": "Agent Rules",
            "status": "canonical",
            "audience": "agent",
            "owners": ["x"],
            "sources": ["AGENTS.md"],
            "last_verified": "2026-06-07",
        },
        AGENT_BODY,
    )
    manifest = json.loads(wiki.build_manifest([doc]))
    assert "outputs" in manifest
    assert wiki.GEN_AGENT_CONTEXT in manifest["outputs"]
    # manifest 자신은 outputs 에 포함하지 않는다(자기참조 방지)
    assert wiki.GEN_MANIFEST not in manifest["outputs"]
    # docs[] 에는 canonical wiki 문서만(생성물 제외)
    ids = [d["id"] for d in manifest["docs"]]
    assert ids == ["sot-agent-rules"]


# --------------------------------------------------------------------------- #
# migration prefix duplicates (warn-only)
# --------------------------------------------------------------------------- #
def _write_migrations(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("-- migration\n", encoding="utf-8")


def test_migration_prefix_duplicates_allowlist_passes(tmp_path, monkeypatch):
    mig = tmp_path / "migrations"
    _write_migrations(
        mig,
        [
            "013_alarm_per_tag_uniqueness.sql",
            "013_create_udt_tables.sql",
            "025_add_tag_source_and_history_tagid.sql",
            "025_demo_device_status_intent.sql",
            "026_cleanup_stale_udt_derived.sql",
        ],
    )
    monkeypatch.setattr(wiki, "MIGRATIONS_DIR", mig)
    # 기존 중복(013, 025)은 allowlist 로 통과 → 경고 없음
    assert wiki.migration_prefix_duplicates() == []


def test_migration_prefix_duplicates_flags_new_dup(tmp_path, monkeypatch):
    mig = tmp_path / "migrations"
    _write_migrations(mig, ["027_first.sql", "027_second.sql", "028_solo.sql"])
    monkeypatch.setattr(wiki, "MIGRATIONS_DIR", mig)
    out = wiki.migration_prefix_duplicates()
    assert len(out) == 1
    assert "027" in out[0]
    assert "027_first.sql" in out[0] and "027_second.sql" in out[0]


def test_migration_prefix_duplicates_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "MIGRATIONS_DIR", tmp_path / "does_not_exist")
    assert wiki.migration_prefix_duplicates() == []


# --------------------------------------------------------------------------- #
# Stage 3: 섹션 인덱스 / anchor / 구획
# --------------------------------------------------------------------------- #
def real_doc(rel: str, text: str) -> wiki.WikiDoc:
    """frontmatter 포함 전문에서 만든 WikiDoc.

    `raw_bytes` 와 `body`/`body_start_line` 이 서로 일치하므로 절대 줄 좌표를
    검사하는 테스트에 쓸 수 있다(`make_doc` 은 raw 가 합성이라 좌표가 어긋난다).
    """
    meta, body, start = wiki.split_frontmatter(text)
    return wiki.WikiDoc(wiki.ROOT / rel, meta, body, text.encode("utf-8"), start)


FENCED_DOC = """---
id: sot-fenced
title: T
---

# Top

## Real Section

```bash
# NOT a heading
## also not a heading
```

### Sub

## Second
"""


def test_slugify_matches_github_space_handling():
    # 구두점이 지워져 공백이 2개 남으면 하이픈도 2개다. 이걸 하나로 합치면
    # `#가격--패키징` 같은 기존 정상 링크가 전부 깨진 것으로 오판된다.
    assert wiki.slugify_heading("가격 / 패키징") == "가격--패키징"
    assert wiki.slugify_heading("PoC 기간 / 투입") == "poc-기간--투입"
    assert wiki.slugify_heading("4. 변경 유형별 최소 게이트") == "4-변경-유형별-최소-게이트"
    assert wiki.slugify_heading("Historian retention / query window") == (
        "historian-retention--query-window"
    )


def test_extract_sections_ignores_headings_inside_code_fences():
    doc = real_doc("docs/wiki/sot/fenced.md", FENCED_DOC)
    assert [s["title"] for s in doc.sections()] == [
        "Top",
        "Real Section",
        "Sub",
        "Second",
    ]


def test_section_offsets_point_at_the_real_heading_line():
    doc = real_doc("docs/wiki/sot/fenced.md", FENCED_DOC)
    lines = FENCED_DOC.splitlines()
    for sec in doc.sections():
        assert sec["title"] in lines[int(sec["line_start"]) - 1]


def test_section_range_covers_subsections_until_same_level():
    doc = real_doc("docs/wiki/sot/fenced.md", FENCED_DOC)
    real = next(s for s in doc.sections() if s["title"] == "Real Section")
    second = next(s for s in doc.sections() if s["title"] == "Second")
    sub = next(s for s in doc.sections() if s["title"] == "Sub")
    # `##` 를 읽으면 하위 `###` 가 함께 들어오고, 다음 `##` 직전에서 끝난다.
    assert real["line_start"] < sub["line_start"] <= real["line_end"]
    assert real["line_end"] == second["line_start"] - 1
    assert real["line_count"] == real["line_end"] - real["line_start"] + 1


def test_duplicate_headings_get_suffixed_anchors():
    doc = real_doc("docs/wiki/sot/dup.md", "---\nid: x\n---\n\n## Dup\n\n## Dup\n")
    assert [s["anchor"] for s in doc.sections()] == ["dup", "dup-1"]


def test_check_anchors_flags_missing_fragment():
    target = real_doc("docs/wiki/sot/target.md", "---\nid: t\n---\n\n## Real\n")
    src = real_doc(
        "docs/wiki/sot/src.md",
        "---\nid: s\n---\n\n[ok](target.md#real) [bad](target.md#nope)\n",
    )
    errors = wiki.check_anchors([target, src])
    assert len(errors) == 1
    assert "target.md#nope" in errors[0]


def test_check_anchors_ignores_external_and_non_wiki_links():
    src = real_doc(
        "docs/wiki/sot/src.md",
        "---\nid: s\n---\n\n"
        "[url](https://x.dev#frag) [outside](../../MANUAL.md#anything)\n",
    )
    assert wiki.check_anchors([src]) == []


def test_check_section_offsets_catches_off_by_one():
    doc = real_doc("docs/wiki/sot/fenced.md", FENCED_DOC)
    assert wiki.check_section_offsets([doc]) == []
    for sec in doc.sections():  # 캐시된 dict 를 직접 밀어 좌표를 어긋나게 한다
        sec["line_start"] = int(sec["line_start"]) + 1
    assert wiki.check_section_offsets([doc])


def test_section_of_splits_by_document_lifetime():
    assert wiki._section_of("docs/wiki/sot/testing.md") == "SOT"
    assert wiki._section_of("docs/wiki/gtm/first-contract.md") == "GTM"
    assert wiki._section_of("docs/wiki/reports/mvp-gap.md") == "Reports"
    assert wiki._section_of("docs/wiki/adr/index.md") == "ADR"
    assert wiki._section_of("docs/wiki/archive/x.md") == "Archive"


def test_stale_report_warning_only_for_old_reports():
    import datetime

    fresh = datetime.date.today().isoformat()
    old = real_doc(
        "docs/wiki/reports/old.md", "---\nid: r\nlast_verified: 2000-01-01\n---\n\n# R\n"
    )
    new = real_doc(
        "docs/wiki/reports/new.md", f"---\nid: r2\nlast_verified: {fresh}\n---\n\n# R\n"
    )
    # sot/ 는 시점 문서가 아니므로 오래돼도 경고 대상이 아니다.
    sot = real_doc(
        "docs/wiki/sot/x.md", "---\nid: s\nlast_verified: 2000-01-01\n---\n\n# S\n"
    )
    warns = wiki._stale_report_warnings([old, new, sot])
    assert len(warns) == 1
    assert "docs/wiki/reports/old.md" in warns[0]


def test_rebase_relative_links_for_projection():
    src_dir = wiki.ROOT / "docs" / "wiki" / "sot"
    dst = wiki.ROOT / "docs" / "LLM_WIKI.md"
    out = wiki._rebase_relative_links(
        "[a](architecture.md) [b](../adr/index.md#adr-004) "
        "[u](https://x.dev) [f](#local)",
        src_dir,
        dst,
    )
    assert "(wiki/sot/architecture.md)" in out
    assert "(wiki/adr/index.md#adr-004)" in out
    assert "(https://x.dev)" in out  # URL 은 건드리지 않는다
    assert "(#local)" in out  # 문서 내 anchor 도 그대로


def test_build_section_index_is_one_grep_line_per_section():
    doc = real_doc("docs/wiki/sot/fenced.md", FENCED_DOC)
    text = wiki.build_section_index([doc])
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(rows) == len(doc.sections())
    for row, sec in zip(rows, doc.sections()):
        cols = row.split("\t")
        assert len(cols) == 8, cols
        assert cols[1] == "docs/wiki/sot/fenced.md"
        assert cols[3] == str(sec["line_start"])  # Read(offset=...) 인자
        assert cols[4] == str(sec["line_count"])  # Read(limit=...) 인자


def test_generate_outputs_includes_section_index():
    outputs = wiki.generate_outputs([])
    assert wiki.GEN_SECTION_INDEX in outputs
    assert wiki.GEN_SECTION_INDEX in wiki.GEN_OUTPUT_PATHS


def test_check_sources_accepts_legacy_permalink_only():
    legacy = "https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/x.py"
    other = "https://github.com/circulation95/PureIron_ignition/blob/master/backend/x.py"
    doc = make_doc("docs/wiki/sot/s.md", {"sources": [legacy, other, "no/such/path"]}, "")
    errors = wiki.check_sources([doc])
    assert len(errors) == 2
    assert not any(legacy in e for e in errors)
    assert any(other in e for e in errors)

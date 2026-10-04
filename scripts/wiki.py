#!/usr/bin/env python3
"""PureIron Wiki tooling — inventory / build / check.

Git 기반 LLM Wiki SOT 시스템의 stdlib-only 도구. 외부 패키지 의존 없음(Python 3.12).

- inventory : 문서 후보·제외 목록을 분류해 출력(비파괴).
- build     : docs/wiki/_generated/* 와 docs/LLM_WIKI.md 생성.
- check     : 구조 검증(비파괴, 파일 미생성). 구조 오류는 exit 1, drift는 경고만.

불변 규칙:
- check 는 절대 파일을 쓰지 않는다(드리프트 리포트 생성은 build 전용).
- _generated/** 는 llms-full.md 입력에서 제외한다.
- sources 는 실재 파일/디렉터리만, glob 은 source_globs 로 분리한다.
- API drift 는 v1 에서 warn-only incomplete heuristic 이다.
- section-index.tsv 는 grep 대상이므로 한 섹션 = 정확히 한 줄, 탭 구분을 유지한다.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
WIKI = DOCS / "wiki"
GEN = WIKI / "_generated"
LLM_WIKI = DOCS / "LLM_WIKI.md"

REQUIRED_FIELDS = ("id", "title", "status", "audience", "owners", "sources", "last_verified")
LIST_FIELDS = ("owners", "sources", "source_globs")
STATUS_VALUES = ("canonical", "generated", "archive")
AUDIENCE_VALUES = ("human", "agent", "both")

# legacy 코드는 이 repo에 없다(Foundation M0). legacy 근거는 고정 태그 permalink로만 `sources`에 남긴다.
# 이 prefix로 시작하는 source는 로컬 경로 검사 대신 형식만 확인한다(네트워크 미접근).
LEGACY_PERMALINK_PREFIXES = (
    "https://github.com/circulation95/PureIron_ignition/blob/legacy-final/",
    "https://github.com/circulation95/PureIron_ignition/tree/legacy-final/",
)

# 생성물의 repo-relative 경로 (build 출력 = check 비교 대상)
GEN_MANIFEST = "docs/wiki/_generated/manifest.json"
GEN_LLMS_TXT = "docs/wiki/_generated/llms.txt"
GEN_LLMS_FULL = "docs/wiki/_generated/llms-full.md"
GEN_DRIFT = "docs/wiki/_generated/sot-drift-report.md"
GEN_AGENT_CONTEXT = "docs/wiki/_generated/agent-context.md"
GEN_SECTION_INDEX = "docs/wiki/_generated/section-index.tsv"
GEN_LLM_WIKI = "docs/LLM_WIKI.md"

# manifest 의 `outputs` 필드 (생성물 경로 — manifest 자신은 제외). 정렬로 결정적 유지.
GEN_OUTPUT_PATHS = sorted(
    [
        GEN_LLMS_TXT,
        GEN_LLMS_FULL,
        GEN_DRIFT,
        GEN_AGENT_CONTEXT,
        GEN_SECTION_INDEX,
        GEN_LLM_WIKI,
    ]
)

# agent-rules.md 의 `## 불변 규칙` 파싱 실패 시 사용하는 fallback (절대 빈 섹션 방출 금지).
HARDCODED_INVARIANTS = [
    "- No ORM — asyncpg raw SQL",
    "- 단일 조립 지점 — core는 `SiteRuntime.build()` + 생성자 주입, 전역 조회·`app.state` 금지",
    "- 마이그레이션 — `pureiron-core migrate` ADD-only, 부팅 시 자동 실행 금지",
    "- 서명 번들 — 현장 변경은 서명된 `.pib`로만",
    "- 엄격 게이트 — 억제 주석 0건, 생성 코드는 검사 제외 + drift 게이트",
]

# inventory 스캔 제외 (캐시/로그/출력물)
EXCLUDE_PARTS = ("__pycache__", "node_modules", ".omc", ".git", "traces", "logs")
EXCLUDE_SUFFIX = (".bak", ".tmp")

# migration prefix 중복 경고 (warn-only). 아래 prefix 는 알려진 기존 중복으로
# 통과시키고, 새로 생긴 중복만 경고한다. 정본은 data-contracts SOT 의 마이그레이션 규율.
MIGRATIONS_DIR = ROOT / "backend" / "src" / "pureiron" / "db" / "migrations"
MIGRATION_DUP_ALLOWLIST = frozenset({"013", "025"})

# secret 본문 스캔 패턴 (wiki/generated markdown 한정)
SECRET_BODY_PATTERNS = (
    re.compile(r"lsv2_[A-Za-z0-9_]{12,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


# --------------------------------------------------------------------------- #
# Frontmatter mini-parser (stdlib only — no pyyaml)
# --------------------------------------------------------------------------- #
class FrontmatterError(Exception):
    """frontmatter 파싱 실패."""


def split_frontmatter(text: str) -> tuple[dict[str, object], str, int]:
    """`---` 펜스 frontmatter 와 본문을 분리한다.

    지원: `key: value`, 인라인 리스트 `[a, b]`, 블록 리스트(`- item`).

    반환: `(meta, body, body_start_line)`. `body_start_line` 은 본문 첫 줄의
    **파일 전체 기준 1-indexed 줄 번호**로, 섹션 인덱스가 `Read(offset=...)` 에
    바로 쓸 수 있는 절대 좌표를 만들기 위해 필요하다.
    """
    if not text.startswith("---"):
        raise FrontmatterError("frontmatter 펜스(`---`) 없음")
    lines = text.splitlines()
    # 첫 줄은 '---'. 닫는 '---' 탐색.
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError("frontmatter 닫는 `---` 없음")

    meta: dict[str, object] = {}
    current_key: str | None = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        list_item = re.match(r"^\s+-\s+(.*)$", raw)
        if list_item and current_key is not None:
            meta.setdefault(current_key, [])
            target = meta[current_key]
            if isinstance(target, list):
                target.append(_strip_scalar(list_item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if not kv:
            raise FrontmatterError(f"frontmatter 파싱 불가: {raw!r}")
        key, rest = kv.group(1), kv.group(2).strip()
        current_key = key
        if rest == "":
            meta[key] = []  # 뒤따르는 블록 리스트 대기
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            meta[key] = (
                [_strip_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
            )
            current_key = None
        else:
            meta[key] = _strip_scalar(rest)
            current_key = None

    # `lstrip("\n")` 로 잘려나가는 선행 빈 줄 수를 세어 본문의 절대 줄 번호를 맞춘다.
    rest = lines[end + 1 :]
    leading_blanks = 0
    for line in rest:
        if line != "":
            break
        leading_blanks += 1
    body = "\n".join(rest).lstrip("\n")
    body_start_line = end + 1 + leading_blanks + 1  # 0-indexed → 1-indexed
    return meta, body, body_start_line


def _strip_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# --------------------------------------------------------------------------- #
# Section extraction (GitHub-style anchors + absolute line ranges)
# --------------------------------------------------------------------------- #
# 코드펜스 안의 `# 주석` 을 heading 으로 오인하면 안 된다. testing.md 의 bash 블록에는
# `# Backend 전체 테스트` 같은 줄이 있고, adr/index.md 의 템플릿 블록에는
# `## ADR-XXX: 제목` 이 있다. 둘 다 섹션이 아니다.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# GitHub anchor 규칙: 소문자화 → 영숫자/공백/하이픈/언더스코어 외 제거 → 공백을 하이픈으로.
# `\w` 는 re.UNICODE 기본이라 한글을 보존한다(GitHub 동작과 일치).
_ANCHOR_DROP_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def slugify_heading(text: str) -> str:
    """heading 텍스트 → GitHub 스타일 anchor.

    github-slugger 와 동작을 맞춘다: 소문자화 → 구두점 제거 → **공백 하나당 하이픈
    하나**. 공백 연속을 하이픈 하나로 합치지 않는 것이 중요하다. `가격 / 패키징` 은
    `/` 가 지워져 공백 2개가 남으므로 anchor 가 `가격--패키징`(하이픈 2개)이 된다.
    합쳐 버리면 기존 문서의 정상 링크가 모두 깨진 것으로 오판된다.
    """
    slug = _ANCHOR_DROP_RE.sub("", text.strip().lower())
    return re.sub(r"\s", "-", slug)


def extract_sections(body: str, body_start_line: int) -> list[dict[str, object]]:
    """본문에서 섹션 목록을 뽑는다.

    각 섹션의 범위는 `자기 heading 줄 ~ 다음 동급/상위 heading 직전`이라서,
    `##` 섹션을 읽으면 그 아래 `###` 하위 절이 함께 들어온다. 줄 번호는 파일 전체
    기준 1-indexed 라 `Read(offset=start, limit=line_count)` 에 그대로 쓸 수 있다.
    """
    lines = body.splitlines()
    heads: list[tuple[int, int, str]] = []  # (0-indexed 본문 줄, level, title)
    fence_char: str | None = None
    fence_len = 0
    for i, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            token = fence.group(1)
            char, length = token[0], len(token)
            if fence_char is None:
                fence_char, fence_len = char, length
            elif char == fence_char and length >= fence_len:
                fence_char, fence_len = None, 0
            continue
        if fence_char is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            heads.append((i, len(heading.group(1)), heading.group(2).strip()))

    # 줄별 바이트 수(개행 포함) 누적 — 섹션 읽기 비용 표시에 쓴다.
    line_bytes = [len(line.encode("utf-8")) + 1 for line in lines]

    used_anchors: dict[str, int] = {}
    sections: list[dict[str, object]] = []
    for idx, (line_idx, level, title) in enumerate(heads):
        end_idx = len(lines) - 1
        for next_idx, next_level, _ in heads[idx + 1 :]:
            if next_level <= level:
                end_idx = next_idx - 1
                break

        base = slugify_heading(title) or f"section-{idx + 1}"
        seen = used_anchors.get(base, 0)
        anchor = base if seen == 0 else f"{base}-{seen}"
        used_anchors[base] = seen + 1

        start_line = body_start_line + line_idx
        end_line = body_start_line + end_idx
        sections.append(
            {
                "anchor": anchor,
                "title": title,
                "level": level,
                "line_start": start_line,
                "line_end": end_line,
                "line_count": end_line - start_line + 1,
                "bytes": sum(line_bytes[line_idx : end_idx + 1]),
            }
        )
    return sections


# --------------------------------------------------------------------------- #
# Document loading
# --------------------------------------------------------------------------- #
class WikiDoc:
    """검증·생성에 쓰이는 위키 문서 표현."""

    def __init__(
        self,
        path: Path,
        meta: dict[str, object],
        body: str,
        raw_bytes: bytes,
        body_start_line: int = 1,
    ):
        self.path = path
        self.rel = path.relative_to(ROOT).as_posix()
        self.meta = meta
        self.body = body
        self.raw_bytes = raw_bytes
        # 본문 첫 줄의 파일 전체 기준 1-indexed 줄 번호 (섹션 인덱스 좌표 기준점).
        self.body_start_line = body_start_line

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.path.stem))

    def list_field(self, key: str) -> list[str]:
        val = self.meta.get(key, [])
        return [str(x) for x in val] if isinstance(val, list) else [str(val)]

    def summary(self) -> str:
        """본문 첫 의미 있는 한 줄 (heading/blockquote/comment 제외)."""
        for line in self.body.splitlines():
            s = line.strip()
            if not s or s.startswith(("#", ">", "<!--", "|", "-", "*", "```")):
                continue
            return s
        return self.title

    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()

    def byte_size(self) -> int:
        return len(self.raw_bytes)

    def sections(self) -> list[dict[str, object]]:
        """본문 heading 을 절대 줄 좌표 섹션으로 변환 (결과 캐시)."""
        cached = getattr(self, "_sections_cache", None)
        if cached is None:
            cached = extract_sections(self.body, self.body_start_line)
            self._sections_cache = cached
        return cached


def iter_wiki_doc_paths() -> list[Path]:
    """docs/wiki/**/*.md (단, _generated/** 제외)."""
    if not WIKI.is_dir():
        return []
    return sorted(
        p
        for p in WIKI.rglob("*.md")
        if "_generated" not in p.relative_to(WIKI).parts
    )


def load_docs() -> tuple[list[WikiDoc], list[str]]:
    """위키 문서 로드. (docs, hard_errors) 반환."""
    docs: list[WikiDoc] = []
    errors: list[str] = []
    for path in iter_wiki_doc_paths():
        rel = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"[utf8] {rel}: UTF-8 디코드 실패 ({exc})")
            continue
        try:
            meta, body, body_start = split_frontmatter(text)
        except FrontmatterError as exc:
            errors.append(f"[frontmatter] {rel}: {exc}")
            continue
        docs.append(WikiDoc(path, meta, body, raw, body_start))
    return docs, errors


# --------------------------------------------------------------------------- #
# Drift / source extraction (warn-only inputs)
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def config_env_vars() -> list[str]:
    """config.py Settings 필드 → PUREIRON_* 환경변수 이름."""
    text = _read_text(ROOT / "backend" / "src" / "pureiron" / "config.py")
    fields = re.findall(r"^    ([a-z_][a-z0-9_]*)\s*:", text, re.MULTILINE)
    seen: list[str] = []
    for f in fields:
        env = f"PUREIRON_{f.upper()}"
        if env not in seen:
            seen.append(env)
    return seen


def env_example_vars() -> list[str]:
    text = _read_text(ROOT / ".env.example")
    return sorted(set(re.findall(r"^(PUREIRON_[A-Z0-9_]+)=", text, re.MULTILINE)))


def secret_source_warnings() -> list[str]:
    """config.py 의 secret 처럼 보이는 비공란 기본값 (본문 복사 금지 대상)."""
    text = _read_text(ROOT / "backend" / "src" / "pureiron" / "config.py")
    out: list[str] = []
    pattern = re.compile(
        r"^    ([a-z_]*(?:secret|key|token|password)[a-z_]*)\s*:[^=]*=\s*[\"']([^\"']+)[\"']",
        re.MULTILINE,
    )
    for name, default in pattern.findall(text):
        if default.strip():
            out.append(f"{name} (기본값 비공란 — 위키 본문에 복사 금지)")
    return sorted(set(out))


def router_prefixes() -> list[str]:
    """*/router.py 의 APIRouter(prefix=...) 프리픽스."""
    base = ROOT / "backend" / "src" / "pureiron"
    found: set[str] = set()
    if base.is_dir():
        for router in base.glob("*/router.py"):
            m = re.search(
                r"APIRouter\(\s*prefix\s*=\s*[\"']([^\"']+)[\"']", _read_text(router)
            )
            if m:
                found.add(m.group(1))
    return sorted(found)


def prefixless_router_modules() -> list[str]:
    base = ROOT / "backend" / "src" / "pureiron"
    out: list[str] = []
    if base.is_dir():
        for router in base.glob("*/router.py"):
            text = _read_text(router)
            if re.search(r"APIRouter\(\s*\)", text):
                out.append(router.parent.name)
    return sorted(out)


def app_direct_routes() -> list[str]:
    """main.py 의 @app.<method>("...") 직접 라우트(raw)."""
    text = _read_text(ROOT / "backend" / "src" / "pureiron" / "main.py")
    routes = re.findall(
        r"@app\.(?:get|post|put|delete|patch|websocket)\(\s*[\"']([^\"']+)[\"']", text
    )
    return sorted(set(routes))


def migration_prefix_duplicates() -> list[str]:
    """migration SQL 의 중복 숫자 prefix 경고 문자열 (allowlist 제외).

    migration 은 파일명 알파벳순 자동 실행이라 prefix 중복은 실행 순서·리뷰 판단에
    영향을 준다. 기존에 의도적으로 남아 있는 중복(`MIGRATION_DUP_ALLOWLIST`)은
    통과시키고, 새로 생긴 중복만 경고한다. warn-only — 빌드/커밋을 막지 않는다.
    """
    if not MIGRATIONS_DIR.is_dir():
        return []
    by_prefix: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_", path.name)
        if m:
            by_prefix.setdefault(m.group(1), []).append(path.name)
    out: list[str] = []
    for prefix, names in sorted(by_prefix.items()):
        if len(names) > 1 and prefix not in MIGRATION_DUP_ALLOWLIST:
            out.append(
                f"prefix `{prefix}_` 중복 {len(names)}개: {', '.join(names)} "
                f"(새 migration 은 max+1 번호 사용)"
            )
    return out


def documented_prefixes(docs: list[WikiDoc]) -> list[str]:
    """api-events.md 에 기재된 백틱 경로(`/...`)."""
    for d in docs:
        if d.meta.get("id") == "sot-api-events":
            return sorted(set(re.findall(r"`(/[^`]+)`", d.body)))
    return []


# --------------------------------------------------------------------------- #
# Generation (build outputs in memory — shared by build & check)
# --------------------------------------------------------------------------- #
def _section_of(rel: str) -> str:
    """위키 문서의 구획. 구획은 **문서의 수명**으로 나뉜다.

    - SOT: 불변 계약. 코드가 바뀌면 같이 바뀌지만 시점 개념이 없다.
    - GTM: 사업/첫 계약 결정. canonical 이지만 엔지니어링 계약이 아니다.
    - Reports: 시점 스냅샷(감사·audit). `last_verified` 이후로는 낡을 수 있다.
    """
    if rel.startswith("docs/wiki/sot/"):
        return "SOT"
    if rel.startswith("docs/wiki/gtm/"):
        return "GTM"
    if rel.startswith("docs/wiki/reports/"):
        return "Reports"
    if rel.startswith("docs/wiki/adr/"):
        return "ADR"
    if rel.startswith("docs/wiki/archive/"):
        return "Archive"
    return "Index"


# llms.txt / 구획 순회 순서 (정렬 대신 의미 순서를 고정한다).
SECTION_ORDER = ("SOT", "GTM", "Reports", "ADR", "Archive")

# Reports 구획의 스냅샷이 이 일수를 넘기면 warn-only 경고를 낸다. 낡은 audit 은
# 없는 것보다 나쁘게 작동하므로(실제와 다른 상태를 근거로 결론이 나옴) 눈에 띄게 둔다.
REPORT_STALE_DAYS = 90


def build_manifest(docs: list[WikiDoc]) -> str:
    entries = []
    for d in sorted(docs, key=lambda x: x.rel):
        entries.append(
            {
                "id": d.meta.get("id", ""),
                "title": d.title,
                "status": d.status,
                "audience": d.meta.get("audience", ""),
                "path": d.rel,
                "sources": d.list_field("sources"),
                "source_globs": d.list_field("source_globs") if "source_globs" in d.meta else [],
                "last_verified": d.meta.get("last_verified", ""),
                "sha256": d.sha256(),
                # 읽기 비용과 섹션 분해 가능성만 노출한다. 섹션 목록 본문은
                # section-index.tsv 에 두어 manifest 를 작게 유지한다.
                "bytes": d.byte_size(),
                "section_count": len(d.sections()),
            }
        )
    manifest = {
        "generated_by": "scripts/wiki.py build",
        "doc_count": len(entries),
        "docs": entries,
        "outputs": list(GEN_OUTPUT_PATHS),
        "section_index": GEN_SECTION_INDEX,
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_section_index(docs: list[WikiDoc]) -> str:
    """섹션 단위 검색 인덱스 (탭 구분, 한 섹션 = 한 줄).

    JSON 이 아니라 줄 단위 TSV 인 이유: 이 파일의 용도는 `Grep` 이다. JSON 이면
    관심 있는 한 섹션을 찾기 위해 인덱스 전체를 파싱해서 읽어야 하는데, 그러면
    큰 SOT 문서를 통째로 읽는 것과 비용이 같아져 목적이 사라진다. 줄 단위면
    grep 이 매칭된 줄만 돌려주므로 검색 비용이 상수에 가깝다.
    """
    lines = [
        "# PureIron Wiki Section Index — 생성물. `python scripts/wiki.py build`로 갱신.",
        "#",
        "# 용도: 큰 SOT 문서를 통째로 읽지 말고 필요한 절만 읽는다.",
        "#   1) Grep 으로 이 파일에서 키워드를 찾는다 (매칭 줄만 반환됨).",
        "#   2) 그 줄의 path/offset/limit 으로 Read(path, offset=offset, limit=limit).",
        "#",
        "# 열: doc_id\tpath\tanchor\toffset\tlimit\tbytes\tlevel\theading",
        "#   offset/limit = 파일 전체 기준 1-indexed 시작 줄 / 줄 수 (Read 인자 그대로)",
        "#   bytes        = 해당 절을 읽는 비용 (한국어는 대략 1.5~2바이트/토큰)",
    ]
    for d in sorted(docs, key=lambda x: x.rel):
        doc_id = str(d.meta.get("id", ""))
        for sec in d.sections():
            heading = re.sub(r"\s+", " ", str(sec["title"])).strip()
            lines.append(
                "\t".join(
                    [
                        doc_id,
                        d.rel,
                        str(sec["anchor"]),
                        str(sec["line_start"]),
                        str(sec["line_count"]),
                        str(sec["bytes"]),
                        f"h{sec['level']}",
                        heading,
                    ]
                )
            )
    return "\n".join(lines).rstrip("\n") + "\n"


def build_llms_txt(docs: list[WikiDoc]) -> str:
    index = next((d for d in docs if d.meta.get("id") == "wiki-index"), None)
    lines = ["# PureIron Wiki", ""]
    if index:
        lines += [f"> {index.summary()}", ""]
    for section in SECTION_ORDER:
        section_docs = sorted(
            (d for d in docs if _section_of(d.rel) == section), key=lambda x: x.rel
        )
        if not section_docs:
            continue
        lines.append(f"## {section}")
        for d in section_docs:
            lines.append(f"- [{d.title}]({d.rel}): {d.summary()}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def build_llms_full(docs: list[WikiDoc]) -> str:
    """status: canonical + wiki-index 본문만. archive / _generated 제외."""
    selected = sorted(
        (d for d in docs if d.status == "canonical"), key=lambda x: x.rel
    )
    parts = [
        "# PureIron Wiki — Full Context",
        "",
        "> 생성물. `python scripts/wiki.py build`로 갱신.",
        ">",
        "> **에이전트는 이 파일을 열지 않는다.** canonical 문서 전문을 이어붙인 외부 LLM",
        "> ingest 전용 산출물이라 한 번 읽으면 컨텍스트를 통째로 소진한다. 작업 중",
        "> 필요한 내용은 `docs/wiki/_generated/section-index.tsv` 를 grep 해서 해당",
        "> 절만 읽는다.",
        "",
    ]
    for d in selected:
        parts.append(f"<!-- source: {d.rel} -->")
        parts.append(f"# {d.title}")
        parts.append("")
        parts.append(d.body.rstrip("\n"))
        parts.append("")
        parts.append("---")
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def agent_invariants(docs: list[WikiDoc]) -> list[str]:
    """agent-rules.md 의 `## 불변 규칙` 섹션 bullet 을 추출(보수적).

    헤더 `## 불변 규칙`(부분 일치) 부터 다음 `## ` 헤더 직전까지의 bullet(`- `/`* `)만
    수집한다. 결과가 비면 HARDCODED_INVARIANTS 로 fallback(절대 빈 섹션 방출 금지).
    반환값은 `- ...` 형태의 줄 리스트.
    """
    doc = next((d for d in docs if d.meta.get("id") == "sot-agent-rules"), None)
    if doc is None:
        return list(HARDCODED_INVARIANTS)
    # bullet 안의 상대 링크를 LLM_WIKI.md 위치 기준으로 다시 계산해서 담는다.
    lines = _rebase_relative_links(doc.body, doc.path.parent, LLM_WIKI).splitlines()
    in_section = False
    bullets: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break  # 다음 섹션 시작 → 종료
            if "불변 규칙" in stripped:
                in_section = True
            continue
        if not in_section:
            continue
        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            bullets.append(f"- {m.group(1).strip()}")
    return bullets or list(HARDCODED_INVARIANTS)


def build_llm_wiki(docs: list[WikiDoc]) -> str:
    """Harness 가 읽는 compact projection."""
    lines = [
        "# PureIron LLM Wiki (compact projection)",
        "",
        "> 생성물 — 직접 편집 금지. `python scripts/wiki.py build`로 갱신.",
        "",
        "이 문서는 Harness 가드레일에 주입되는 **압축 투영본**이다. 세부 내용은",
        "아래 `세부 탐색` 절차대로 **섹션 단위**로 찾는다.",
        "",
        "## SOT 요약",
    ]
    for d in sorted(docs, key=lambda x: x.rel):
        if _section_of(d.rel) != "SOT":
            continue
        sid = d.meta.get("id", "")
        cost = f"{d.byte_size() / 1024:.0f}KB/{len(d.sections())}절"
        lines.append(
            f"- **{sid}** ({cost}) — {d.summary()} "
            f"([{d.rel}]({_relpath_from(LLM_WIKI, d.path)}))"
        )

    # SOT 외 구획은 경로만 싣는다. 요약까지 넣으면 사업 문서·낡은 audit 산문이
    # 모든 엔지니어링 step 프롬프트에 딸려 들어간다. 필요하면 섹션 인덱스로 찾는다.
    others = [
        d
        for d in sorted(docs, key=lambda x: x.rel)
        if _section_of(d.rel) in ("GTM", "Reports")
    ]
    if others:
        lines += ["", "## SOT 외 구획 (필요할 때만 열기)"]
        for d in others:
            kind = "사업/첫 계약 결정" if _section_of(d.rel) == "GTM" else "시점 스냅샷(audit)"
            lines.append(
                f"- **{d.meta.get('id', '')}** — {kind}. "
                f"[{d.rel}]({_relpath_from(LLM_WIKI, d.path)})"
            )
        lines.append(
            "`reports/**` 는 `last_verified` 시점의 감사 결과이므로 결론 근거로 쓰기 전에 "
            "코드로 재확인한다."
        )

    lines += ["", "## 핵심 불변 규칙"]
    lines += agent_invariants(docs)
    lines += [
        "",
        "## 세부 탐색 (섹션 단위 — 문서를 통째로 읽지 않는다)",
        "위 SOT 는 대부분 10KB 이상이라 전문 Read 는 컨텍스트 낭비다. 순서를 지킨다.",
        "",
        "1. `Grep(pattern=<키워드>, path=\"docs/wiki/_generated/section-index.tsv\")`",
        "2. 매칭 줄의 열: `doc_id  path  anchor  offset  limit  bytes  level  heading`",
        "3. `Read(path, offset=<offset>, limit=<limit>)` — 그 절만 읽는다.",
        "",
        "문서 전체 목록·메타데이터는 `docs/wiki/_generated/manifest.json`.",
        "`_generated/llms-full.md` 는 외부 ingest 전용이므로 열지 않는다.",
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def build_agent_context(docs: list[WikiDoc]) -> str:
    """agent-rules.md 본문을 단독 에이전트 컨텍스트로 투영(생성물)."""
    doc = next((d for d in docs if d.meta.get("id") == "sot-agent-rules"), None)
    lines = [
        "# PureIron Agent Context (generated projection)",
        "",
        "> 생성물 — 직접 편집 금지. `python scripts/wiki.py build`로 갱신.",
        "> 정본: `docs/wiki/sot/agent-rules.md`",
        "",
    ]
    if doc is not None:
        body = _rebase_relative_links(
            doc.body, doc.path.parent, ROOT / GEN_AGENT_CONTEXT
        )
        lines.append(body.rstrip("\n"))
    else:
        lines += ["## 핵심 불변 규칙", *HARDCODED_INVARIANTS]
    return "\n".join(lines).rstrip("\n") + "\n"


def _relpath_from(src_file: Path, target: Path) -> str:
    import os

    return Path(os.path.relpath(target, src_file.parent)).as_posix()


def _rebase_relative_links(text: str, src_dir: Path, dst_file: Path) -> str:
    """본문을 다른 위치로 투영할 때 상대 링크를 다시 계산한다.

    생성물은 SOT 본문을 다른 디렉터리로 복사하므로, `[architecture](architecture.md)`
    처럼 원본 위치를 기준으로 쓰인 링크가 그대로 옮겨지면 죽은 링크가 된다.
    (`docs/wiki/sot/` 기준 링크가 `docs/LLM_WIKI.md`·`docs/wiki/_generated/` 로
    복사되던 기존 문제.) URL·mailto·문서 내 `#anchor` 는 건드리지 않는다.
    """
    link_re = re.compile(r"(\]\()([^)]+)(\))")

    def repl(m: re.Match[str]) -> str:
        target = m.group(2).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        path_part, sep, frag = target.partition("#")
        if not path_part:
            return m.group(0)
        resolved = (src_dir / path_part).resolve()
        return f"{m.group(1)}{_relpath_from(dst_file, resolved)}{sep}{frag}{m.group(3)}"

    return link_re.sub(repl, text)


def build_drift_report(docs: list[WikiDoc]) -> str:
    cfg = config_env_vars()
    env = env_example_vars()
    missing_in_env = sorted(set(cfg) - set(env))
    extra_in_env = sorted(set(env) - set(cfg))

    src_prefixes = router_prefixes()
    doc_prefixes = documented_prefixes(docs)
    undocumented = sorted(set(src_prefixes) - set(doc_prefixes))
    missing_routes = sorted(set(doc_prefixes) - set(src_prefixes))

    lines = [
        "# SOT Drift Report",
        "",
        "> 생성물(build 전용). drift 는 경고(warn-only)이며 빌드/커밋을 막지 않는다.",
        "",
        "## env drift (`config.py` ↔ `.env.example`)",
        "",
    ]
    if missing_in_env:
        lines.append("`.env.example` 에 누락된 변수:")
        lines += [f"- {v}" for v in missing_in_env]
    else:
        lines.append("- (누락 없음)")
    lines.append("")
    if extra_in_env:
        lines.append("`config.py` 에 없는 `.env.example` 변수:")
        lines += [f"- {v}" for v in extra_in_env]
    else:
        lines.append("`config.py` 에 없는 `.env.example` 변수: (없음)")
    lines += ["", "## API drift (incomplete heuristic, v1)", ""]
    lines.append("router.py APIRouter prefix:")
    lines += [f"- {p}" for p in src_prefixes] or ["- (없음)"]
    lines.append("")
    if undocumented:
        lines.append("api-events.md 에 미기재된 소스 프리픽스:")
        lines += [f"- {p}" for p in undocumented]
    else:
        lines.append("api-events.md 에 미기재된 소스 프리픽스: (없음)")
    lines.append("")
    if missing_routes:
        lines.append("문서에만 있고 소스에서 못 찾은 프리픽스:")
        lines += [f"- {p}" for p in missing_routes]
    else:
        lines.append("문서에만 있고 소스에서 못 찾은 프리픽스: (없음)")
    lines += ["", "prefix 없는 router 모듈:"]
    lines += [f"- {m}" for m in prefixless_router_modules()] or ["- (없음)"]
    lines += ["", "main.py 직접 라우트(raw):"]
    lines += [f"- {r}" for r in app_direct_routes()] or ["- (없음)"]

    lines += ["", "## source warnings (secret-like 기본값)", ""]
    warns = secret_source_warnings()
    lines += [f"- {w}" for w in warns] or ["- (없음)"]
    lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def generate_outputs(docs: list[WikiDoc]) -> dict[str, str]:
    """모든 생성물의 {repo-relative path: content} 반환 (결정적)."""
    return {
        GEN_MANIFEST: build_manifest(docs),
        GEN_LLMS_TXT: build_llms_txt(docs),
        GEN_LLMS_FULL: build_llms_full(docs),
        GEN_DRIFT: build_drift_report(docs),
        GEN_AGENT_CONTEXT: build_agent_context(docs),
        GEN_SECTION_INDEX: build_section_index(docs),
        GEN_LLM_WIKI: build_llm_wiki(docs),
    }


# --------------------------------------------------------------------------- #
# Structural checks (hard-fail)
# --------------------------------------------------------------------------- #
def check_frontmatter(docs: list[WikiDoc]) -> list[str]:
    errors: list[str] = []
    for d in docs:
        for field in REQUIRED_FIELDS:
            if field not in d.meta or d.meta[field] in ("", [], None):
                errors.append(f"[frontmatter] {d.rel}: 필수 필드 누락 `{field}`")
        for field in LIST_FIELDS:
            if field in d.meta and not isinstance(d.meta[field], list):
                errors.append(f"[frontmatter] {d.rel}: `{field}` 는 리스트여야 함")
        status = d.meta.get("status")
        if status and status not in STATUS_VALUES:
            errors.append(f"[frontmatter] {d.rel}: status `{status}` 비허용")
        audience = d.meta.get("audience")
        if audience and audience not in AUDIENCE_VALUES:
            errors.append(f"[frontmatter] {d.rel}: audience `{audience}` 비허용")
        lv = d.meta.get("last_verified")
        if lv and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(lv)):
            errors.append(f"[frontmatter] {d.rel}: last_verified 형식(YYYY-MM-DD) 위반: {lv}")
    return errors


def check_sources(docs: list[WikiDoc]) -> list[str]:
    errors: list[str] = []
    for d in docs:
        for src in d.list_field("sources"):
            if src.startswith(LEGACY_PERMALINK_PREFIXES):
                continue
            if any(ch in src for ch in "*?[") or not (ROOT / src).exists():
                if any(ch in src for ch in "*?["):
                    errors.append(f"[sources] {d.rel}: glob 금지(`source_globs` 사용): {src}")
                else:
                    errors.append(f"[sources] {d.rel}: 존재하지 않는 경로: {src}")
    return errors


def check_links(docs: list[WikiDoc]) -> list[str]:
    errors: list[str] = []
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for d in docs:
        for target in link_re.findall(d.body):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0].strip()
            if not path_part:
                continue
            resolved = (d.path.parent / path_part).resolve()
            if not resolved.exists():
                errors.append(f"[link] {d.rel}: 깨진 링크 → {target}")
    return errors


def check_anchors(docs: list[WikiDoc]) -> list[str]:
    """`#anchor` 링크가 실제 heading 을 가리키는지 검증한다.

    `check_links` 는 경로만 보고 fragment 는 보지 않아서, 문서 안 heading 이 바뀌면
    `foo.md#옛-제목` 이 조용히 죽은 링크로 남는다. 섹션 인덱스가 있으니 이제
    유효 anchor 집합을 알 수 있어 hard-fail 로 막는다. 위키 밖 문서는 인덱스가
    없으므로 대상에서 제외한다(경로 존재만 `check_links` 가 본다).
    """
    anchors = {d.rel: {str(s["anchor"]) for s in d.sections()} for d in docs}
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    errors: list[str] = []
    for d in docs:
        for target in link_re.findall(d.body):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:")) or "#" not in target:
                continue
            path_part, _, frag = target.partition("#")
            if not frag:
                continue
            if path_part == "":
                rel = d.rel  # 같은 문서 안 anchor
            else:
                resolved = (d.path.parent / path_part).resolve()
                try:
                    rel = resolved.relative_to(ROOT).as_posix()
                except ValueError:
                    continue  # repo 밖
            if rel not in anchors:
                continue  # 위키 밖 문서
            if frag not in anchors[rel]:
                errors.append(f"[anchor] {d.rel}: 존재하지 않는 anchor → {target}")
    return errors


def check_section_offsets(docs: list[WikiDoc]) -> list[str]:
    """섹션 인덱스의 `offset` 이 실제 heading 줄을 가리키는지 검증한다.

    off-by-one 이 생기면 인덱스는 그럴듯하게 생성되지만 에이전트가 엉뚱한 줄부터
    읽게 되고, 아무 에러도 나지 않아 조용히 쓸모없어진다. 그래서 hard-fail 로 둔다.
    """
    errors: list[str] = []
    for d in docs:
        file_lines = d.raw_bytes.decode("utf-8").splitlines()
        for sec in d.sections():
            start = int(sec["line_start"])  # 1-indexed
            end = int(sec["line_end"])
            if not 1 <= start <= len(file_lines):
                errors.append(
                    f"[section] {d.rel}: offset {start} 범위 밖 (총 {len(file_lines)}줄)"
                )
                continue
            if end < start or end > len(file_lines):
                errors.append(f"[section] {d.rel}:{start}: line_end {end} 비정상")
                continue
            actual = file_lines[start - 1]
            heading = _HEADING_RE.match(actual)
            if heading is None or heading.group(2).strip() != sec["title"]:
                errors.append(
                    f"[section] {d.rel}: offset {start} 이 heading "
                    f"`{sec['title']}` 을 가리키지 않음 (실제: {actual.strip()!r})"
                )
    return errors


def check_stale(docs: list[WikiDoc]) -> list[str]:
    """생성물을 메모리에서 재생성해 디스크 바이트와 비교 (비파괴)."""
    errors: list[str] = []
    for rel, content in generate_outputs(docs).items():
        disk = ROOT / rel
        expected = content.encode("utf-8")
        if not disk.exists():
            errors.append(f"[stale] {rel}: 생성물 없음 — `python scripts/wiki.py build` 실행 필요")
        elif disk.read_bytes() != expected:
            errors.append(f"[stale] {rel}: 최신이 아님 — `python scripts/wiki.py build` 후 커밋")
    return errors


# --------------------------------------------------------------------------- #
# Warnings (warn-only — stdout only, never written by check)
# --------------------------------------------------------------------------- #
def collect_warnings(docs: list[WikiDoc]) -> list[str]:
    warns: list[str] = []
    # source_globs 매칭 0건
    for d in docs:
        for pat in d.list_field("source_globs"):
            if not _glob_matches(pat):
                warns.append(f"[glob] {d.rel}: source_globs 매칭 0건: {pat}")
    # env drift
    missing_env = sorted(set(config_env_vars()) - set(env_example_vars()))
    if missing_env:
        joined = ", ".join(missing_env)
        warns.append(f"[env-drift] .env.example 누락 {len(missing_env)}개: {joined}")
    # api drift
    undocumented = sorted(set(router_prefixes()) - set(documented_prefixes(docs)))
    if undocumented:
        warns.append(f"[api-drift] 미기재 프리픽스: {', '.join(undocumented)}")
    # migration prefix 중복 (allowlist 제외)
    for w in migration_prefix_duplicates():
        warns.append(f"[migration-dup] {w}")
    # reports/** 스냅샷 노후화
    warns.extend(_stale_report_warnings(docs))
    # secret source warnings
    for w in secret_source_warnings():
        warns.append(f"[secret-source] {w}")
    # secret body scan (wiki/generated markdown)
    warns.extend(_secret_body_warnings(docs))
    return warns


def _glob_matches(pattern: str) -> bool:
    # configs/**/*.yaml 같은 ** 패턴은 rglob 로, 단순 패턴은 glob 로 평가.
    try:
        if "**" in pattern:
            base, _, tail = pattern.partition("**/")
            base_dir = ROOT / base if base else ROOT
            return any(base_dir.rglob(tail or "*"))
        return any(ROOT.glob(pattern))
    except (OSError, ValueError):
        return False


def _stale_report_warnings(docs: list[WikiDoc]) -> list[str]:
    """`reports/**` 스냅샷이 `REPORT_STALE_DAYS` 를 넘겼는지 (warn-only).

    build 출력에는 들어가지 않는다 — 오늘 날짜에 의존하는 값이 생성물에 섞이면
    `check_stale` 이 매일 실패하기 때문이다. `check` 의 stdout 경고로만 낸다.
    """
    today = datetime.date.today()
    out: list[str] = []
    for d in docs:
        if _section_of(d.rel) != "Reports":
            continue
        raw = str(d.meta.get("last_verified", ""))
        try:
            verified = datetime.date.fromisoformat(raw)
        except ValueError:
            continue  # 형식 오류는 check_frontmatter 가 hard-fail 로 잡는다
        age = (today - verified).days
        if age > REPORT_STALE_DAYS:
            out.append(
                f"[stale-report] {d.rel}: last_verified {raw} 기준 {age}일 경과 "
                f"({REPORT_STALE_DAYS}일 초과) — 재감사 후 본문·last_verified 갱신 필요"
            )
    return out


def _secret_body_warnings(docs: list[WikiDoc]) -> list[str]:
    out: list[str] = []
    scan: list[tuple[str, str]] = [(d.rel, d.body) for d in docs]
    for rel in (GEN_LLMS_FULL, GEN_DRIFT, GEN_AGENT_CONTEXT, GEN_LLM_WIKI):
        p = ROOT / rel
        if p.exists():
            scan.append((rel, _read_text(p)))
    for rel, text in scan:
        for pat in SECRET_BODY_PATTERNS:
            if pat.search(text):
                out.append(f"[secret-body] {rel}: secret 처럼 보이는 토큰 노출")
                break
    return out


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_inventory() -> int:
    candidates: list[tuple[str, str]] = []
    excluded: list[str] = []

    def classify(path: Path) -> str:
        rel = path.relative_to(ROOT).as_posix()
        if rel == "docs/LLM_WIKI.md" or rel.startswith("docs/wiki/_generated/"):
            return "generated"
        if rel.startswith("docs/wiki/"):
            return "wiki"
        if rel in ("AGENTS.md", "CLAUDE.md"):
            return "duplicate"
        if rel in ("pipeline.md",):
            return "archive-candidate"
        return "source"

    scan_globs = [
        "*.md",
        "docs/**/*.md",
        "backend/CLAUDE.md",
        "frontend/CLAUDE.md",
        "frontend/AGENTS.md",
        "configs/**/*.yaml",
        "configs/**/*.yml",
        "configs/**/*.json",
        "backend/src/pureiron/db/migrations/*.sql",
    ]
    seen: set[str] = set()
    for pattern in scan_globs:
        for path in ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            parts = path.relative_to(ROOT).parts
            if any(p in EXCLUDE_PARTS for p in parts) or path.suffix in EXCLUDE_SUFFIX:
                excluded.append(rel)
                continue
            candidates.append((classify(path), rel))

    print("# Wiki Inventory\n")
    print(f"후보 {len(candidates)}개 / 제외 {len(excluded)}개\n")
    by_class: dict[str, list[str]] = {}
    for cls, rel in sorted(candidates):
        by_class.setdefault(cls, []).append(rel)
    for cls in ("wiki", "generated", "source", "duplicate", "archive-candidate"):
        items = by_class.get(cls, [])
        print(f"## {cls} ({len(items)})")
        for rel in items:
            print(f"  - {rel}")
        print()
    if excluded:
        print(f"## excluded ({len(excluded)})")
        for rel in sorted(excluded):
            print(f"  - {rel}")
        print()
    return 0


def cmd_build() -> int:
    docs, load_errors = load_docs()
    if load_errors:
        for e in load_errors:
            print(f"ERROR {e}", file=sys.stderr)
        print("\nbuild 중단: 로딩 단계 오류를 먼저 고치세요.", file=sys.stderr)
        return 1
    GEN.mkdir(parents=True, exist_ok=True)
    outputs = generate_outputs(docs)
    for rel, content in outputs.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"  wrote {rel}")
    print(f"\nbuild 완료: 문서 {len(docs)}개 → 생성물 {len(outputs)}개")
    return 0


def cmd_check() -> int:
    docs, load_errors = load_docs()
    errors: list[str] = list(load_errors)
    errors += check_frontmatter(docs)
    errors += check_sources(docs)
    errors += check_links(docs)
    errors += check_anchors(docs)
    errors += check_section_offsets(docs)
    errors += check_stale(docs)

    warnings = collect_warnings(docs)
    for w in warnings:
        print(f"WARN  {w}")

    if errors:
        print()
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        print(f"\ncheck 실패: 구조 오류 {len(errors)}개 / 경고 {len(warnings)}개", file=sys.stderr)
        return 1
    print(f"\ncheck 통과: 문서 {len(docs)}개, 구조 오류 0개, 경고 {len(warnings)}개")
    return 0


def _force_utf8_streams() -> None:
    """Windows 콘솔(cp949 등)에서도 UTF-8 출력이 깨지지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="PureIron Wiki tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="문서 후보·제외 목록 출력(비파괴)")
    sub.add_parser("build", help="생성물 갱신")
    sub.add_parser("check", help="구조 검증(비파괴) + drift 경고")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        return cmd_inventory()
    if args.command == "build":
        return cmd_build()
    if args.command == "check":
        return cmd_check()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

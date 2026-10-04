"""`pureiron-core migrate lint`: 확장형(ADD-only) 마이그레이션 규칙 검사(ADR-024, plan §6.6).

baseline(epoch 1)의 파일은 규칙 목록에서 면제되고, 그 뒤 파일은 구 앱(N-1)이 새 스키마(N)에서 계속
동작하도록 파괴적·제약 추가 구문을 거부한다. `--base-ref`가 주어지면 base에 이미 있는 마이그레이션과
epoch 항목이 바뀌지 않았는지도 본다(적용된 파일을 고치면 checksum이 깨진다).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pureiron_core.platform.migrate import (
    EPOCHS_FILE,
    NO_TRANSACTION_MARKER,
    Catalog,
    CatalogError,
    load_catalog,
    load_epochs,
    parse_migration_name,
)
from pureiron_core.platform.sqltext import SqlSyntaxError, Statement, split_statements

BASELINE_EPOCH = 1

_CONTRACT = re.compile(
    r"--\s*contract:\s*drops\s+(\S+)\s+deprecated\s+in\s+epoch\s+(\d+)", re.IGNORECASE
)
_SIMPLE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bRENAME\b"), "RENAME is not additive"),
    (re.compile(r"\bTRUNCATE\b"), "TRUNCATE is not additive"),
    (re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b"), "CREATE UNIQUE INDEX can fail on existing data"),
    (
        re.compile(r"\bCREATE\s+(OR\s+REPLACE\s+)?(CONSTRAINT\s+)?TRIGGER\b"),
        "CREATE TRIGGER changes write behaviour for older apps",
    ),
    (
        re.compile(r"\bREFRESH_CONTINUOUS_AGGREGATE\b"),
        "refresh_continuous_aggregate in a migration blocks on data size; use a refresh policy",
    ),
)
_DROP = re.compile(r"\bDROP\b")
_ALTER_TYPE = re.compile(r"^ALTER\s+(COLUMN\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b")
# baseline 이후 파일에서 허용하는 구문 형태(허용 목록). 목록 밖 구문은 모두 거부한다:
# DO 블록, 함수·프로시저, EXECUTE, BEGIN/COMMIT, UPDATE/DELETE, OR REPLACE,
# drop_chunks·remove_*·retention 정책 등.
_ALLOWED_STATEMENTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^CREATE\s+TABLE\b"),
    re.compile(r"^CREATE\s+INDEX\b"),
    re.compile(r"^CREATE\s+VIEW\b"),
    re.compile(r"^CREATE\s+MATERIALIZED\s+VIEW\b"),
    re.compile(r"^CREATE\s+TYPE\b"),
    re.compile(r"^CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\b"),
    re.compile(r"^ALTER\s+TYPE\s+\S+\s+ADD\s+VALUE\b"),
    re.compile(r"^ALTER\s+TABLE\b"),
    re.compile(r"^COMMENT\s+ON\b"),
    re.compile(
        r"^SELECT\s+(CREATE_HYPERTABLE|ADD_COMPRESSION_POLICY|ADD_CONTINUOUS_AGGREGATE_POLICY)\s*\("
    ),
    re.compile(r"^INSERT\s+INTO\b.*\bON\s+CONFLICT\s+DO\s+NOTHING$"),
)
_TIMESCALE_SET = re.compile(r"^SET\s*\(\s*TIMESCALEDB\.")
# 허용 목록은 구문 첫머리만 보므로, 구문 어디에든 나오면 거부할 파괴적 함수·형태를 따로 찾는다.
_DESTRUCTIVE_CALL = re.compile(
    r"\b(DROP_CHUNKS|REMOVE_\w+|ADD_RETENTION_POLICY|DECOMPRESS_CHUNK|COMPRESS_CHUNK|DETACH_\w+"
    r"|DELETE_\w+|ALTER_JOB|SET_CHUNK_TIME_INTERVAL|ATTACH_\w+|MOVE_CHUNK|REORDER_CHUNK)\s*\("
)
_CREATE_TABLE_AS = re.compile(r"^CREATE\s+TABLE\b[^(]*\bAS\b")
_TIMESCALE_SELECT = re.compile(r"^SELECT\s+\w+\s*\((.*)\)$")
_TABLE_CONSTRAINT = re.compile(
    r"^ADD\s+(CONSTRAINT|CHECK|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY|EXCLUDE)\b"
)
_COLUMN_CONSTRAINT = re.compile(r"\b(REFERENCES|CHECK|UNIQUE|PRIMARY\s+KEY)\b")


@dataclass(frozen=True, slots=True)
class LintIssue:
    file: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


@dataclass(slots=True)
class LintReport:
    issues: list[LintIssue] = field(default_factory=list[LintIssue])
    notices: list[str] = field(default_factory=list[str])

    def add(self, file: str, message: str) -> None:
        self.issues.append(LintIssue(file, message))


def _top_level_actions(code: str) -> list[str]:
    """`ALTER TABLE t <action>, <action>`의 action 목록(괄호 안 쉼표는 무시)."""
    match = re.match(r"^ALTER\s+TABLE\s+(IF\s+EXISTS\s+)?(ONLY\s+)?\S+\s+(.*)$", code)
    if match is None:
        return []
    actions: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in match[3]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            actions.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    actions.append("".join(current).strip())
    return [a for a in actions if a]


def _check_alter_table(filename: str, code: str, report: LintReport) -> None:
    for action in _top_level_actions(code):
        if action.startswith("DROP"):
            continue  # contract 예외 여부는 _check_drop이 판정한다
        if not action.startswith("ADD"):
            if _ALTER_TYPE.match(action):
                report.add(filename, "ALTER ... TYPE rewrites the column and breaks older apps")
            elif not _TIMESCALE_SET.match(action):
                report.add(filename, f"ALTER TABLE action is not additive: {action}")
            continue
        if _TABLE_CONSTRAINT.match(action):
            report.add(filename, f"ADD CONSTRAINT can fail on existing rows: {action}")
            continue
        if _COLUMN_CONSTRAINT.search(action):
            report.add(
                filename, f"column constraint on ADD COLUMN can fail on existing rows: {action}"
            )
        if re.search(r"\bNOT\s+NULL\b", action) and not re.search(r"\bDEFAULT\b", action):
            report.add(
                filename, f"ADD COLUMN ... NOT NULL without DEFAULT breaks older writers: {action}"
            )


def _contracts(sql: str) -> list[tuple[str, int]]:
    return [(m[1].upper(), int(m[2])) for m in _CONTRACT.finditer(sql)]


def _mentions(code: str, obj: str) -> bool:
    """`table.column` 같은 선언 객체의 각 이름이 구문에 단어로 모두 나오는지."""
    return all(re.search(rf"\b{re.escape(part)}\b", code) for part in obj.split("."))


def _check_drop(
    filename: str,
    statement: Statement,
    file_epoch: int,
    contracts: list[tuple[str, int]],
    report: LintReport,
) -> None:
    covering = [(obj, dep) for obj, dep in contracts if _mentions(statement.code, obj)]
    if not covering:
        report.add(
            filename,
            "DROP is not additive (contract removal needs "
            "`-- contract: drops <obj> deprecated in epoch <E>`)",
        )
        return
    for obj, deprecated in covering:
        # deprecated 표시 epoch D의 앱은 객체를 쓰지 않고, D-1 앱의 envelope 상한은 D다.
        # 따라서 D+1 스키마부터 제거할 수 있다(plan §6.6: 주석 `deprecated in epoch <E-1>`).
        if file_epoch < deprecated + 1:
            report.add(
                filename,
                f"contract drop of {obj} deprecated in epoch {deprecated} is allowed from epoch "
                f"{deprecated + 1}, this file is epoch {file_epoch}",
            )


def _check_statement(
    filename: str,
    statement: Statement,
    file_epoch: int,
    contracts: list[tuple[str, int]],
    report: LintReport,
) -> None:
    code = statement.code
    for pattern, message in _SIMPLE_RULES:
        if pattern.search(code):
            report.add(filename, message)
    if _DROP.search(code):
        _check_drop(filename, statement, file_epoch, contracts, report)
    if not code.startswith("DROP") and not any(p.match(code) for p in _ALLOWED_STATEMENTS):
        head = " ".join(code.split()[:4])
        report.add(filename, f"statement is not in the additive allowlist: {head} ...")
    if call := _DESTRUCTIVE_CALL.search(code):
        report.add(filename, f"destructive TimescaleDB call is not allowed: {call[1].lower()}")
    if _CREATE_TABLE_AS.match(code):
        report.add(
            filename, "CREATE TABLE ... AS runs arbitrary queries; create the table, then insert"
        )
    if code.startswith("SELECT"):
        _check_single_call(filename, code, report)
    if code.startswith("ALTER TABLE"):
        _check_alter_table(filename, code, report)
    if "TIMESCALEDB.CONTINUOUS" in code and "WITH NO DATA" not in code:
        report.add(
            filename, "continuous aggregate must be created WITH NO DATA (refresh via policy)"
        )


def _check_single_call(filename: str, code: str, report: LintReport) -> None:
    """허용되는 TimescaleDB SELECT는 FROM 없는 함수 호출 하나뿐이다(인자 안 쉼표는 허용)."""
    match = _TIMESCALE_SELECT.match(code)
    depth = 0
    top_level_comma = False
    for ch in match[1] if match else "":
        depth += {"(": 1, ")": -1}.get(ch, 0)
        top_level_comma = top_level_comma or (ch == "," and depth < 0)
    if match is None or top_level_comma or " FROM " in code or depth != 0:
        report.add(filename, "SELECT in a migration must be a single policy/hypertable call")


def _check_no_transaction(filename: str, statements: list[Statement], report: LintReport) -> None:
    if len(statements) > 1:
        report.add(filename, f"{NO_TRANSACTION_MARKER} file must contain exactly one statement")
    for statement in statements:
        if "IF NOT EXISTS" not in statement.code and "IF_NOT_EXISTS" not in statement.code:
            report.add(
                filename,
                f"{NO_TRANSACTION_MARKER} statement must be re-runnable "
                "(IF NOT EXISTS / if_not_exists)",
            )


def _scan_names(directory: Path, report: LintReport) -> None:
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        parsed = parse_migration_name(path.name)
        if parsed is None:
            report.add(path.name, "file name must match NNNN_lower_snake.sql")
            continue
        version = parsed[0]
        if version in seen:
            report.add(
                path.name, f"duplicate migration version {version:04d} (also {seen[version]})"
            )
        seen[version] = path.name


def _lint_rules(catalog: Catalog, report: LintReport) -> None:
    baseline_last = catalog.last_version_of(BASELINE_EPOCH)
    for migration in catalog.migrations:
        if migration.version <= baseline_last:
            continue
        try:
            statements = split_statements(migration.sql)
        except SqlSyntaxError as exc:
            report.add(migration.filename, f"cannot parse SQL: {exc}")
            continue
        file_epoch = catalog.epoch_of(migration.version)
        contracts = _contracts(migration.sql)
        for statement in statements:
            _check_statement(migration.filename, statement, file_epoch, contracts, report)
        if not migration.transactional:
            _check_no_transaction(migration.filename, statements, report)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _lint_against_base(directory: Path, base_ref: str, report: LintReport) -> None:
    top = _git(directory, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        report.add(str(directory), "--base-ref needs a git checkout")
        return
    root = Path(top.stdout.decode().strip())
    rel = directory.resolve().relative_to(root.resolve()).as_posix()
    # ref 자체가 없으면(오타·force-push 뒤 미fetch) 불변성 검사를 조용히 끄지 않고 실패한다.
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}").returncode != 0:
        report.add(rel, f"--base-ref {base_ref} is not a known commit (fetch it first)")
        return
    if _git(root, "cat-file", "-e", f"{base_ref}:{rel}").returncode != 0:
        report.notices.append(
            f"{base_ref} has no {rel} yet — released-file immutability check skipped"
        )
        return
    listing = _git(root, "ls-tree", "--name-only", f"{base_ref}:{rel}")
    if listing.returncode != 0:
        report.add(rel, f"cannot list {base_ref}:{rel}: {listing.stderr.decode().strip()}")
        return
    base_names = listing.stdout.decode().split()
    base_versions = [
        v for n in base_names if (parsed := parse_migration_name(n)) for v in (parsed[0],)
    ]
    if base_versions:
        newest_released = max(base_versions)
        for path in sorted(directory.glob("*.sql")):
            parsed = parse_migration_name(path.name)
            if parsed and path.name not in base_names and parsed[0] <= newest_released:
                report.add(
                    path.name,
                    f"new migration numbered at or below released {newest_released:04d}; "
                    "use a higher version",
                )
    for name in base_names:
        base_bytes = _git(root, "show", f"{base_ref}:{rel}/{name}").stdout
        current = directory / name
        if name == EPOCHS_FILE:
            _compare_epochs(directory, base_bytes, report)
        elif name.endswith(".sql"):
            if not current.exists():
                report.add(name, f"released migration deleted (present in {base_ref})")
            elif _normalized(current.read_bytes()) != _normalized(base_bytes):
                report.add(
                    name, f"released migration modified (differs from {base_ref}); add a new file"
                )


def _compare_epochs(directory: Path, base_bytes: bytes, report: LintReport) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base_path = Path(tmp) / EPOCHS_FILE
        base_path.write_bytes(base_bytes)
        try:
            base = load_epochs(base_path)
        except CatalogError as exc:
            report.notices.append(f"base {EPOCHS_FILE} unreadable, skipped: {exc}")
            return
    current = load_epochs(directory / EPOCHS_FILE)
    for epoch, filename in sorted(base.items()):
        if current.get(epoch) != filename:
            report.add(EPOCHS_FILE, f"released epoch {epoch} → {filename} must not change")


def lint_directory(directory: Path, *, base_ref: str | None = None) -> LintReport:
    report = LintReport()
    _scan_names(directory, report)
    if report.issues:
        return report
    try:
        catalog = load_catalog(directory)
    except CatalogError as exc:
        report.add(str(directory), str(exc))
        return report
    _lint_rules(catalog, report)
    if base_ref is not None:
        _lint_against_base(directory, base_ref, report)
    return report

"""고객 프로젝트 트리 로드·교차 검증·content hash·정규형 포맷(ADR-025, plan §6.3).

트리 구성: `project.yaml`, `devices/<id>.yaml`, `tags/<device>.yaml`, `alarms/*.yaml`,
`screens/`·`assets/`(예약 — 비어 있어야 함, `.gitkeep`만 허용). 그 밖의 파일(README, .github 등)은
프로젝트 내용이 아니므로 무시한다.

content_hash = `sha256:` + sha256(JCS([[posix_relpath, 정규 데이터], ...] — 경로 UTF-8 바이트 순)).
정규 데이터는 문자열 NFC + 스키마 기본값 생략을 적용한 값이라, 서식·키 순서·줄바꿈·기본값 명시
여부가 달라도 같은 내용이면 같은 해시가 된다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from pureiron_project.canonical import CanonicalError, Json, dump, jcs, load
from pureiron_project.schema import Kind, omit_defaults, validate

PROJECT_FILE = "project.yaml"
CONTENT_DIRS: tuple[tuple[str, Kind], ...] = (
    ("devices", Kind.DEVICE),
    ("tags", Kind.TAG_LIST),
    ("alarms", Kind.ALARM_LIST),
)
RESERVED_DIRS = ("screens", "assets")
ALLOWED_IN_RESERVED = {".gitkeep"}


class RelPathError(ValueError):
    pass


def normalize_relpath(text: str) -> str:
    """Windows·POSIX 상대 경로 입력 → POSIX 상대 경로. 절대 경로·`..`·드라이브는 거부."""
    windows = PureWindowsPath(text)
    if windows.drive or windows.root or text.startswith(("/", "\\")):
        raise RelPathError(f"absolute path not allowed: {text!r}")
    parts = [p for p in windows.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise RelPathError(f"invalid relative path: {text!r}")
    return PurePosixPath(*parts).as_posix()


@dataclass(frozen=True, slots=True)
class ProjectFile:
    relpath: str
    kind: Kind
    path: Path
    data: Json


@dataclass(slots=True)
class Project:
    root: Path
    files: list[ProjectFile] = field(default_factory=list[ProjectFile])
    problems: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.problems

    def content_hash(self) -> str:
        ordered = sorted(self.files, key=lambda f: f.relpath.encode("utf-8"))
        payload: Json = [[f.relpath, f.data] for f in ordered]
        return "sha256:" + hashlib.sha256(jcs(payload)).hexdigest()


def _is_link(path: Path, root: Path, problems: list[str]) -> bool:
    """심볼릭 링크는 거부한다 — fmt가 링크를 따라 프로젝트 밖 파일을 덮어쓰지 않게."""
    if path.is_symlink():
        problems.append(
            f"{normalize_relpath(str(path.relative_to(root)))}: symbolic links are not allowed"
        )
        return True
    return False


def _discover_dir(
    root: Path, directory: str, kind: Kind, problems: list[str]
) -> list[tuple[str, Kind, Path]]:
    base = root / directory
    if not base.exists() or _is_link(base, root, problems):
        return []
    found: list[tuple[str, Kind, Path]] = []
    for path in sorted(base.iterdir()):
        rel = normalize_relpath(str(path.relative_to(root)))
        if _is_link(path, root, problems):
            continue
        if path.is_dir():
            problems.append(f"{rel}: nested directories are not allowed")
        elif path.suffix != ".yaml":
            problems.append(f"{rel}: only .yaml files are allowed")
        else:
            found.append((rel, kind, path))
    return found


def _discover(root: Path, problems: list[str]) -> list[tuple[str, Kind, Path]]:
    found: list[tuple[str, Kind, Path]] = []
    project_file = root / PROJECT_FILE
    if not _is_link(project_file, root, problems):
        if project_file.is_file():
            found.append((PROJECT_FILE, Kind.PROJECT, project_file))
        else:
            problems.append(f"{PROJECT_FILE}: missing")
    for directory, kind in CONTENT_DIRS:
        found.extend(_discover_dir(root, directory, kind, problems))
    for directory in RESERVED_DIRS:
        base = root / directory
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.name not in ALLOWED_IN_RESERVED:
                    rel = normalize_relpath(str(path.relative_to(root)))
                    problems.append(
                        f"{rel}: {directory}/ is reserved (screens·assets 지원은 이후 단계)"
                    )
    return found


def _as_list(data: Json) -> list[dict[str, Json]]:
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _check_devices(files: list[ProjectFile], problems: list[str]) -> set[str]:
    devices: set[str] = set()
    for f in files:
        if f.kind is Kind.DEVICE and isinstance(f.data, dict):
            device_id = f.data.get("id")
            if device_id != f.path.stem:
                problems.append(
                    f"{f.relpath}: id {device_id!r} must match file name {f.path.stem!r}"
                )
            if isinstance(device_id, str):
                devices.add(device_id)
    return devices


def _check_tags(files: list[ProjectFile], devices: set[str], problems: list[str]) -> set[str]:
    tag_paths: dict[str, str] = {}
    for f in (f for f in files if f.kind is Kind.TAG_LIST):
        if f.path.stem not in devices:
            problems.append(f"{f.relpath}: no device {f.path.stem!r} for this tag file")
        for tag in _as_list(f.data):
            path, device = tag.get("path"), tag.get("device")
            if device != f.path.stem:
                problems.append(
                    f"{f.relpath}: tag {path!r} device {device!r} must be {f.path.stem!r}"
                )
            if not isinstance(path, str):
                continue
            if path in tag_paths:
                problems.append(
                    f"{f.relpath}: duplicate tag path {path!r} (also {tag_paths[path]})"
                )
            tag_paths[path] = f.relpath
    return set(tag_paths)


def _check_alarms(files: list[ProjectFile], tags: set[str], problems: list[str]) -> None:
    alarm_ids: dict[str, str] = {}
    for f in (f for f in files if f.kind is Kind.ALARM_LIST):
        for alarm in _as_list(f.data):
            alarm_id, tag = alarm.get("id"), alarm.get("tag")
            if isinstance(alarm_id, str):
                if alarm_id in alarm_ids:
                    problems.append(
                        f"{f.relpath}: duplicate alarm id {alarm_id!r} (also {alarm_ids[alarm_id]})"
                    )
                alarm_ids[alarm_id] = f.relpath
            if isinstance(tag, str) and tag not in tags:
                problems.append(f"{f.relpath}: alarm {alarm_id!r} refers to unknown tag {tag!r}")


def _cross_check(files: list[ProjectFile], problems: list[str]) -> None:
    devices = _check_devices(files, problems)
    tags = _check_tags(files, devices, problems)
    _check_alarms(files, tags, problems)


def load_project(root: Path) -> Project:
    project = Project(root=root)
    for rel, kind, path in _discover(root, project.problems):
        try:
            raw = load(path.read_text(encoding="utf-8"), rel)
        except (CanonicalError, UnicodeDecodeError) as exc:
            project.problems.append(str(exc))
            continue
        errors = validate(kind, raw)
        project.problems.extend(f"{rel}: {error}" for error in errors)
        if not errors:
            project.files.append(ProjectFile(rel, kind, path, omit_defaults(kind, raw)))
    if project.ok:
        _cross_check(project.files, project.problems)
    return project


def format_project(project: Project, *, check: bool) -> list[str]:
    """정규형과 다른 파일의 상대 경로 목록. check=False면 정규형으로 다시 쓴다."""
    changed: list[str] = []
    for f in project.files:
        canonical = dump(f.data).encode("utf-8")
        if f.path.read_bytes() != canonical:
            changed.append(f.relpath)
            if not check:
                f.path.write_bytes(canonical)
    return changed

"""Recent-log inventory + tail extraction for gh-log-triage-pr.

Usage:
    python skills/gh-log-triage-pr/scripts/collect_recent_logs.py [--hours N]
        [--tail-lines N] [--list-all] [--emit-dir DIR]

Scans the known log roots for files modified within the window and prints
a JSON manifest to stdout. Large files are captured tail-only, with
truncation recorded in the manifest so possible omissions are reportable.

Recency semantics: recency = file MTIME. Filename dates (YYYY-MM-DD.log)
are NOT consulted. On a fresh checkout, mtime reflects checkout time, not
when the log content was produced.

Self-output exclusion: anything under .omc/state/log-triage/ and any file
whose name starts with "log-triage-" is never collected (prevents the
scheduled run from re-triaging its own previous output).

stdlib-only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
from pathlib import Path

LOG_ROOT_GLOBS = [
    "logs/**",
    "backend/**/logs/**",
    "frontend/**/logs/**",
    ".omc/logs/**",
]

# Only files that plausibly ARE logs (avoids collecting e.g. python
# modules living in a package directory named "logs/").
LOG_EXTENSIONS = {".log", ".jsonl", ".txt", ".out", ".err"}

# Hardcoded self-output exclusion (see module docstring).
EXCLUDED_DIR_PARTS = (".omc", "state", "log-triage")
EXCLUDED_NAME_PATTERN = "log-triage-*"

DEFAULT_HOURS = 24
DEFAULT_TAIL_LINES = 500
TAIL_THRESHOLD_BYTES = 256 * 1024  # files above this are captured tail-only


def is_excluded(path: Path) -> bool:
    parts = path.parts
    for i in range(len(parts) - len(EXCLUDED_DIR_PARTS) + 1):
        if tuple(parts[i:i + len(EXCLUDED_DIR_PARTS)]) == EXCLUDED_DIR_PARTS:
            return True
    return fnmatch.fnmatch(path.name, EXCLUDED_NAME_PATTERN)


def find_log_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in LOG_ROOT_GLOBS:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if ".git" in p.parts:
                continue
            if p.suffix.lower() not in LOG_EXTENSIONS:
                continue
            if is_excluded(p.relative_to(root)):
                continue
            found.add(p)
    return sorted(found)


def tail_lines(path: Path, n: int) -> tuple[list[str], bool]:
    """Return (last n lines, truncated?). Reads at most ~4x the needed bytes."""
    size = path.stat().st_size
    approx_bytes = min(size, max(n * 512, 64 * 1024))
    with open(path, "rb") as fh:
        fh.seek(size - approx_bytes)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > approx_bytes:
        lines = lines[1:]  # drop the partial first line
    truncated = len(lines) > n or size > approx_bytes
    return lines[-n:], truncated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                    help=f"collection window in hours by mtime (default {DEFAULT_HOURS})")
    ap.add_argument("--tail-lines", type=int, default=DEFAULT_TAIL_LINES,
                    help=f"lines kept for large files (default {DEFAULT_TAIL_LINES})")
    ap.add_argument("--list-all", action="store_true",
                    help="also list files OUTSIDE the window (inventory when the window is empty)")
    ap.add_argument("--emit-dir", type=Path, default=None,
                    help="write captured tails as files into this directory")
    ap.add_argument("--root", type=Path, default=Path("."),
                    help="repo root (default: cwd)")
    args = ap.parse_args()

    root = args.root.resolve()
    now = time.time()
    cutoff = now - args.hours * 3600

    included: list[dict] = []
    outside: list[dict] = []

    for path in find_log_files(root):
        st = path.stat()
        rel = path.relative_to(root).as_posix()
        entry = {
            "path": rel,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
            "size_bytes": st.st_size,
        }
        if st.st_mtime < cutoff:
            outside.append(entry)
            continue

        whole = st.st_size <= TAIL_THRESHOLD_BYTES
        if whole:
            entry.update(captured="whole", truncated=False)
        else:
            lines, truncated = tail_lines(path, args.tail_lines)
            entry.update(
                captured=f"tail:{len(lines)}",
                truncated=truncated,
                omission_note="earlier lines not captured; report possible omissions",
            )
            if args.emit_dir:
                args.emit_dir.mkdir(parents=True, exist_ok=True)
                out = args.emit_dir / (rel.replace("/", "__") + ".tail")
                out.write_text("\n".join(lines), encoding="utf-8")
                entry["tail_file"] = str(out)
        included.append(entry)

    manifest = {
        "window_hours": args.hours,
        "window_start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cutoff)),
        "window_end": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "recency_semantics": "mtime (filename dates not consulted)",
        "included": included,
        "included_count": len(included),
        "outside_window_count": len(outside),
    }
    if args.list_all:
        manifest["outside_window"] = outside

    json.dump(manifest, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

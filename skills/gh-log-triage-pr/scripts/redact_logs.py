"""Fail-closed best-effort log redaction for gh-log-triage-pr.

Usage:
    python skills/gh-log-triage-pr/scripts/redact_logs.py [FILE ...] [--jsonl]
    cat some.log | python skills/gh-log-triage-pr/scripts/redact_logs.py

Behavior:
- Plain text: any line matching a secret pattern is masked ENTIRELY
  (fail-closed: no attempt to keep the "safe" part of the line).
- Tokens that look high-entropy but match no known pattern are flagged
  with [ENTROPY?] (flag only, never masked - SCADA logs carry UUIDs,
  base64 payloads and device IDs that are legitimate).
- --jsonl: each line is parsed as JSON and redacted structurally by a
  key deny-list (nested keys included). Lines that fail to parse are
  masked entirely.

This is a BEST-EFFORT first-pass filter, not a safety guarantee.
Human review is required before posting output anywhere.

stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys

MASK = "[REDACTED LINE - {reason}]"
VALUE_MASK = "[REDACTED]"

# Patterns that mask the WHOLE line when matched (fail-closed).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api-key", re.compile(
        r"(sk|pk)-[A-Za-z0-9_\-]{16,}"
        r"|gh[opsu]_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
        r"|AKIA[0-9A-Z]{16}"
    )),
    ("authorization-header", re.compile(
        r"\b(authorization|proxy-authorization)\b\s*[:=]"
        r"|\bbearer\s+[A-Za-z0-9_\-\.=/+]{8,}"
        r"|\bbasic\s+[A-Za-z0-9+/=]{12,}",
        re.IGNORECASE,
    )),
    ("key-value-secret", re.compile(
        r"\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret|refresh[_-]?token|"
        r"session[_-]?id|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    )),
    ("cookie", re.compile(r"\b(set-cookie|cookie)\b\s*[:=]", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+")),
    ("dsn-password", re.compile(r"\b[a-z][a-z0-9+]*://[^\s/:@]+:[^\s/@]+@")),
    ("pem-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
ENTROPY_THRESHOLD = 4.0
# Common non-secret shapes to skip when entropy-flagging.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

DENY_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "authorization",
    "cookie", "api_key", "apikey", "access_token", "refresh_token",
    "credential", "credentials", "private_key", "client_secret",
    "session_id", "set-cookie",
}

BANNER = (
    "# --- redact_logs.py: best-effort filter - human review required "
    "before posting anywhere ---"
)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def flag_entropy(line: str) -> str:
    """Append [ENTROPY?] flag when a suspicious token is present (flag only)."""
    for tok in ENTROPY_TOKEN.findall(line):
        if UUID_RE.match(tok) or HEX_RE.match(tok):
            continue
        if shannon_entropy(tok) >= ENTROPY_THRESHOLD:
            return line + "  [ENTROPY?]"
    return line


def redact_text_line(line: str) -> str:
    for reason, pat in SECRET_PATTERNS:
        if pat.search(line):
            return MASK.format(reason=reason)
    return flag_entropy(line)


def redact_json_value(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: VALUE_MASK if str(k).lower().replace("-", "_") in DENY_KEYS
            else redact_json_value(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_json_value(v) for v in obj]
    if isinstance(obj, str):
        for _reason, pat in SECRET_PATTERNS:
            if pat.search(obj):
                return VALUE_MASK
    return obj


def redact_jsonl_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return line
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        # Fail closed: unparseable JSONL line is masked entirely.
        return MASK.format(reason="jsonl-parse-failure")
    return json.dumps(redact_json_value(parsed), ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="input files (default: stdin)")
    ap.add_argument("--jsonl", action="store_true",
                    help="treat input as JSONL and redact structurally by key")
    args = ap.parse_args()

    # Windows consoles may default to cp949 etc.; log content is arbitrary.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def process(stream) -> None:
        for raw in stream:
            line = raw.rstrip("\r\n")
            out = redact_jsonl_line(line) if args.jsonl else redact_text_line(line)
            print(out)

    if args.files:
        for path in args.files:
            with open(path, encoding="utf-8", errors="replace") as fh:
                process(fh)
    else:
        process(sys.stdin)

    print(BANNER)
    return 0


if __name__ == "__main__":
    sys.exit(main())

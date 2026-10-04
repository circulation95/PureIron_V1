"""SQL 스크립트 분해: 구문 단위 분리와 lint용 정제 텍스트.

따옴표 문자열('…', E'…'), 따옴표 식별자("…"), dollar-quote($tag$…$tag$), 줄 주석(--),
중첩 블록 주석(/* … */) 안의 `;`는 구문 경계가 아니다. legacy `gateway.py:66`
`_split_sql_statements`의 역할을 대신하되, 실패를 건너뛰지 않는 새 runner용으로 다시 쓴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass(frozen=True, slots=True)
class Statement:
    sql: str
    """실행할 원문(주석 포함, 끝 `;` 제외)."""
    code: str
    """lint용: 주석 제거, 문자열·식별자 내용은 비움, 공백 하나로 정리, 대문자."""


class _Scanner:
    def __init__(self, script: str) -> None:
        self.s = script
        self.i = 0
        self.statements: list[Statement] = []
        self._raw: list[str] = []
        self._code: list[str] = []

    def run(self) -> list[Statement]:
        s = self.s
        while self.i < len(s):
            ch = s[self.i]
            nxt = s[self.i + 1] if self.i + 1 < len(s) else ""
            if ch == "-" and nxt == "-":
                self._line_comment()
            elif ch == "/" and nxt == "*":
                self._block_comment()
            elif ch == "'":
                self._quoted("'", code_placeholder="''")
            elif ch == '"':
                self._quoted('"', code_placeholder='""')
            elif ch == "$" and (m := _DOLLAR_TAG.match(s, self.i)) is not None:
                self._dollar(m.group(0))
            elif ch == ";":
                self.i += 1
                self._flush()
            else:
                self._raw.append(ch)
                self._code.append(ch)
                self.i += 1
        self._flush()
        return self.statements

    def _line_comment(self) -> None:
        end = self.s.find("\n", self.i)
        end = len(self.s) if end == -1 else end
        self._raw.append(self.s[self.i : end])
        self._code.append(" ")
        self.i = end

    def _block_comment(self) -> None:
        depth = 0
        start = self.i
        s = self.s
        while self.i < len(s):
            if s.startswith("/*", self.i):
                depth += 1
                self.i += 2
            elif s.startswith("*/", self.i):
                depth -= 1
                self.i += 2
                if depth == 0:
                    break
            else:
                self.i += 1
        if depth != 0:
            raise SqlSyntaxError("unterminated block comment")
        self._raw.append(s[start : self.i])
        self._code.append(" ")

    def _quoted(self, quote: str, *, code_placeholder: str) -> None:
        start = self.i
        self.i += 1
        s = self.s
        while True:
            end = s.find(quote, self.i)
            if end == -1:
                raise SqlSyntaxError(f"unterminated {quote} literal")
            if s.startswith(quote * 2, end):
                self.i = end + 2
                continue
            if quote == "'" and self._is_escape_string(start) and _escaped(s, end):
                self.i = end + 1
                continue
            self.i = end + 1
            break
        self._raw.append(s[start : self.i])
        self._code.append(code_placeholder)

    def _is_escape_string(self, quote_pos: int) -> bool:
        return quote_pos > 0 and self.s[quote_pos - 1] in "eE"

    def _dollar(self, tag: str) -> None:
        start = self.i
        end = self.s.find(tag, self.i + len(tag))
        if end == -1:
            raise SqlSyntaxError(f"unterminated dollar-quoted string {tag}")
        self.i = end + len(tag)
        self._raw.append(self.s[start : self.i])
        self._code.append("''")

    def _flush(self) -> None:
        raw = "".join(self._raw).strip()
        code = " ".join("".join(self._code).split()).upper()
        self._raw.clear()
        self._code.clear()
        if code:
            self.statements.append(Statement(sql=raw, code=code))


def _escaped(s: str, pos: int) -> bool:
    backslashes = 0
    j = pos - 1
    while j >= 0 and s[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


class SqlSyntaxError(ValueError):
    pass


def split_statements(script: str) -> list[Statement]:
    """주석만 있는 구문은 버린다. 닫히지 않은 문자열·주석은 `SqlSyntaxError`."""
    return _Scanner(script).run()

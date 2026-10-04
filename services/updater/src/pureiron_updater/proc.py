"""외부 명령 실행(docker·docker compose). 테스트는 `Runner`를 fake로 바꿔 명령줄을 검사한다."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pureiron_updater.ports import CommandError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 600.0


@dataclass(frozen=True, slots=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_S, stdout: Path | None = None
    ) -> Result:
        """`stdout`을 주면 표준 출력을 그 파일로(대용량 pg_dump). 실패도 Result로 돌려준다."""
        ...


def subprocess_runner(
    argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_S, stdout: Path | None = None
) -> Result:
    log.info("run: %s", " ".join(argv))
    try:
        if stdout is None:
            done = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout, check=False
            )
            return Result(done.returncode, done.stdout, done.stderr)
        with stdout.open("wb") as sink:
            done_bytes = subprocess.run(
                list(argv), stdout=sink, stderr=subprocess.PIPE, timeout=timeout, check=False
            )
        return Result(done_bytes.returncode, "", done_bytes.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"timed out after {timeout:.0f}s: {' '.join(argv)}") from exc
    except OSError as exc:
        raise CommandError(f"cannot run {argv[0]}: {exc}") from exc


def check(result: Result, argv: Sequence[str]) -> str:
    """종료 코드 0이면 stdout, 아니면 `CommandError`(stderr 마지막 줄들 포함)."""
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise CommandError(f"exit {result.returncode}: {' '.join(argv)}\n{tail}")
    return result.stdout

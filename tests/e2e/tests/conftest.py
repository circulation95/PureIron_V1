"""e2e 단위 시험 공용 설정 — 하네스 경로와, 실제 환경을 건드리지 못하게 막는 autouse fixture.

`isolated_from_the_host`(M6-3a 리뷰 L8): 실행기·preflight·하네스 함수는 기본값으로 실제 환경을
읽는다(환경변수 `GITHUB_EVENT_NAME`·실제 시계, `nightly.recover` → WSL·foundation-ci·VM 삭제 등).
주입을 빠뜨린 시험이 CI(`schedule` 이벤트, 늦은 시각)나 개발 PC에서 실제 동작을 하지 않게:
① `GITHUB_EVENT_NAME`·`GITHUB_ACTIONS`·`PUREIRON_G2_*`·`PUREIRON_E2E_*` 환경변수를 지우고
② 실제 VM·WSL·Hyper-V·GitHub·Docker를 부르는 프로그램(`BLOCKED`)을 띄우려 하면 곧바로 실패시킨다.
프로그램 실행 단계에서 막으므로 함수 기본값에 미리 묶인 호출(`step_run(recover_env=recover)` 등)도
걸린다. 실패는 `pytest.fail`(BaseException) — 하네스의 `except OSError`·`except Exception`이
삼키지 않는다. 시험 안에서 pytest·python·git을 띄우는 것은 그대로 된다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

E2E = Path(__file__).resolve().parents[1]
for sub in ("gates", "site", "m2d", "tests"):
    sys.path.insert(0, str(E2E / sub))
sys.path.insert(0, str(E2E.parents[1] / "deploy" / "build"))

BLOCKED = frozenset(
    {"wsl", "powershell", "pwsh", "ssh", "scp", "gh", "docker", "qemu-img", "hyper-v"}
)
"""단위 시험에서 띄우면 안 되는 프로그램(확장자·경로를 뗀 이름, 소문자)."""
ENV_EXACT = ("GITHUB_EVENT_NAME", "GITHUB_ACTIONS")
ENV_PREFIXES = ("PUREIRON_G2_", "PUREIRON_E2E_")


def program(args: object) -> str:
    """`Popen` 인자의 프로그램 이름(경로·`.exe`를 떼고 소문자)."""
    head: object = args
    if isinstance(args, list | tuple):
        items = cast(list[object] | tuple[object, ...], args)
        head = items[0] if items else ""
    if isinstance(head, bytes):
        head = head.decode(errors="replace")
    words = str(head).split()
    return Path(words[0] if words else "").name.lower().removesuffix(".exe")


@pytest.fixture(autouse=True)
def isolated_from_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key in ENV_EXACT or key.startswith(ENV_PREFIXES):
            monkeypatch.delenv(key)
    original: Callable[..., None] = subprocess.Popen.__init__

    def guarded(self: object, args: object, *rest: Any, **kwargs: Any) -> None:
        name = program(args)
        if name in BLOCKED:
            pytest.fail(
                f"unit test tried to run {name!r} ({args!r}) — inject a fake instead "
                "(tests/e2e/tests/conftest.py isolated_from_the_host)"
            )
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)

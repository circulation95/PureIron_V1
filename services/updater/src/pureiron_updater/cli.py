"""`pureiron-site` — 현장 운영 명령(updater venv 엔트리포인트, plan M2).

    pureiron-site apply <pib>            # bundles/에 사본을 두고 적용(창 밖이면 verified 대기)
    pureiron-site bundle verify <pib>    # 현재 상태 기준 검증만(부작용 없음)
    pureiron-site status [--json]
    pureiron-site recover                # 끊긴 시도 수렴(systemd ExecStartPre와 같은 동작)
    pureiron-site run                    # 서비스 loop: recover → (poll →) advance, 주기 반복

종료 코드: 0 성공·대기·변경 없음, 1 거부·실패, 2 다른 Updater 실행 중·진행 중 시도, 3 설정 오류.
enroll·secret·report export·project export는 M2c 이후 범위다.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.verify import AttemptResult, GenState, VerifyContext, verify_bundle
from pureiron_updater import __version__
from pureiron_updater.compose import ComposeCli
from pureiron_updater.core import CoreHttp
from pureiron_updater.images import DockerCli
from pureiron_updater.phases import AttemptInProgressError, Deps, Updater
from pureiron_updater.poll import TOKEN_FILE, ControlApi, PollError, poll_once
from pureiron_updater.recover import recover
from pureiron_updater.settings import Settings, SettingsError
from pureiron_updater.state import (
    BusyError,
    StateError,
    StateStore,
    UpdaterState,
    process_lock,
    to_json,
)

log = logging.getLogger("pureiron_updater")

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_CONFIG = 0, 1, 2, 3
CHUNK = 1024 * 1024


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def real_deps(settings: Settings) -> Deps:
    return Deps(
        docker=DockerCli(),
        compose=ComposeCli(),
        core=CoreHttp(settings.core_url, settings.secrets_dir / "updater_token"),
        clock=SystemClock(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pureiron-site")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    apply = sub.add_parser("apply", help="서명 번들 적용")
    apply.add_argument("pib", type=Path)
    bundle = sub.add_parser("bundle", help="번들 검사")
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)
    verify = bundle_sub.add_parser("verify", help="현재 상태 기준 검증(부작용 없음)")
    verify.add_argument("pib", type=Path)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    sub.add_parser("recover")
    sub.add_parser("run")
    return parser


def _copy_in(source: Path, bundles: Path) -> tuple[Path, str]:
    """번들을 `bundles/`에 복사(USB 분리 뒤에도 복구 가능)하며 sha256을 계산한다."""
    bundles.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with (
        source.open("rb") as src,
        tempfile.NamedTemporaryFile(dir=bundles, suffix=".part", delete=False) as dst,
    ):
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    sha = "sha256:" + digest.hexdigest()
    final = bundles / f"apply-{digest.hexdigest()[:16]}.pib"
    Path(dst.name).replace(final)
    return final, sha


def _summary(state: UpdaterState) -> str:
    attempt = state.attempt
    lines = [
        f"applied generation: {state.applied_gen}",
        f"last attempted:     {state.last_attempted_gen} ({state.last_result})",
        f"schema epoch:       {state.schema_epoch}",
    ]
    if attempt is not None:
        lines.append(f"attempt:            g{attempt.generation} {attempt.phase.value}")
    if state.alarm:
        lines.append(f"ALARM (manual intervention): {state.alarm}")
    return "\n".join(lines)


def _outcome(before: UpdaterState, after: UpdaterState) -> int:
    print(_summary(after))
    if after.attempt is not None:
        return EXIT_OK  # 창 대기·일시 장애로 멈춤 — 서비스 loop가 이어 간다
    if after.last_result in (AttemptResult.FAILED, AttemptResult.REJECTED) and (
        after.last_attempted_gen != before.last_attempted_gen
        or after.last_result != before.last_result
    ):
        return EXIT_FAILED
    if after.skipped_bundles != before.skipped_bundles:
        return EXIT_FAILED
    return EXIT_OK


def _apply(settings: Settings, pib: Path) -> int:
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        recover(updater)
        before = updater.state
        bundle, sha = _copy_in(pib, settings.bundles_dir)
        updater.submit(bundle, sha)
        return _outcome(before, updater.advance())


def _verify_only(settings: Settings, pib: Path) -> int:
    state = StateStore(settings.state_dir).load()
    ctx = VerifyContext(
        root_pubkey=keys.read_public_key(settings.root_pubkey_file),
        site_id=settings.site_id,
        updater_version=settings.updater_version,
        trust=state.trust,
        generation=GenState(state.applied_gen, state.last_attempted_gen, state.last_result),
        schema_epoch=state.schema_epoch if state.applied_gen is not None else None,
    )
    try:
        result = verify_bundle(pib, ctx)
    except BundleRejectedError as exc:
        print(f"{exc.code.value}: {exc.detail}", file=sys.stderr)
        return EXIT_FAILED
    out = {
        "site_id": result.manifest.site_id,
        "generation": result.manifest.generation,
        "decision": result.generation.value,
        "schema": None if result.schema is None else result.schema.action.value,
        "verify_s": round(result.verify_s, 3),
    }
    print(json.dumps(out, sort_keys=True))
    return EXIT_OK


def _status(settings: Settings, *, as_json: bool) -> int:
    state = StateStore(settings.state_dir).load()
    text = json.dumps(to_json(state), indent=2, sort_keys=True, ensure_ascii=False)
    print(text if as_json else _summary(state))
    return EXIT_OK


def _recover(settings: Settings) -> int:
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        before = updater.state
        return _outcome(before, recover(updater))


def _control_api(settings: Settings) -> ControlApi | None:
    if settings.control_api is None:
        return None
    return ControlApi(settings.control_api, settings.site_id, settings.secrets_dir / TOKEN_FILE)


def _tick(settings: Settings, api: ControlApi | None, *, first: bool) -> None:
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        if first:
            recover(updater)
        if api is not None:
            try:
                poll_once(updater, api)
            except (PollError, OSError) as exc:
                log.warning("poll failed: %s", exc)
        updater.advance()


def _run(settings: Settings) -> int:
    api = _control_api(settings)
    first = True
    while True:
        try:
            _tick(settings, api, first=first)
            first = False
        except BusyError as exc:
            log.info("skipping tick: %s", exc)
        time.sleep(settings.poll_interval_s)


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
        command: str = args.command
        commands: dict[str, Callable[[], int]] = {
            "apply": lambda: _apply(settings, args.pib),
            "bundle": lambda: _verify_only(settings, args.pib),
            "status": lambda: _status(settings, as_json=args.json),
            "recover": lambda: _recover(settings),
            "run": lambda: _run(settings),
        }
        return commands[command]()
    except (BusyError, AttemptInProgressError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except (SettingsError, StateError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())

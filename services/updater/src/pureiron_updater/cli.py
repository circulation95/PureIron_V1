"""`pureiron-site` — 현장 운영 명령(updater venv 엔트리포인트, plan M2).

    pureiron-site apply <pib>            # bundles/에 사본을 두고 적용(창 밖이면 verified 대기)
    pureiron-site bundle verify <pib> [--kind install]   # 현재 상태 기준 검증만(부작용 없음)
    pureiron-site status [--json]
    pureiron-site recover                # 끊긴 시도 수렴(서비스 첫 tick과 같은 동작)
    pureiron-site run                    # 서비스 loop: recover → (poll → 송신 →) advance
    pureiron-site enroll --site <id> [--re-enroll] [--next-seq N] [--out FILE]
    pureiron-site secret set <name> [--generate] [--if-missing]   # 값은 stdin(한 줄)
    pureiron-site report export --out DIR                          # 폐쇄망 보고 묶음(USB)
    pureiron-site project export --out draft.tar                   # 서명된 현장 드래프트(USB)

종료 코드: 0 성공·대기·변경 없음, 1 거부·실패, 2 다른 Updater 실행 중·진행 중 시도, 3 설정 오류.
`enroll`·`secret set`·`report export`·`project export`·`bundle verify --kind install`은
`PUREIRON_SITE_ID` 없이 쓴다(등록 전이거나, 현장 id를 `state/site.json`에서 읽는다).
설정은 프로세스 환경 + `/etc/pureiron/updater.env`(`settings.py`). `project export`는 등록 뒤에만
동작하고 드래프트 형식은 `draft.py`(M5-2).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.manifest import BundleKind
from pureiron_bundle.verify import (
    AttemptResult,
    GenState,
    VerifyContext,
    verify_bundle,
    verify_install_bundle,
)
from pureiron_updater import __version__
from pureiron_updater.compose import ComposeCli
from pureiron_updater.core import CoreHttp
from pureiron_updater.draft import DraftError, export_draft
from pureiron_updater.enroll import EnrollError, enroll, load_identity
from pureiron_updater.images import DockerCli
from pureiron_updater.metrics import result_of
from pureiron_updater.outbox import export_reports, pending
from pureiron_updater.phases import AttemptInProgressError, Deps, Updater
from pureiron_updater.poll import TOKEN_FILE, ControlApi, PollError, poll_once
from pureiron_updater.recover import recover
from pureiron_updater.secret_store import generate_secret, set_secret, value_from_input
from pureiron_updater.send import send_all
from pureiron_updater.settings import Settings, SettingsError, merged_env
from pureiron_updater.state import (
    TERMINAL,
    BusyError,
    Phase,
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
    verify.add_argument("--kind", choices=[k.value for k in BundleKind], default="site")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    sub.add_parser("recover")
    sub.add_parser("run")
    enroll_cmd = sub.add_parser("enroll", help="site 키 생성·enrollment 파일 출력")
    enroll_cmd.add_argument("--site", required=True)
    enroll_cmd.add_argument("--re-enroll", action="store_true", help="새 키쌍(키 분실·유출)")
    enroll_cmd.add_argument(
        "--next-seq", type=int, default=None, help="벤더 rekey 응답 next_seq(카운터만 앞으로)"
    )
    enroll_cmd.add_argument("--out", type=Path, default=None, help="enrollment 사본(USB 반출)")
    secret = sub.add_parser("secret", help="secret 파일")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_sub.add_parser("set", help="state/secrets/<name>(0600), 값은 stdin")
    secret_set.add_argument("name")
    secret_set.add_argument("--generate", action="store_true", help="난수 값 생성")
    secret_set.add_argument("--if-missing", action="store_true", help="이미 있으면 두기")
    report = sub.add_parser("report", help="보고")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    export = report_sub.add_parser("export", help="대기 보고를 서명 묶음으로(report_seq)")
    export.add_argument("--out", type=Path, required=True)
    project = sub.add_parser("project", help="현장 프로젝트")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    draft = project_sub.add_parser(
        "export", help="현장 드래프트(운영 값 PATCH가 실린 유효 트리)를 site key 서명 파일로"
    )
    draft.add_argument("--out", type=Path, required=True, help="draft.tar 경로(USB 반출)")
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


def _outcome(updater: Updater, sha256: str | None = None) -> int:
    """판정(0 성공·대기·no-op, 1 실패·거부). `sha256`을 주면 그 번들의 시도만 본다 — 대체된
    이전 시도(`E_SUPERSEDED`)나 마무리만 한 이전 시도는 이번 apply의 결과가 아니다."""
    state = updater.state
    print(_summary(state))
    attempt = state.attempt
    if (
        attempt is not None
        and attempt.phase in TERMINAL
        and sha256 in (None, attempt.bundle_sha256)
    ):
        # 결과는 났지만 잠금을 아직 못 풀었다 — 서비스 loop가 마무리한다.
        return EXIT_OK if attempt.phase is Phase.SUCCEEDED else EXIT_FAILED
    mine = [a for a in updater.finished if sha256 in (None, a.bundle_sha256)]
    failed = [a for a in mine if result_of(a) is not AttemptResult.SUCCEEDED]
    return EXIT_FAILED if failed else EXIT_OK


def _apply(settings: Settings, pib: Path) -> int:
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        recover(updater)
        bundle, sha = _copy_in(pib, settings.bundles_dir)
        updater.submit(bundle, sha)
        updater.advance()
        return _outcome(updater, sha)


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


def _verify_install(settings: Settings, pib: Path) -> int:
    """설치 번들 재검증(부트스트랩 5a). 기존 호스트(Updater 교체)는 저장된 trust 상태 기준."""
    state = StateStore(settings.state_dir).load()
    root = keys.read_public_key(settings.root_pubkey_file)
    try:
        result = verify_install_bundle(pib, root, state.trust)
    except BundleRejectedError as exc:
        print(f"{exc.code.value}: {exc.detail}", file=sys.stderr)
        return EXIT_FAILED
    out = {
        "kind": BundleKind.INSTALL.value,
        "release": result.manifest.release,
        "updater": result.manifest.updater_version,
        "files": len(result.files),
        "verify_s": round(result.verify_s, 3),
    }
    print(json.dumps(out, sort_keys=True))
    return EXIT_OK


def _enroll(settings: Settings, args: argparse.Namespace) -> int:
    with process_lock(settings.state_dir):
        path = enroll(
            settings,
            args.site,
            now=datetime.now(UTC),
            re_enroll=args.re_enroll,
            next_seq=args.next_seq,
        )
        identity = load_identity(settings.state_dir)
    out: Path | None = args.out
    if out is not None:
        shutil.copyfile(path, out)
    if identity is not None:
        print(f"site:        {identity.site_id}")
        print(f"fingerprint: {identity.fingerprint}")
        print(f"next seq:    {identity.next_report_seq}")
    print(f"enrollment:  {out or path}")
    return EXIT_OK


def _secret_set(settings: Settings, args: argparse.Namespace) -> int:
    value = generate_secret() if args.generate else value_from_input(sys.stdin.buffer.read())
    written = set_secret(
        settings.secrets_dir,
        args.name,
        value,
        uid=settings.secret_uid,
        if_missing=args.if_missing,
    )
    print(f"{args.name}: {'written' if written else 'kept (already set)'}")
    return EXIT_OK


def _report_export(settings: Settings, out: Path) -> int:
    """대기 보고가 없을 때까지 내보낸다 — 끊긴 export를 먼저 끝내고 그 뒤 쌓인 보고도 묶는다."""
    written = 0
    with process_lock(settings.state_dir):
        while (result := export_reports(settings, out, now=datetime.now(UTC))) is not None:
            path, count = result
            print(f"{count} report(s) → {path}")
            written += 1
    if not written:
        print("no pending reports")
    return EXIT_OK


def _project_export(settings: Settings, out: Path) -> int:
    with process_lock(settings.state_dir):
        try:
            result = export_draft(settings, real_deps(settings).compose, out, now=datetime.now(UTC))
        except DraftError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED
    payload = result.payload
    print(f"draft:         {result.path}")
    print(f"site:          {payload['site_id']}")
    print(f"site_rev:      {payload['site_rev']} (base {payload['base_site_rev']})")
    print(f"content_hash:  {payload['content_hash']}")
    if not result.has_drafts:
        print("note: no site drafts since the last applied bundle (site_rev == base_site_rev)")
    return EXIT_OK


def _status(settings: Settings, *, as_json: bool) -> int:
    state = StateStore(settings.state_dir).load()
    if as_json:
        print(json.dumps(to_json(state), indent=2, sort_keys=True, ensure_ascii=False))
        return EXIT_OK
    print(_summary(state))
    print(f"reports pending:    {len(pending(settings.state_dir))}")
    return EXIT_OK


def _recover(settings: Settings) -> int:
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        recover(updater)
        return _outcome(updater)


def _control_api(settings: Settings) -> ControlApi | None:
    if settings.control_api is None:
        return None
    return ControlApi(settings.control_api, settings.site_id, settings.secrets_dir / TOKEN_FILE)


def tick(settings: Settings, api: ControlApi | None, *, first: bool) -> None:
    """서비스 loop 한 번: 복구 규칙(창) 적용 → poll → heartbeat → 보고 → advance. 재검증은 첫
    tick에만. 송신 단계의 실패는 로그만 남긴다(`send.send_all` — apply를 막지 않는다)."""
    with process_lock(settings.state_dir):
        updater = Updater(settings, real_deps(settings))
        recover(updater, reverify=first)
        if api is not None:
            try:
                poll_once(updater, api)
            except (PollError, OSError) as exc:
                log.warning("poll failed: %s", exc)
            send_all(settings, updater.state, api, now=datetime.now(UTC))
        updater.advance()


def _run(settings: Settings) -> int:
    api = _control_api(settings)
    first = True
    while True:
        try:
            tick(settings, api, first=first)
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
    command: str = args.command
    install_verify = command == "bundle" and args.kind == BundleKind.INSTALL
    try:
        settings = Settings.from_env(
            merged_env(os.environ),
            require_site=not (
                install_verify or command in ("enroll", "secret", "report", "project")
            ),
        )
        commands: dict[str, Callable[[], int]] = {
            "apply": lambda: _apply(settings, args.pib),
            "bundle": lambda: (_verify_install if install_verify else _verify_only)(
                settings, args.pib
            ),
            "status": lambda: _status(settings, as_json=args.json),
            "recover": lambda: _recover(settings),
            "run": lambda: _run(settings),
            "enroll": lambda: _enroll(settings, args),
            "secret": lambda: _secret_set(settings, args),
            "report": lambda: _report_export(settings, args.out),
            "project": lambda: _project_export(settings, args.out),
        }
        return commands[command]()
    except (BusyError, AttemptInProgressError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except (SettingsError, StateError, EnrollError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())

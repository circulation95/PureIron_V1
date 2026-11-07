"""deploy/central compose를 실제로 띄우는 control-web e2e 준비·정리(M4-3 G8).

    uv run python deploy/tests/central_stack.py up --out <stack.json>
    uv run python deploy/tests/central_stack.py down --state <stack.json>

`up`은 운영 절차(deploy/central/README.md)를 그대로 밟는다:
비밀 파일 3개 → `run --rm control-api migrate up` → CLI로 viewer·ci 토큰 발급 →
실제 Updater 코드(`pureiron_updater.enroll`)로 만든 enrollment를 CLI `site register`로 등록 →
`up -d --wait` → Caddy 루트 CA 내보내기 → 그 CA로 검증하는 HTTPS로
`PUT /api/v1/bundles/<site>/1`(ci 토큰, basic auth 없음 — G3).
heartbeat는 서명만 해 파일로 두고 보내지 않는다: online → stale(3 x poll_s = 6s) 전환을
시험이 직접 재도록 Playwright가 보내기 직전에 보낸다.

compose 프로젝트·포트는 다른 게이트·리허설과 겹치지 않게 env로 바꾼다:
- `PUREIRON_IT_PROJECT`(기본 pureiron-it → 프로젝트 `<값>-central`)
- `PUREIRON_IT_CENTRAL_PORT`(기본 28543), `PUREIRON_RELEASE`(이미지 태그, build_images.sh와 같은 값)
- `PUREIRON_IT_KEEP=1`이면 `down`이 아무것도 지우지 않는다(디버깅).
비밀·작업 파일은 임시 디렉터리에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.manifest import MANIFEST_FILE, SITE_IMAGES
from pureiron_bundle.pib import CERT_FILE, CERT_SIG, MANIFEST_SIG, write_pib
from pureiron_bundle.sitesign import Json, SitePurpose, dumps, sign_document
from pureiron_updater.enroll import enroll
from pureiron_updater.settings import Settings as UpdaterSettings

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "deploy" / "central" / "compose.yml"
PROJECT = os.environ.get("PUREIRON_IT_PROJECT", "pureiron-it") + "-central"
PORT = os.environ.get("PUREIRON_IT_CENTRAL_PORT", "28543")
RELEASE = os.environ.get("PUREIRON_RELEASE", "0.1.0-dev")
HOST = "localhost"
POLL_S = "2"
SITE_ID = "site-e2e"
GENERATION = 1
PRODUCT = "2026.11.0"
PROJECT_VERSION = "v1"
UI_USER = "ops"
# 현장 시계의 보고 시각(중앙 수신 시각 last_seen과 일부러 다른 날) — 두 시각을 따로 보이는지 본다.
REPORTED_AT = "2026-11-01T09:00:00+00:00"
CA_IN_CONTAINER = "/data/caddy/pki/authorities/local/root.crt"
READY_TIMEOUT_S = 120.0
COMMAND_TIMEOUT_S = 600.0


def _env(secrets_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PUREIRON_CENTRAL_SECRETS_DIR": str(secrets_dir),
        "PUREIRON_CENTRAL_RELEASE": RELEASE,
        "PUREIRON_CENTRAL_HOST": HOST,
        "PUREIRON_CENTRAL_BIND": "127.0.0.1",
        "PUREIRON_CENTRAL_HTTPS_PORT": PORT,
        "PUREIRON_CONTROL_POLL_S": POLL_S,
    }


def compose(secrets_dir: Path, *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        env=_env(secrets_dir),
        input=stdin,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    out = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")
        command = " ".join(args)
        raise RuntimeError(f"docker compose {command} exited {result.returncode}\n{out}{err}")
    return out


def _cli_json(secrets_dir: Path, *args: str, stdin: bytes | None = None) -> dict[str, object]:
    """`pureiron-control <args> --json`(control-api 이미지 진입점, compose run). 끝 줄이 JSON."""
    out = compose(secrets_dir, "run", "--rm", "-T", "control-api", *args, "--json", stdin=stdin)
    return cast(dict[str, object], json.loads(out.strip().splitlines()[-1]))


def _write_secret(path: Path, value: str) -> None:
    # 컨테이너 사용자(control-api UID 10001)가 bind mount로 읽는다 — 임시 디렉터리 안이라 0644.
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o644)


def _basic_auth_line(password: str) -> str:
    hashed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "caddy",
            f"pureiron/control-web:{RELEASE}",
            "hash-password",
            "--plaintext",
            password,
        ],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=True,
    ).stdout.strip()
    return f"{UI_USER} {hashed}"


def _manifest() -> Json:
    """control-api가 받는 형식 그대로의 manifest(서명은 가짜 — 확인은 현장 Updater 몫)."""
    return {
        "format": 1,
        "kind": "site",
        "site_id": SITE_ID,
        "generation": GENERATION,
        "created_at": "2026-11-01T00:00:00Z",
        "product_release": {
            "version": PRODUCT,
            "min_updater": "0.1.0",
            "images": {
                svc: {"config_digest": "sha256:" + "a" * 64, "tag": f"pureiron/{svc}:r5"}
                for svc in SITE_IMAGES
            },
            "schema": {
                "target": 5,
                "app_expected": 5,
                "app_max_compatible": 6,
                "compat_app_min": "2026.10.0",
            },
        },
        "project": {
            "project_id": "e2e",
            "version": PROJECT_VERSION,
            "content_hash": "sha256:" + "0" * 64,
        },
        "window": {
            "once": {"start": "2026-11-01T10:00:00+09:00", "end": "2026-11-01T11:00:00+09:00"}
        },
        "files": {},
        "fleet_commit": "e2e0c0ffee",
    }


def _pib(out: Path) -> bytes:
    metadata = {
        MANIFEST_FILE: json.dumps(_manifest()).encode(),
        MANIFEST_SIG: b"\1" * 64,
        CERT_FILE: b"{}",
        CERT_SIG: b"\2" * 64,
    }
    write_pib(out, metadata, {})
    return out.read_bytes()


def _heartbeat(seed: bytes) -> bytes:
    payload: dict[str, Json] = {
        "site_id": SITE_ID,
        "report_seq": 1,
        "current": {"product": PRODUCT, "project_version": PROJECT_VERSION, "gen": GENERATION},
        "updater_phase": "idle",
        "disk_state": "ok",
        "at": REPORTED_AT,
    }
    return dumps(sign_document(seed, SitePurpose.HEARTBEAT, payload))


def _export_ca(secrets_dir: Path, target: Path) -> ssl.SSLContext:
    """README의 루트 CA 내보내기 절차 그대로. Caddy가 첫 인증서를 만들 때까지 기다린다."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            compose(secrets_dir, "cp", f"control-web:{CA_IN_CONTAINER}", str(target))
            context = ssl.create_default_context(cafile=str(target))
            with urllib.request.urlopen(
                f"https://{HOST}:{PORT}/api/v1/healthz", context=context, timeout=3
            ) as response:
                if response.status == 200:
                    return context
        except (RuntimeError, OSError, ssl.SSLError) as exc:
            last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"central HTTPS not ready within {READY_TIMEOUT_S}s (last: {last})")


def _put_bundle(context: ssl.SSLContext, ci_token: str, body: bytes) -> None:
    request = urllib.request.Request(
        f"https://{HOST}:{PORT}/api/v1/bundles/{SITE_ID}/{GENERATION}",
        data=body,
        method="PUT",
        headers={"Authorization": f"Bearer {ci_token}", "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            if response.status not in {200, 201}:
                raise RuntimeError(f"PUT bundle: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"PUT bundle: HTTP {exc.code} {exc.read()!r}") from exc


def up(out: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="pureiron-central-e2e-"))
    # 중간에 실패해도 `down --state`가 이 작업 디렉터리를 지우도록 먼저 기록한다(끝에서 덮어쓴다).
    out.write_text(json.dumps({"work": str(work)}), encoding="utf-8")
    secrets_dir = work / "secrets"
    secrets_dir.mkdir()
    password = secrets.token_urlsafe(12)
    _write_secret(secrets_dir / "control_db_password", secrets.token_urlsafe(24))
    _write_secret(secrets_dir / "viewer_token", "not-issued-yet")
    _write_secret(secrets_dir / "ui_basic_auth", _basic_auth_line(password))
    compose(secrets_dir, "down", "-v", "--remove-orphans")
    compose(secrets_dir, "config", "--quiet")
    compose(secrets_dir, "run", "--rm", "-T", "control-api", "migrate", "up")
    viewer = _cli_json(secrets_dir, "token", "create", "--role", "viewer", "--name", "control-web")
    _write_secret(secrets_dir / "viewer_token", str(viewer["token"]))
    ci = _cli_json(secrets_dir, "token", "create", "--role", "ci", "--name", "e2e-ci")

    site = UpdaterSettings(
        site_id=SITE_ID,
        root=work / "site",
        root_pubkey_file=work / "site" / "root.pub",
        core_url="http://127.0.0.1:9/api/v1",
        env_file=work / "site" / "updater.env",
    )
    enrollment = enroll(site, SITE_ID, now=datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    registered = _cli_json(
        secrets_dir, "site", "register", "/dev/stdin", stdin=enrollment.read_bytes()
    )
    heartbeat = work / "heartbeat.json"
    heartbeat.write_bytes(_heartbeat(keys.read_private_key(site.site_key_file)))

    compose(secrets_dir, "up", "-d", "--wait")
    context = _export_ca(secrets_dir, work / "central-root.crt")
    _put_bundle(context, str(ci["token"]), _pib(work / "g1.pib"))

    state: dict[str, object] = {
        "work": str(work),
        "base_url": f"https://{HOST}:{PORT}",
        "user": UI_USER,
        "password": password,
        "site": SITE_ID,
        "site_token": registered["token"],
        "heartbeat_file": str(heartbeat),
        "reported_at": REPORTED_AT,
        "generation": GENERATION,
        "poll_s": float(POLL_S),
        "project": PROJECT,
    }
    out.write_text(json.dumps(state), encoding="utf-8")
    print(f"central stack up: {PROJECT} on {state['base_url']}")


def down(state_file: Path | None) -> None:
    work = None
    if state_file is not None and state_file.exists():
        work = Path(str(json.loads(state_file.read_text(encoding="utf-8"))["work"]))
    if os.environ.get("PUREIRON_IT_KEEP") == "1":
        print(f"PUREIRON_IT_KEEP=1: leaving {PROJECT} up (work dir {work})")
        return
    secrets_dir = work / "secrets" if work is not None else Path(tempfile.gettempdir())
    compose(secrets_dir, "down", "-v", "--remove-orphans")
    if work is not None:
        shutil.rmtree(work, ignore_errors=True)


def logs(state_file: Path) -> str:
    work = Path(str(json.loads(state_file.read_text(encoding="utf-8"))["work"]))
    return compose(work / "secrets", "logs", "--no-color", "--timestamps")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up").add_argument("--out", type=Path, required=True)
    sub.add_parser("down").add_argument("--state", type=Path, default=None)
    sub.add_parser("logs").add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "up":
        up(args.out)
    elif args.command == "down":
        down(args.state)
    else:
        sys.stdout.write(logs(args.state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

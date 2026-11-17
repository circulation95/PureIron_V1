"""M4 마감 VM 온라인 단계의 중앙(c0) — WSL에서 `deploy/central` compose를 띄우고 다룬다.

    uv run --no-sync python tests/e2e/gates/harness/central.py up [--ca-out PATH]
    uv run --no-sync python tests/e2e/gates/harness/central.py register --enrollment PATH
    uv run --no-sync python tests/e2e/gates/harness/central.py site-token --site site-a  # 원문
    uv run --no-sync python tests/e2e/gates/harness/central.py put --site site-a --gen 2 --pib PATH
    uv run --no-sync python tests/e2e/gates/harness/central.py fleet [--site site-a] [--direct]
    uv run --no-sync python tests/e2e/gates/harness/central.py fleet --ui   # control-web 경로
    uv run --no-sync python tests/e2e/gates/harness/central.py offline-import --file PATH
    uv run --no-sync python tests/e2e/gates/harness/central.py stop-web | start-web | status | down
    uv run --no-sync python tests/e2e/gates/harness/central.py dump --name c1-<token>
    uv run --no-sync python tests/e2e/gates/harness/central.py restore --name c1-<token>

`m2d/run.py online`(Windows 호스트)이 `golden.wsl`로 부른다. 운영 절차(deploy/central/README.md)를
그대로 밟는다: **`up`은 항상 새로 시작한다**(같은 compose 프로젝트를 볼륨째 내리고) → 비밀 파일
→ `run --rm control-api migrate up`(기동 때 자동 실행하지 않는다) → CLI로 admin·ci·viewer 토큰
발급 → `up -d --wait` → Caddy 루트 CA 내보내기·SHA-256 지문 → 그 CA로 검증하는 HTTPS `healthz`
200. 새로 시작하는 이유: VM 체크포인트 c0의 enrollment는 실행마다 새로 등록해야 하고(같은 키
재등록은 409), 지난 실행의 게시·보고가 판정에 섞이지 않아야 한다.

- 값(D9): compose 프로젝트 `pureiron-e2e-central`, release `c0`(이미지는
  `deploy/releases/central/c0.json` config digest와 같아야 하고, 없거나 다르면 image store
  `central/c0/*.tar`에서 적재), 바인드·인증서 이름·SNI 모두 `10.77.0.1`(내부 전용 vSwitch 호스트
  주소 — LAN에 열리지 않는다), HTTPS 28443, `PUREIRON_CONTROL_POLL_S=10`(현장 Updater poll
  주기와 같게 — online = 마지막 수신 ≤ 30 s).
- **비밀값은 파일로만**: `~/pureiron-e2e-central/secrets`(0700, 저장소·artifacts 밖). 토큰
  원문은 stdout에 내지 않는다 — 예외는 `site-token`(호출자가 VM `pureiron-site secret set`의
  stdin으로 바로 넘긴다). 다른 명령의 출력(JSON)에는 파일 경로·지문·상태만 있다. 파일:
  `control_db_password`·`viewer_token`(compose 비밀 — Caddy `/ui/fleet`)·`ui_basic_auth`(compose
  비밀), `admin_token`·`ci_token`(publish `PUT /bundles`)·`publish_viewer_token`(publish·
  check-gen `GET /fleet`)·`ui_password`(control-web 화면 basic auth 원문)·`<site>.token`(현장
  token, `register`가 씀).
- `dump`·`restore`(M6-4a ★P1 c1 쌍): control-db 전체를 `pg_dump -Fc`로
  `~/pureiron-e2e-central/dumps/<이름>.dump`(0600, 디렉터리 0700 — 비밀 디렉터리와 같은 규칙, 토큰
  해시가 들어 있다)에, 그때의 `/fleet` 요약(`fleet_view`)을 `<이름>.fleet.json`에 둔다. `restore`는
  control-web·control-api를 멈추고 `pg_restore --clean --single-transaction` → control-api만 시작 →
  `/fleet`(control-api 직접)이 덤프 때 요약과 같은지 본다(다르면 실패). control-web은 멈춘 채로 둔다
  — 부른 쪽이 VM 체크포인트를 되돌린 뒤 `start-web`한다(되돌리기 전의 VM이 복원된 중앙에 보고하지
  않게). 번들 파일(볼륨 `control-data`)은 되돌리지 않는다: DB에 없는 gen 파일은 쓰이지 않고 같은
  gen을 다시 게시하면 덮어쓴다. `up`이 작업 디렉터리를 지우므로 덤프는 그 세션 것뿐이다.
- `down`은 compose를 볼륨째 내리고 작업 디렉터리(비밀 포함)를 지운다. 15분 실측(코디네이터)은
  `run.py online --keep-central`로 남긴 중앙을 그대로 쓰고 끝나면 `down`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "deploy" / "build"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from images import image_store

from harness.bundles import ensure_image, local_digest

ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / "deploy" / "central" / "compose.yml"
RECORD_DIR = ROOT / "deploy" / "releases" / "central"
PROJECT = "pureiron-e2e-central"
RELEASE = "c0"
HOST = "10.77.0.1"
PORT = 28443
POLL_S = 10
BASE_URL = f"https://{HOST}:{PORT}/api/v1"
WORK = Path.home() / "pureiron-e2e-central"
SECRETS = WORK / "secrets"
CA_FILE = WORK / "central-root.crt"
"""Caddy 로컬 CA 루트 인증서(공개값 — 비밀 디렉터리 밖)."""
CA_IN_CONTAINER = "/data/caddy/pki/authorities/local/root.crt"
CENTRAL_SERVICES = ("control-api", "control-web")
UI_USER = "ops"
TOKENS = {
    "admin": ("admin", "e2e-admin"),
    "ci": ("ci", "publish-ci"),
    "publish_viewer": ("viewer", "publish-viewer"),
}
"""파일 이름 접두(`<접두>_token`) → (역할, 토큰 이름). Caddy용 viewer(`viewer_token`)는 따로."""
DUMPS = WORK / "dumps"
DB_SERVICE = "control-db"
DB_USER = "pureiron"
DB_NAME = "pureiron_control"
DUMP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
FLEET_FIELDS = (
    ("desired", "generation"),
    ("desired", "sha256"),
    ("", "max_published_gen"),
    ("current", "gen"),
    ("current", "product"),
    ("", "last_heartbeat_seq"),
    ("", "last_report_seq"),
)
"""`/fleet` 행에서 복원 일치를 보는 값 — 게시·반영·수위(시각·연결 상태는 흐르므로 뺀다)."""
READY_TIMEOUT_S = 120.0
COMMAND_TIMEOUT_S = 600.0
HTTP_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 600.0


class CentralError(RuntimeError):
    pass


def compose_env() -> dict[str, str]:
    return {
        **os.environ,
        "PUREIRON_CENTRAL_SECRETS_DIR": str(SECRETS),
        "PUREIRON_CENTRAL_RELEASE": RELEASE,
        "PUREIRON_CENTRAL_HOST": HOST,
        "PUREIRON_CENTRAL_DEFAULT_SNI": HOST,
        "PUREIRON_CENTRAL_BIND": HOST,
        "PUREIRON_CENTRAL_HTTPS_PORT": str(PORT),
        "PUREIRON_CONTROL_POLL_S": str(POLL_S),
    }


def compose_raw(*args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
    """`docker compose -p pureiron-e2e-central -f deploy/central/compose.yml <args>`의 stdout(바이트
    — `pg_dump`). 실패하면 명령과 stderr를 담아 멈춘다 — 토큰을 출력하는 명령(`token create`·
    `site register`)은 `_cli_json`만 부르고 그 출력은 오류 메시지에 넣지 않는다."""
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        env=compose_env(),
        input=stdin,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if check and result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")
        raise CentralError(f"docker compose {' '.join(args)} exited {result.returncode}\n{err}")
    return result.stdout


def compose(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
    return compose_raw(*args, stdin=stdin, check=check).decode("utf-8", "replace")


def _cli_json(*args: str, stdin: bytes | None = None) -> dict[str, object]:
    """`pureiron-control <args> --json`(control-api 이미지, compose run). 끝 줄이 JSON. 출력에
    토큰이 있으므로 실패해도 stdout은 오류에 싣지 않는다(stderr만)."""
    command = ["run", "--rm", "-T", "control-api", *args, "--json"]
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *command],
        env=compose_env(),
        input=stdin,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")
        raise CentralError(f"pureiron-control {' '.join(args)} exited {result.returncode}\n{err}")
    lines = result.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        return cast(dict[str, object], json.loads(lines[-1]))
    except (IndexError, ValueError) as exc:
        raise CentralError(f"pureiron-control {' '.join(args)}: no JSON on the last line") from exc


def write_secret(path: Path, value: str, *, mode: int = 0o600) -> None:
    """한 줄 비밀 파일. compose 비밀(컨테이너 사용자가 bind mount로 읽음)은 0644 — 보호는 0700
    디렉터리(deploy/central/README.md "비밀 파일"). 같은 파일에 덮어쓴다(mv 금지 규칙과 같다)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    path.chmod(mode)


def read_secret(name: str) -> str:
    try:
        value = (SECRETS / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CentralError(f"{SECRETS / name} unreadable — central.py up 먼저") from exc
    if not value:
        raise CentralError(f"{SECRETS / name} is empty")
    return value


def docker_load(tar: Path) -> None:
    subprocess.run(["docker", "load", "-q", "-i", str(tar)], check=True, capture_output=True)


def ensure_images() -> dict[str, str]:
    """`pureiron/<svc>:c0`의 config digest를 release 기록 값으로(없거나 다르면 store tar 적재)."""
    record = json.loads((RECORD_DIR / f"{RELEASE}.json").read_text(encoding="utf-8"))
    images = cast(dict[str, dict[str, str]], record["images"])
    store = image_store() / "central" / RELEASE
    scratch = WORK / "digest-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    found: dict[str, str] = {}
    for svc in CENTRAL_SERVICES:
        want = images[svc]["config_digest"]
        ensure_image(
            f"pureiron/{svc}:{RELEASE}",
            store / f"{svc}.tar",
            want,
            digest_of=lambda tag: local_digest(tag, scratch),
            load=docker_load,
        )
        found[svc] = want
    shutil.rmtree(scratch, ignore_errors=True)
    return found


def basic_auth_line(password: str) -> str:
    """control-web 화면 basic auth 한 줄(`ops <bcrypt>`). 원문은 `caddy hash-password`의 stdin으로만
    넘긴다 — `--plaintext`면 `docker run` 인자라 호스트 `ps`·컨테이너 프로세스 인자에 보인다.
    stdin이 터미널이 아니면 caddy는 첫 줄을 읽고 끝 줄바꿈을 떼어 낸다(줄바꿈이 없으면 실패 —
    v2.11.7에서 확인)."""
    image = f"pureiron/control-web:{RELEASE}"
    hashed = (
        subprocess.run(
            ["docker", "run", "--rm", "-i", "--entrypoint", "caddy", image, "hash-password"],
            input=(password + "\n").encode(),  # 바이트 그대로(줄바꿈 변환 없음 — M6-4b-1 fix r2)
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
            check=True,
        )
        .stdout.decode("utf-8", "replace")
        .strip()
    )
    return f"{UI_USER} {hashed}"


def fingerprint(pem: str) -> str:
    """`openssl x509 -noout -fingerprint -sha256`와 같은 표기(대문자 hex, `:` 구분)."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(CA_FILE))


def healthz() -> int:
    with urllib.request.urlopen(f"{BASE_URL}/healthz", context=context(), timeout=5) as response:
        return response.status


def export_ca() -> str:
    """README의 루트 CA 내보내기 그대로(`compose cp`). Caddy가 첫 인증서를 만들 때까지 기다리고, 그
    CA로 검증하는 HTTPS `healthz` 200을 확인한다."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            compose("cp", f"control-web:{CA_IN_CONTAINER}", str(CA_FILE))
            if healthz() == 200:
                return CA_FILE.read_text(encoding="ascii")
        except (CentralError, OSError, ssl.SSLError) as exc:
            last = str(exc)
        time.sleep(1)
    raise CentralError(f"central HTTPS not ready within {READY_TIMEOUT_S}s (last: {last})")


def status() -> dict[str, object]:
    """비밀값 없는 상태 요약 — run.py가 결과에 남기고 코디네이터가 15분 실측에 넘긴다."""
    pem = CA_FILE.read_text(encoding="ascii") if CA_FILE.is_file() else None
    return {
        "project": PROJECT,
        "release": RELEASE,
        "base_url": BASE_URL,
        "poll_s": POLL_S,
        "ca_file": str(CA_FILE),
        "ca_sha256": fingerprint(pem) if pem else None,
        "secrets_dir": str(SECRETS),
        "token_files": {name: str(SECRETS / f"{name}_token") for name in (*TOKENS, "viewer")},
        "ui_user": UI_USER,
        "ui_password_file": str(SECRETS / "ui_password"),
    }


def up(ca_out: Path | None) -> dict[str, object]:
    images = ensure_images()
    compose("down", "-v", "--remove-orphans")
    shutil.rmtree(WORK, ignore_errors=True)
    SECRETS.mkdir(parents=True, mode=0o700)
    SECRETS.chmod(0o700)
    password = secrets.token_urlsafe(18)
    write_secret(SECRETS / "ui_password", password)
    write_secret(SECRETS / "control_db_password", secrets.token_urlsafe(24), mode=0o644)
    write_secret(SECRETS / "viewer_token", "not-issued-yet", mode=0o644)
    write_secret(SECRETS / "ui_basic_auth", basic_auth_line(password), mode=0o644)
    compose("config", "--quiet")
    compose("run", "--rm", "-T", "control-api", "migrate", "up")
    viewer = _cli_json("token", "create", "--role", "viewer", "--name", "control-web")
    write_secret(SECRETS / "viewer_token", str(viewer["token"]), mode=0o644)
    for prefix, (role, name) in TOKENS.items():
        created = _cli_json("token", "create", "--role", role, "--name", name)
        write_secret(SECRETS / f"{prefix}_token", str(created["token"]))
    compose("up", "-d", "--wait")
    pem = export_ca()
    if ca_out is not None:
        ca_out.parent.mkdir(parents=True, exist_ok=True)
        ca_out.write_text(pem, encoding="ascii")
    return {**status(), "images": images, "healthz": healthz(), "ca_out": str(ca_out or "")}


def register(enrollment: Path) -> dict[str, object]:
    """CLI `site register`(README 첫 기동 절) → 현장 token은 `<site>.token`(0600)에만."""
    done = _cli_json("site", "register", "/dev/stdin", stdin=enrollment.read_bytes())
    site = str(done["site_id"])
    write_secret(SECRETS / f"{site}.token", str(done["token"]))
    return {k: v for k, v in done.items() if k != "token"} | {
        "token_file": str(SECRETS / f"{site}.token")
    }


def _request(
    method: str,
    path: str,
    token_name: str,
    *,
    body: BinaryIO | None = None,
    length: int | None = None,
    content_type: str = "application/octet-stream",
) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {read_secret(token_name)}"}
    if length is not None:
        headers |= {"Content-Type": content_type, "Content-Length": str(length)}
    request = urllib.request.Request(f"{BASE_URL}{path}", data=body, method=method, headers=headers)
    timeout = UPLOAD_TIMEOUT_S if length is not None else HTTP_TIMEOUT_S
    try:
        with urllib.request.urlopen(request, context=context(), timeout=timeout) as response:
            status_code, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status_code, raw = exc.code, exc.read()
    try:
        return status_code, cast(object, json.loads(raw))
    except ValueError:
        return status_code, raw.decode("utf-8", "replace")[:500]


def put(site: str, gen: int, pib: Path) -> dict[str, object]:
    """`PUT /bundles/{site}/{gen}`(ci 토큰, Caddy HTTPS — publish와 같은 경로)."""
    with pib.open("rb") as body:
        status_code, reply = _request(
            "PUT", f"/bundles/{site}/{gen}", "ci_token", body=body, length=pib.stat().st_size
        )
    return {"status": status_code, "body": reply}


def offline_import(path: Path) -> dict[str, object]:
    """`POST /offline-imports`(admin 토큰 — 비밀 파일에서만 읽는다, Caddy HTTPS, M6-4b-2 G2-7):
    Updater `report export` 묶음 파일(`site-reports` 봉투, JSON) 그대로."""
    with path.open("rb") as body:
        status_code, reply = _request(
            "POST",
            "/offline-imports",
            "admin_token",
            body=body,
            length=path.stat().st_size,
            content_type="application/json",
        )
    return {"status": status_code, "body": reply, "file": path.name}


def fleet_https(site: str | None) -> dict[str, object]:
    query = "?" + urllib.parse.urlencode({"site": site}) if site else ""
    status_code, reply = _request("GET", f"/fleet{query}", "publish_viewer_token")
    return {"status": status_code, "body": reply, "via": "caddy"}


UI_FLEET = f"https://{HOST}:{PORT}/ui/fleet"
"""control-web 화면이 읽는 경로(`services/control-web/src/api/client.ts` `FLEET_PATH`)."""


def fleet_ui() -> dict[str, object]:
    """control-web과 같은 길로 `/fleet`을 읽는다(M6-4b G2-9): Caddy basic auth(`ops` + 원문
    `ui_password`) → `/ui/fleet` → Caddy가 viewer 토큰 비밀 파일을 붙여 control-api `GET
    /api/v1/fleet`으로 넘긴다(deploy/central/Caddyfile — 토큰을 붙이는 유일한 곳). 비밀번호는 비밀
    파일에서만 읽고 출력·오류에 싣지 않는다. 행 전부를 돌려준다(현장 고르기는 부른 쪽)."""
    pair = f"{UI_USER}:{read_secret('ui_password')}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    request = urllib.request.Request(UI_FLEET, headers=headers)
    try:
        with urllib.request.urlopen(request, context=context(), timeout=HTTP_TIMEOUT_S) as response:
            status_code, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status_code, raw = exc.code, exc.read()
    try:
        reply: object = json.loads(raw)
    except ValueError:
        reply = raw.decode("utf-8", "replace")[:500]
    return {"status": status_code, "body": reply, "via": "caddy-ui"}


DIRECT_FLEET = """\
import json, sys, urllib.error, urllib.request
token = sys.stdin.readline().strip()
req = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer " + token})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        status, raw = r.status, r.read()
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read()
print(json.dumps({"status": status, "body": json.loads(raw)}))
"""


def fleet_direct(site: str | None) -> dict[str, object]:
    """Caddy를 거치지 않고 control-api 컨테이너 안에서 `GET /fleet`(control-web을 멈춘 동안
    판정용). 토큰은 stdin으로만 넘긴다(argv·env에 남기지 않는다)."""
    query = "?" + urllib.parse.urlencode({"site": site}) if site else ""
    out = compose(
        "exec",
        "-T",
        "control-api",
        "python",
        "-c",
        DIRECT_FLEET,
        f"http://127.0.0.1:8100/api/v1/fleet{query}",
        stdin=(read_secret("publish_viewer_token") + "\n").encode(),
    )
    reply = cast(dict[str, object], json.loads(out.strip().splitlines()[-1]))
    return {**reply, "via": "control-api"}


def start_web() -> dict[str, object]:
    compose("start", "control-web")
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            return {"healthz": healthz()}
        except (OSError, ssl.SSLError):
            time.sleep(1)
    raise CentralError(f"control-web did not come back within {READY_TIMEOUT_S}s")


def fleet_view(reply: dict[str, object]) -> dict[str, dict[str, object]]:
    """`/fleet` 응답(200) → 현장 → `FLEET_FIELDS` 값. 200이 아니거나 행이 없으면 빈 dict."""
    body = reply.get("body")
    if reply.get("status") != 200 or not isinstance(body, list):
        return {}
    view: dict[str, dict[str, object]] = {}
    for raw in cast(list[object], body):
        if not isinstance(raw, dict):
            continue
        row = cast(dict[str, object], raw)
        values: dict[str, object] = {}
        for part, key in FLEET_FIELDS:
            source = cast(dict[str, object], row.get(part) or {}) if part else row
            values[f"{part}.{key}" if part else key] = source.get(key)
        view[str(row.get("site"))] = values
    return view


def fleet_mismatch(
    saved: dict[str, dict[str, object]], now: dict[str, dict[str, object]]
) -> list[str]:
    """복원 뒤 `/fleet` 요약이 덤프 때와 다른 항목(`<현장>.<값>`, 한쪽에만 있는 현장). 덤프 때
    요약이 비었으면 그것 자체가 불일치다 — 못 읽은 값이 일치가 되지 않게."""
    if not saved:
        return ["<saved view empty>"]
    diff = sorted(set(saved) ^ set(now))
    for site in sorted(set(saved) & set(now)):
        diff += [f"{site}.{k}" for k in saved[site] if saved[site][k] != now[site].get(k)]
    return diff


def _dump_paths(name: str) -> tuple[Path, Path]:
    if DUMP_NAME.fullmatch(name) is None:
        raise CentralError(f"dump name must match {DUMP_NAME.pattern}: {name!r}")
    return DUMPS / f"{name}.dump", DUMPS / f"{name}.fleet.json"


def dump(name: str) -> dict[str, object]:
    """control-db 전체(`pg_dump -Fc`)와 그때의 `/fleet` 요약. 부른 쪽이 control-web을 먼저 멈춰
    둔다(요약과 덤프 사이에 현장 보고가 끼지 않게)."""
    data_file, view_file = _dump_paths(name)
    started = time.monotonic()
    view = fleet_view(fleet_direct(None))
    if not view:
        raise CentralError("cannot read /fleet before dump")
    data = compose_raw("exec", "-T", DB_SERVICE, "pg_dump", "-U", DB_USER, "-d", DB_NAME, "-Fc")
    DUMPS.mkdir(parents=True, mode=0o700, exist_ok=True)
    DUMPS.chmod(0o700)
    fd = os.open(data_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    view_file.write_text(json.dumps(view, sort_keys=True), encoding="utf-8")
    return {
        "name": name,
        "file": str(data_file),
        "bytes": len(data),
        "seconds": round(time.monotonic() - started, 1),
        "fleet": view,
    }


def wait_api() -> dict[str, object]:
    """control-api가 `/fleet`(직접) 200을 낼 때까지(control-web은 멈춘 채)."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            reply = fleet_direct(None)
            if reply.get("status") == 200:
                return reply
            last = f"status {reply.get('status')}"
        except (CentralError, ValueError, IndexError) as exc:
            last = str(exc)[-200:]
        time.sleep(1)
    raise CentralError(f"control-api not ready within {READY_TIMEOUT_S}s after restore ({last})")


def restore(name: str) -> dict[str, object]:
    """control-web·control-api 멈춤 → `pg_restore --clean --if-exists --single-transaction` →
    control-api 시작 → `/fleet`(직접)이 덤프 때 요약과 같은지(`fleet_mismatch`, 다르면
    `CentralError`). control-web은 멈춘 채 둔다(부른 쪽이 VM 복원 뒤 `start-web`)."""
    data_file, view_file = _dump_paths(name)
    if not data_file.is_file() or not view_file.is_file():
        raise CentralError(f"no dump {name!r} in {DUMPS} — dumps exist for this session only")
    saved = cast(dict[str, dict[str, object]], json.loads(view_file.read_text(encoding="utf-8")))
    started = time.monotonic()
    compose("stop", "control-web", "control-api")
    compose_raw(
        "exec",
        "-T",
        DB_SERVICE,
        "pg_restore",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--single-transaction",
        stdin=data_file.read_bytes(),
    )
    compose("start", "control-api")
    now = fleet_view(wait_api())
    mismatch = fleet_mismatch(saved, now)
    if mismatch:
        raise CentralError(f"/fleet after restore differs from the dump: {mismatch}")
    return {
        "name": name,
        "seconds": round(time.monotonic() - started, 1),
        "fleet": now,
        "fleet_match": True,
    }


def down() -> dict[str, object]:
    compose("down", "-v", "--remove-orphans")
    shutil.rmtree(WORK, ignore_errors=True)
    return {"project": PROJECT, "down": True, "work_removed": not WORK.exists()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="central")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up").add_argument("--ca-out", type=Path, default=None)
    sub.add_parser("register").add_argument("--enrollment", type=Path, required=True)
    sub.add_parser("site-token").add_argument("--site", required=True)
    put_cmd = sub.add_parser("put")
    put_cmd.add_argument("--site", required=True)
    put_cmd.add_argument("--gen", type=int, required=True)
    put_cmd.add_argument("--pib", type=Path, required=True)
    fleet_cmd = sub.add_parser("fleet")
    fleet_cmd.add_argument("--site", default=None)
    fleet_cmd.add_argument("--direct", action="store_true", help="control-api 컨테이너 안에서 조회")
    fleet_cmd.add_argument("--ui", action="store_true", help="control-web 경로(Caddy basic auth)")
    sub.add_parser("offline-import").add_argument("--file", type=Path, required=True)
    for name in ("stop-web", "start-web", "status", "down"):
        sub.add_parser(name)
    for name in ("dump", "restore"):
        sub.add_parser(name).add_argument("--name", required=True)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.command == "site-token":
        sys.stdout.write(read_secret(f"{args.site}.token") + "\n")
        return 0
    print(json.dumps(run_command(args), ensure_ascii=False, sort_keys=True))
    return 0


def fleet(args: argparse.Namespace) -> dict[str, object]:
    """`fleet`: control-web 경로(`--ui`) · control-api 직접(`--direct`) · publish 토큰 HTTPS."""
    if args.ui:
        return fleet_ui()
    if args.direct:
        return fleet_direct(args.site)
    return fleet_https(args.site)


def stop_web() -> dict[str, object]:
    compose("stop", "control-web")
    return {"stopped": "control-web"}


def run_command(args: argparse.Namespace) -> dict[str, object]:
    """JSON을 내는 명령(토큰 원문을 내는 `site-token`은 `main`에서)."""
    commands: dict[str, Callable[[], dict[str, object]]] = {
        "up": lambda: up(args.ca_out),
        "register": lambda: register(args.enrollment),
        "put": lambda: put(args.site, args.gen, args.pib),
        "fleet": lambda: fleet(args),
        "offline-import": lambda: offline_import(args.file),
        "stop-web": stop_web,
        "start-web": start_web,
        "status": status,
        "dump": lambda: dump(args.name),
        "restore": lambda: restore(args.name),
        "down": down,
    }
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())

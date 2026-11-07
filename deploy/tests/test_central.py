"""중앙 compose·Caddyfile·이미지 정적 규칙(M4-3 G1·G3·G4·G5). docker 없이 돈다.

실제 동작(Caddy가 토큰을 `/ui/fleet`에만 붙이는지, basic auth·보안 헤더)은 control-web e2e가,
`caddy validate`·`docker compose config`는 test_central_compose.py(integration)가 본다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

DEPLOY = Path(__file__).resolve().parents[1]
REPO = DEPLOY.parent
CENTRAL = DEPLOY / "central"
COMPOSE = CENTRAL / "compose.yml"
CADDYFILE = CENTRAL / "Caddyfile"
ENV_EXAMPLE = CENTRAL / ".env.example"
CONTROL_DEV = DEPLOY / "compose" / "control.dev.yml"
NGINX = REPO / "services" / "web" / "nginx.conf"
API_DOCKERFILE = REPO / "services" / "control-api" / "Dockerfile"
WEB_DOCKERFILE = REPO / "services" / "control-web" / "Dockerfile"
CORE_DOCKERFILE = REPO / "services" / "core" / "Dockerfile"
SITE_WEB_DOCKERFILE = REPO / "services" / "web" / "Dockerfile"
# CI(25432·25433)·M4-2 리허설(25443 control-db, 8100 control-api)이 쓰는 호스트 포트.
PORTS_IN_USE = {"25432", "25433", "25443", "8100"}
E2E_PORT = "28543"  # deploy/tests/central_stack.py 기본


class _Loader(Protocol):
    def load(self, stream: str) -> object: ...


def _yaml(path: Path) -> dict[str, object]:
    loader = cast(_Loader, YAML(typ="safe", pure=True))
    return cast(dict[str, object], loader.load(path.read_text(encoding="utf-8")))


def _services(path: Path = COMPOSE) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], _yaml(path)["services"])


def _caddy_lines() -> list[str]:
    """주석·빈 줄을 뺀 Caddyfile 지시 줄(앞뒤 공백 제거)."""
    raw = CADDYFILE.read_text(encoding="ascii").splitlines()
    lines = (line.split("#", 1)[0].strip() for line in raw)
    return [line for line in lines if line]


def _block(lines: list[str], opener: str) -> list[str]:
    """`opener {`로 시작하는 블록 안의 줄(중첩 포함, 닫는 줄 제외)."""
    start = lines.index(f"{opener} {{")
    depth, body = 0, list[str]()
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return body[1:]
        body.append(line)
    raise AssertionError(f"unterminated block {opener}")


def _arg(dockerfile: Path, name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"{dockerfile.name}: ARG {name}"
    return match[1]


# --- compose ---------------------------------------------------------------------------------


def test_compose_services_and_project_name() -> None:
    assert _yaml(COMPOSE)["name"] == "pureiron-central"
    assert set(_services()) == {"control-db", "control-api", "control-web"}


def test_central_images_are_local_release_tags() -> None:
    """현장 compose와 같은 규칙: 로컬 이미지만(pull 안 함), 태그 = release 이름."""
    for svc in ("control-api", "control-web"):
        service = _services()[svc]
        assert service["image"] == f"pureiron/{svc}:${{PUREIRON_CENTRAL_RELEASE:-0.1.0-dev}}"
        assert service["pull_policy"] == "never"


def test_control_db_is_pinned_to_the_dev_digest() -> None:
    image = str(_services()["control-db"]["image"])
    assert re.fullmatch(r"postgres:16@sha256:[0-9a-f]{64}", image)
    assert image == _services(CONTROL_DEV)["control-db"]["image"]


def test_secrets_are_files_only() -> None:
    secrets = cast(dict[str, dict[str, str]], _yaml(COMPOSE)["secrets"])
    assert set(secrets) == {"control_db_password", "viewer_token", "ui_basic_auth"}
    secrets_dir = "${PUREIRON_CENTRAL_SECRETS_DIR:?set PUREIRON_CENTRAL_SECRETS_DIR}"
    for name, spec in secrets.items():
        assert spec["file"] == f"{secrets_dir}/{name}"
    text = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:" not in text
    for service in _services().values():
        env = cast(dict[str, str], service.get("environment", {}))
        assert not [key for key in env if key.endswith(("_TOKEN", "_PASSWORD"))], env


def test_viewer_token_reaches_only_caddy() -> None:
    services = _services()
    assert services["control-web"]["secrets"] == ["viewer_token", "ui_basic_auth"]
    assert services["control-api"]["secrets"] == ["control_db_password"]
    assert services["control-db"]["secrets"] == ["control_db_password"]


def test_only_caddy_publishes_a_port() -> None:
    services = _services()
    assert "ports" not in services["control-db"]
    assert "ports" not in services["control-api"]
    ports = cast(list[str], services["control-web"]["ports"])
    bind, port = "${PUREIRON_CENTRAL_BIND:-127.0.0.1}", "${PUREIRON_CENTRAL_HTTPS_PORT:-28443}"
    assert ports == [f"{bind}:{port}:8443"]


def test_default_ports_do_not_collide() -> None:
    env = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert env["PUREIRON_CENTRAL_HTTPS_PORT"] not in PORTS_IN_USE | {E2E_PORT}
    assert E2E_PORT not in PORTS_IN_USE
    assert env["PUREIRON_CENTRAL_BIND"] == "127.0.0.1"


def test_no_migrate_on_boot() -> None:
    """마이그레이션은 배포 절차(`run --rm control-api migrate up`)만 — 기동 명령에 없다."""
    api = _services()["control-api"]
    assert "command" not in api
    assert "entrypoint" not in api
    text = API_DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["pureiron-control"]' in text
    assert 'CMD ["serve"]' in text


def test_poll_interval_comes_from_env() -> None:
    env = cast(dict[str, str], _services()["control-api"]["environment"])
    assert env["PUREIRON_CONTROL_POLL_S"] == "${PUREIRON_CONTROL_POLL_S:-60}"


def test_caddyfile_is_mounted_read_only() -> None:
    volumes = cast(list[str], _services()["control-web"]["volumes"])
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in volumes


# --- Caddyfile ------------------------------------------------------------------------------


def test_token_is_added_only_inside_the_fleet_handler() -> None:
    """★G1·R1: Authorization을 만드는 줄은 하나뿐이고 GET/HEAD `/ui/fleet` 핸들러 안에 있다."""
    lines = _caddy_lines()
    injections = [line for line in lines if "Authorization" in line]
    assert injections == ['header_up Authorization "Bearer {file./run/secrets/viewer_token}"']
    assert _block(lines, "@fleet") == ["path /ui/fleet", "method GET HEAD"]
    fleet = _block(lines, "handle @fleet")
    assert fleet[0] == "rewrite * /api/v1/fleet"
    assert injections[0] in fleet
    assert "respond 404" in _block(lines, "handle /ui/*")


def test_api_is_proxied_without_basic_auth() -> None:
    """G3: `/api/v1/*`는 basic auth 밖에서 그대로 control-api로(토큰 주입 없음)."""
    lines = _caddy_lines()
    assert _block(lines, "handle /api/v1/*") == ["reverse_proxy control-api:8100"]
    assert lines.index("handle /api/v1/* {") < lines.index("basic_auth {")
    assert _block(lines, "basic_auth") == ["import /run/secrets/ui_basic_auth"]


def test_tls_internal_and_host_from_env() -> None:
    lines = _caddy_lines()
    assert "{$PUREIRON_CENTRAL_HOST:localhost} {" in lines
    assert "tls internal" in lines
    assert "https_port 8443" in lines


def test_security_headers_match_site_web() -> None:
    pattern = re.compile(r'^\s*add_header (\S+) "([^"]+)" always;', re.MULTILINE)
    nginx = dict(pattern.findall(NGINX.read_text(encoding="utf-8")))
    assert len(nginx) == 4
    caddy = _block(_caddy_lines(), "header")
    for name, value in nginx.items():
        assert f'{name} "{value}"' in caddy, name


# --- 이미지 ---------------------------------------------------------------------------------


def test_base_images_are_digest_pinned_and_shared() -> None:
    for dockerfile, arg in (
        (API_DOCKERFILE, "PYTHON_IMAGE"),
        (API_DOCKERFILE, "UV_IMAGE"),
        (WEB_DOCKERFILE, "NODE_IMAGE"),
        (WEB_DOCKERFILE, "CADDY_IMAGE"),
    ):
        assert re.search(r"@sha256:[0-9a-f]{64}$", _arg(dockerfile, arg)), (dockerfile.name, arg)
    assert _arg(API_DOCKERFILE, "PYTHON_IMAGE") == _arg(CORE_DOCKERFILE, "PYTHON_IMAGE")
    assert _arg(API_DOCKERFILE, "UV_IMAGE") == _arg(CORE_DOCKERFILE, "UV_IMAGE")
    assert _arg(WEB_DOCKERFILE, "NODE_IMAGE") == _arg(SITE_WEB_DOCKERFILE, "NODE_IMAGE")


def test_control_api_image_is_built_like_core() -> None:
    text = API_DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --locked --no-dev --no-editable --package pureiron-control-api" in text
    assert "normalize_venv.py" in text
    assert "COPY services/control-api/migrations /opt/pureiron/migrations" in text
    assert "PUREIRON_CONTROL_MIGRATIONS_DIR=/opt/pureiron/migrations" in text
    assert "USER 10001:10001" in text


def test_dockerignore_admits_central_sources() -> None:
    lines = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for needed in ("!services/control-api/migrations/", "!services/control-web/"):
        assert needed in lines
    assert "services/control-web/node_modules/" in lines

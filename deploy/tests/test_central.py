"""중앙 compose·Caddyfile·이미지 정적 규칙(M4-3 G1·G3·G4·G5). docker 없이 돈다.

실제 동작은 두 곳에서 본다. 토큰이 `/ui/fleet`에만 붙고 응답에 나가지 않는지, basic auth·보안 헤더는
control-web e2e. `caddy validate`·`docker compose config`와 저장소 Caddyfile을 실제 Caddy로 띄운
검사(SNI 없는 IP 접속, 설정에 없는 Host 421, 빈 `ui_basic_auth`)는 test_central_compose.py(integration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
FLEET_TOKEN_HEADER = 'header_up Authorization "Bearer {file./run/secrets/viewer_token}"'


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


def _no_children() -> list[_Node]:
    return []


@dataclass
class _Node:
    """Caddyfile 지시 한 줄. `head {`로 열린 블록이면 children에 안쪽 지시(순서 유지)."""

    head: str
    children: list[_Node] = field(default_factory=_no_children)

    def heads(self) -> list[str]:
        return [child.head for child in self.children]

    def child(self, head: str) -> _Node:
        found = [c for c in self.children if c.head == head]
        assert len(found) == 1, (head, self.heads())
        return found[0]

    def walk(self) -> list[_Node]:
        return [self, *(node for child in self.children for node in child.walk())]


def _caddy_tree() -> _Node:
    """주석 뺀 Caddyfile을 블록 트리로(한 줄에 `{`/`}` 하나 — 이 파일의 형식)."""
    root = _Node("<root>")
    stack = [root]
    for line in _caddy_lines():
        if line == "}":
            stack.pop()
        elif line.endswith(" {") or line == "{":
            node = _Node(line.removesuffix("{").strip() or "{")
            stack[-1].children.append(node)
            stack.append(node)
        else:
            stack[-1].children.append(_Node(line))
    assert stack == [root], "unbalanced Caddyfile"
    return root


def _site() -> _Node:
    return _caddy_tree().child("{$PUREIRON_CENTRAL_HOST:localhost}")


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
    """★G1·R1: Authorization을 만드는 줄은 하나뿐이고 GET `/ui/fleet` 핸들러 안에 있다."""
    lines = _caddy_lines()
    injections = [line for line in lines if "Authorization" in line]
    assert injections == [FLEET_TOKEN_HEADER]
    assert _block(lines, "@fleet") == ["path /ui/fleet", "method GET"]
    fleet = _block(lines, "handle @fleet")
    assert fleet[0] == "rewrite * /api/v1/fleet"
    assert injections[0] in fleet
    assert "respond 404" in _block(lines, "handle /ui/*")


def test_viewer_token_appears_only_in_the_fleet_header_up() -> None:
    """★G1(리뷰 L-A): 비밀 파일을 읽는 줄은 upstream 요청 헤더 하나뿐이다. 정적 핸들러의 `header`나
    프록시의 `header_down`으로 읽으면 브라우저 응답에 토큰이 나간다(리뷰 실측 X13·X16)."""
    readers = [line for line in _caddy_lines() if "viewer_token" in line]
    assert readers == [FLEET_TOKEN_HEADER]


def test_fleet_handler_is_exact() -> None:
    """★G1(리뷰 L-A): 토큰이 가는 곳은 control-api `GET /api/v1/fleet` 하나다. 경로를 또 바꾸거나
    upstream을 바꾸면(리뷰 X14·X15) 토큰이 다른 곳으로 간다."""
    outer = _site().child("handle")
    assert outer.child("@fleet").heads() == ["path /ui/fleet", "method GET"]
    fleet = outer.child("handle @fleet")
    assert fleet.heads() == ["rewrite * /api/v1/fleet", "reverse_proxy control-api:8100"]
    assert fleet.child("reverse_proxy control-api:8100").heads() == [FLEET_TOKEN_HEADER]


def test_other_ui_handlers_are_exact() -> None:
    """리뷰 L-A: `/ui/*`는 404뿐이고 정적 핸들러는 파일만 낸다(리뷰 X17·X23: 프록시 추가 금지)."""
    outer = _site().child("handle")
    assert outer.child("handle /ui/*").heads() == ["respond 404"]
    static = outer.child("handle")
    assert static.heads() == [
        "root * /srv/control-web",
        "try_files {path} /index.html",
        "file_server",
    ]


def test_global_options_are_exact() -> None:
    """리뷰 L-A: admin API는 꺼 둔다(리뷰 X18). 전역 옵션은 이 다섯 줄뿐이다."""
    assert _caddy_tree().child("{").heads() == [
        "admin off",
        "skip_install_trust",
        "https_port 8443",
        "auto_https disable_redirects",
        "default_sni {$PUREIRON_CENTRAL_DEFAULT_SNI:localhost}",
    ]


def test_api_is_proxied_without_basic_auth() -> None:
    """G3: `/api/v1/*`는 basic auth 밖에서 그대로 control-api로(토큰 주입 없음)."""
    lines = _caddy_lines()
    assert _block(lines, "handle /api/v1/*") == ["reverse_proxy control-api:8100"]
    assert lines.index("handle /api/v1/* {") < lines.index("basic_auth {")
    assert _block(lines, "basic_auth") == ["import /run/secrets/ui_basic_auth"]


def test_basic_auth_comes_before_every_ui_handler() -> None:
    """★G1(리뷰 M2): 바깥 `handle`의 첫 지시가 basic_auth이고 fleet·`/ui/*`·정적 핸들러는
    모두 그 뒤 같은 깊이에 있다. basic_auth를 `handle /ui/*` 안으로 옮기면 익명
    `GET /ui/fleet`에 viewer 토큰이 붙는다(리뷰 실측 V1)."""
    site = _site()
    assert site.heads() == ["tls internal", "header", "handle /api/v1/*", "handle"]
    outer = site.child("handle")
    assert outer.heads() == ["basic_auth", "@fleet", "handle @fleet", "handle /ui/*", "handle"]
    assert outer.children[0].heads() == ["import /run/secrets/ui_basic_auth"]
    every = _caddy_tree().walk()
    assert [n for n in every if n.head == "basic_auth"] == [outer.children[0]]


def test_api_is_the_only_handler_outside_basic_auth() -> None:
    """basic auth 없는 핸들러는 `handle /api/v1/*` 하나이고, 그 안에는 토큰 주입이 없다."""
    site = _site()
    handlers = [c for c in site.children if c.head.startswith("handle")]
    unauthenticated = [c for c in handlers if c.head != "handle"]
    assert [c.head for c in unauthenticated] == ["handle /api/v1/*"]
    api = unauthenticated[0]
    assert not [n for n in api.walk() if "header_up" in n.head or "Authorization" in n.head]


def test_tls_internal_and_host_from_env() -> None:
    lines = _caddy_lines()
    assert "{$PUREIRON_CENTRAL_HOST:localhost} {" in lines
    assert "tls internal" in lines
    assert "https_port 8443" in lines


def test_default_sni_for_ip_clients() -> None:
    """리뷰 M1: IP로 붙는 클라이언트는 SNI를 보내지 않는다 — 전역 default_sni(env)로 고른다."""
    options = _caddy_tree().child("{")
    assert "default_sni {$PUREIRON_CENTRAL_DEFAULT_SNI:localhost}" in options.heads()
    env = cast(dict[str, str], _services()["control-web"]["environment"])
    assert env["PUREIRON_CENTRAL_DEFAULT_SNI"] == "${PUREIRON_CENTRAL_DEFAULT_SNI:-localhost}"
    assert "PUREIRON_CENTRAL_DEFAULT_SNI=" in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_other_hosts_get_421_with_headers() -> None:
    """리뷰 N4: 어떤 사이트에도 맞지 않는 Host는 빈 200 대신 421 + 보안 헤더."""
    tree = _caddy_tree()
    assert tree.heads() == ["{", "{$PUREIRON_CENTRAL_HOST:localhost}", "https://"]
    fallback = tree.child("https://")
    assert fallback.heads() == ["tls internal", "header", "respond 421"]
    assert fallback.child("header").heads() == _site().child("header").heads()


def test_security_headers_match_site_web() -> None:
    pattern = re.compile(r'^\s*add_header (\S+) "([^"]+)" always;', re.MULTILINE)
    nginx = dict(pattern.findall(NGINX.read_text(encoding="utf-8")))
    assert len(nginx) == 4
    caddy = _site().child("header").heads()
    # 리뷰 L1: 정확히 이 4줄뿐(접두 없는 설정). 삭제(-)·defer(>)가 하나라도 있으면 Caddy가
    # 블록 전체를 미루고 오류 응답(basic auth 401 등)에서는 헤더 4개가 모두 빠진다(2.11.7 실측).
    assert len(caddy) == 4, caddy
    assert not [line for line in caddy if line.startswith(("-", ">"))], caddy
    assert sorted(caddy) == sorted(f'{name} "{value}"' for name, value in nginx.items())


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

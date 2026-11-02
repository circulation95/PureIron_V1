"""HTTP 표면: 조립(DB 연결 없이)·healthz·OpenAPI 경로와 인증 없는 요청 401·ETag 비교."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pureiron_control import __version__
from pureiron_control.api.bundles import etag_matches, etag_of
from pureiron_control.app import create_app
from pureiron_control.runtime import ControlRuntime
from pureiron_control.settings import Settings

# foundation §6.4 표 + 운영 경로. 메서드는 OpenAPI 소문자.
EXPECTED_OPERATIONS = {
    ("/api/v1/healthz", "get"),
    ("/api/v1/readyz", "get"),
    ("/api/v1/sites", "post"),
    ("/api/v1/sites/{site}/rekey", "post"),
    ("/api/v1/sites/{site}/token", "post"),
    ("/api/v1/sites/{site}/heartbeat", "post"),
    ("/api/v1/sites/{site}/desired", "get"),
    ("/api/v1/sites/{site}/bundles/{generation}", "get"),
    ("/api/v1/sites/{site}/reports", "post"),
    ("/api/v1/sites/{site}/drafts", "post"),
    ("/api/v1/sites/{site}/drafts/latest", "get"),
    ("/api/v1/bundles/{site}/{generation}", "put"),
    ("/api/v1/offline-imports", "post"),
    ("/api/v1/fleet", "get"),
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings.from_env({"PUREIRON_CONTROL_DATA_DIR": str(tmp_path)})
    # lifespan(DB 풀)을 열지 않는다 — 인증 전에 끝나는 요청만 보낸다.
    return TestClient(create_app(ControlRuntime.build(settings)))


def test_healthz_reports_version(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_openapi_has_the_control_surface(client: TestClient) -> None:
    paths: dict[str, dict[str, object]] = client.get("/openapi.json").json()["paths"]
    operations = {(path, method) for path, item in paths.items() for method in item}
    assert operations == EXPECTED_OPERATIONS


def test_publish_generation_is_bounded_by_the_integer_column(client: TestClient) -> None:
    """`bundles.generation`은 integer — 넘는 gen은 16 GiB를 받기 전에 422(리뷰 L-3)."""
    put = client.get("/openapi.json").json()["paths"]["/api/v1/bundles/{site}/{generation}"]["put"]
    schemas = {p["name"]: p["schema"] for p in put["parameters"]}
    assert (schemas["generation"]["minimum"], schemas["generation"]["maximum"]) == (1, 2**31 - 1)
    assert "507" in put["responses"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/sites"),
        ("POST", "/api/v1/sites/site-a/heartbeat"),
        ("GET", "/api/v1/sites/site-a/desired"),
        ("PUT", "/api/v1/bundles/site-a/1"),
        ("GET", "/api/v1/fleet"),
        ("POST", "/api/v1/offline-imports"),
    ],
)
def test_requests_without_token_are_401(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, content=b"{}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("token", ["Bearer nope", "Bearer pis_", "Basic dXNlcjpwYXNz"])
def test_unknown_token_shapes_are_401_without_db(client: TestClient, token: str) -> None:
    response = client.get("/api/v1/fleet", headers={"Authorization": token})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/sites/Site_A/desired", "/api/v1/sites/-a/desired"])
def test_bad_site_id_in_path_is_422(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ('"3"', True),
        ('W/"3"', True),
        ('"2", "3"', True),
        ("*", True),
        ('"4"', False),
        ("3", False),
    ],
)
def test_if_none_match(header: str | None, expected: bool) -> None:
    assert etag_of(3) == '"3"'
    assert etag_matches(header, etag_of(3)) is expected


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/sites/{site}/heartbeat", "post"),
        ("/api/v1/sites/{site}/reports", "post"),
        ("/api/v1/sites/{site}/drafts", "post"),
        ("/api/v1/offline-imports", "post"),
    ],
)
def test_signed_site_documents_document_the_key_mismatch_409(
    client: TestClient, path: str, method: str
) -> None:
    """리뷰 M1: 현장 서명 봉투는 400(형식 위반)과 409(등록 키 불일치)를 구분해 문서화한다."""
    responses = client.get("/openapi.json").json()["paths"][path][method]["responses"]
    assert {"400", "409"} <= set(responses)

from fastapi.testclient import TestClient

from pureiron_control import __version__
from pureiron_control.app import create_app


def test_healthz_reports_version() -> None:
    client = TestClient(create_app())
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}

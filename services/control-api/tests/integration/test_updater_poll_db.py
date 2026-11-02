"""현장 Updater `poll.py`(r4, 변경 없음)가 control-api를 그대로 소비하는지: desired·304·Range.

control-api를 실제 HTTP 서버(uvicorn, 스레드)로 띄우고 Updater의 `ControlApi`가 urllib로 붙는다.
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import uvicorn

from pureiron_control.app import create_app
from pureiron_control.runtime import ControlRuntime
from pureiron_updater.poll import ControlApi, file_sha256

from .conftest import Central
from .site_kit import Site, build_pib

pytestmark = pytest.mark.integration

STARTUP_TIMEOUT_S = 15.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def server(central: Central) -> Generator[str]:
    port = _free_port()
    app = create_app(ControlRuntime.build(central.settings, clock=central.clock))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not instance.started:
        assert time.monotonic() < deadline, "control-api server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/api/v1"
    finally:
        instance.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)


def test_updater_poll_client_reads_desired_and_resumes_download(
    central: Central, server: str, tmp_path: Path
) -> None:
    site = Site(tmp_path, "site-a")
    registered = central.client.post(
        "/api/v1/sites", content=site.enroll(), headers=central.as_role("admin")
    )
    token_file = tmp_path / "control_api_token"
    token_file.write_text(registered.json()["token"] + "\n", encoding="utf-8")
    api = ControlApi(server, "site-a", token_file)
    assert api.desired() is None  # 게시 전 404

    pib = build_pib(tmp_path / "g1.pib", "site-a", 1, padding_bytes=300_000).read_bytes()
    published = central.client.put(
        "/api/v1/bundles/site-a/1", content=pib, headers=central.as_role("ci")
    )
    assert published.status_code == 201
    desired = api.desired()
    assert desired is not None
    assert (desired.generation, desired.size) == (1, len(pib))
    assert desired.sha256 == "sha256:" + hashlib.sha256(pib).hexdigest()
    assert api.desired() == desired  # 두 번째는 If-None-Match → 304 → 마지막 값

    part = tmp_path / "poll.pib.part"
    part.write_bytes(pib[:123_456])  # 끊긴 다운로드
    api.download(desired.bundle_url, part, desired.size)
    assert part.stat().st_size == len(pib)
    assert file_sha256(part) == desired.sha256

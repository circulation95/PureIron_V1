"""control-api 스트리밍 PUT을 실제 TLS 1.3으로(M4 마감 수정 r3).

TLS 1.3 서버는 핸드셰이크 직후 NewSessionTicket 레코드를 보내 클라이언트 소켓이 HTTP 응답 없이도
readable이 된다. `select`만 보던 put_bundle은 첫 청크 전에 보내기를 멈추고 응답을 기다리다 read
timeout 났다(fleet-test run 42536145640, 중앙 Caddy). 여기서는 로컬 TLS 1.3 서버(시험 안에서 만든
자체 서명 인증서, 기본 설정 = 세션 티켓 전송)로 ① 수 MB 본문이 전부 도착해 201, ② 본문 전 409·404
응답(L-7 HTTPS판)에서 보내기를 멈추고 그 상태·detail을 읽는지 본다. 인증서·키는 pytest tmp에만
쓰고 클라이언트 신뢰는 `ControlApi.ssl_context` 주입으로만 한다(프로세스 전역 무변경).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import socket
import ssl
import threading
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fleet.publish import ControlApi, ControlApiError

CLIENT_TIMEOUT_S = 5.0
"""수정 전 코드는 응답을 이만큼 기다리다 timeout 난다(운영 120 s 대신 짧게)."""
SERVER_TIMEOUT_S = 15.0
BODY_SIZE = 8 * 1024 * 1024

Handler = Callable[[ssl.SSLSocket, int, list[int]], None]
"""(TLS 연결, Content-Length, [받은 본문 바이트 수]) — 헤더를 읽은 뒤 불린다."""


def _self_signed(directory: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pureiron-tls-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_file, key_file = directory / "server.crt", directory / "server.key"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file, cert_pem.decode()


@contextlib.contextmanager
def _tls_server(
    tmp_path: Path, handler: Handler
) -> Generator[tuple[ControlApi, list[int], list[BaseException]]]:
    cert_file, key_file, cert_pem = _self_signed(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_3
    server_context.load_cert_chain(cert_file, key_file)
    assert server_context.num_tickets > 0  # 기본 설정: 핸드셰이크 뒤 세션 티켓을 보낸다
    client_context = ssl.create_default_context(cadata=cert_pem)
    client_context.minimum_version = ssl.TLSVersion.TLSv1_3

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    received = [0]
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            raw, _ = listener.accept()
            raw.settimeout(SERVER_TIMEOUT_S)
            with server_context.wrap_socket(raw, server_side=True) as conn:
                assert conn.version() == "TLSv1.3"
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("헤더 전에 연결이 닫혔다")
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                received[0] = len(body)
                handler(conn, length, received)
        except BaseException as exc:  # 시험 스레드 — 본 스레드가 errors로 확인한다
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    api = ControlApi(
        f"https://127.0.0.1:{port}/api/v1",
        viewer_token=None,
        ci_token="ci",
        timeout=CLIENT_TIMEOUT_S,
        ssl_context=client_context,
    )
    try:
        yield api, received, errors
    finally:
        thread.join(timeout=SERVER_TIMEOUT_S + 5)
        listener.close()


def _response(status: str, payload: dict[str, object]) -> bytes:
    body = json.dumps(payload).encode()
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def _big_pib(tmp_path: Path, size: int) -> Path:
    pib = tmp_path / "big.pib"
    with pib.open("wb") as f:
        f.truncate(size)
    return pib


def test_put_over_tls13_sends_whole_body_despite_session_tickets(tmp_path: Path) -> None:
    """세션 티켓으로 소켓이 readable이어도 이른 응답으로 보지 않고 본문을 끝까지 보낸다."""

    def accept_all(conn: ssl.SSLSocket, length: int, received: list[int]) -> None:
        while received[0] < length:
            chunk = conn.recv(1 << 20)
            if not chunk:
                raise ConnectionError(f"본문 {received[0]}/{length} B에서 연결이 닫혔다")
            received[0] += len(chunk)
        conn.sendall(_response("201 Created", {"received": received[0]}))

    with _tls_server(tmp_path, accept_all) as (api, received, errors):
        result = api.put_bundle("site-a", 3, _big_pib(tmp_path, BODY_SIZE))
    assert errors == []
    assert received[0] == BODY_SIZE
    assert result == {"received": BODY_SIZE}


@pytest.mark.parametrize(
    ("status", "code", "payload"),
    [
        # 큰 본문 — 이른 읽기로 가져온 앞부분 + 소켓에 남은 뒷부분을 이어 JSON으로 파싱해야 한다.
        (
            "409 Conflict",
            409,
            {"pad": "x" * 200_000, "detail": "generation 3 <= latest 3"},
        ),
        ("404 Not Found", 404, {"detail": "site 'site-a' is not registered"}),
    ],
)
def test_put_over_tls13_stops_on_early_response(
    tmp_path: Path, status: str, code: int, payload: dict[str, object]
) -> None:
    """서버가 본문을 받기 전에 답하면(M4-0 L-7, HTTPS) 보내기를 멈추고 그 상태·detail로 실패한다."""
    size = 64 * 1024 * 1024

    def answer_early(conn: ssl.SSLSocket, length: int, received: list[int]) -> None:
        del length
        conn.sendall(_response(status, payload))
        with contextlib.suppress(OSError):  # 보내 온 것은 연결이 닫힐 때까지 비운다
            while chunk := conn.recv(1 << 20):
                received[0] += len(chunk)

    with (
        _tls_server(tmp_path, answer_early) as (api, received, errors),
        pytest.raises(ControlApiError, match=f"HTTP {code}: {payload['detail']}") as failure,
    ):
        api.put_bundle("site-a", 3, _big_pib(tmp_path, size))
    assert failure.value.status == code
    assert errors == []
    assert received[0] < size

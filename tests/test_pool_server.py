from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from sightmesh.pool import server


@pytest.fixture
def pool_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def request(port: int, path: str, body: bytes = b"{}", **headers: str):
    connection = http.client.HTTPConnection("127.0.0.1", port)
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def test_post_rejects_non_json_and_oversized_bodies(pool_server: int) -> None:
    status, _ = request(pool_server, "/api/order", b"x", Host="127.0.0.1")
    assert status == 415

    status, _ = request(
        pool_server,
        "/api/order",
        b"x" * (server.MAX_REQUEST_BODY_BYTES + 1),
        Host="127.0.0.1",
        **{"Content-Type": "application/json"},
    )
    assert status == 413


def test_post_rejects_cross_origin_requests(pool_server: int) -> None:
    status, _ = request(
        pool_server,
        "/api/order",
        Host="127.0.0.1",
        Origin="http://attacker.example",
        **{"Content-Type": "application/json"},
    )
    assert status == 403


def test_post_accepts_same_loopback_origin(pool_server: int) -> None:
    status, _ = request(
        pool_server,
        "/missing",
        Host=f"localhost:{pool_server}",
        Origin=f"http://localhost:{pool_server}",
        **{"Content-Type": "application/json"},
    )
    assert status == 404


def test_server_errors_do_not_echo_secrets(pool_server: int, monkeypatch) -> None:
    monkeypatch.setitem(
        server.ROUTES,
        "/api/secret",
        lambda _body: (_ for _ in ()).throw(ValueError("token=top-secret")),
    )

    status, body = request(
        pool_server,
        "/api/secret",
        Host="127.0.0.1",
        **{"Content-Type": "application/json"},
    )

    assert status == 400
    assert b"top-secret" not in body
    assert b"operation failed" in body

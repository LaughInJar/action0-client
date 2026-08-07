"""
The async machinery of this library is event-loop-agnostic: the base
classes and stubs use only ``async``/``await`` (no asyncio APIs), and
httpx does its async I/O through anyio — so the same backends run under
trio unchanged. These tests pin that promise with ``trio.run``.

(aiohttp is asyncio-only by design; there is no trio test for it.)
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import ClassVar

import httpx
import trio

from action0.client import APIClient
from action0.client import Client
from action0.client import JsonOperation
from action0.client import query
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.testing import AsyncStubBackend
from action0.req import Request
from action0.req import Response


class Ping(JsonOperation[Any]):
    """The example operation used in these tests."""

    path = "/ping"
    q: "str | None" = query(default=None)


class EchoHandler(BaseHTTPRequestHandler):
    """The local test endpoint: echoes the request path as JSON."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        encoded = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class TrioStubTestCase(unittest.TestCase):
    """
    tests for the core async machinery (base classes, stubs, clients)
    under trio
    """

    def test_client_send_awaits_under_trio(self) -> None:
        """
        Test that Client + AsyncStubBackend run on a trio loop.
        """

        async def main() -> Response:
            client = Client(AsyncStubBackend(Response(204)))
            return await client.send(Request("https://api.example.com/ping"))

        self.assertEqual(trio.run(main).status, 204)

    def test_api_client_pipeline_under_trio(self) -> None:
        """
        Test that the full operation pipeline (send + map + parse) runs
        on a trio loop.
        """

        async def main() -> Any:
            backend = AsyncStubBackend(Response(200, body='{"pong": true}'))
            client = APIClient(backend, "https://api.example.com/v1")
            return await client.send(Ping(q="x"))

        self.assertEqual(trio.run(main), {"pong": True})


class TrioHttpxTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.httpx.AsyncHttpxBackend`
    under trio — both over a mock transport and over real sockets
    """

    server: ClassVar[ThreadingHTTPServer]
    thread: ClassVar[threading.Thread]
    base_url: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_mock_transport_under_trio(self) -> None:
        """
        Test the backend and operation layer over httpx.MockTransport on
        a trio loop.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'{"pong": true}')

        async def main() -> Any:
            transport = httpx.MockTransport(handler)
            async with AsyncHttpxBackend(httpx.AsyncClient(transport=transport)) as backend:
                client = APIClient(backend, "https://api.example.com")
                return await client.send(Ping())

        self.assertEqual(trio.run(main), {"pong": True})

    def test_real_sockets_under_trio(self) -> None:
        """
        Test actual HTTP I/O through httpx's anyio transport on a trio
        loop, against the local server.
        """

        async def main() -> Any:
            async with AsyncHttpxBackend() as backend:
                client = APIClient(backend, self.base_url)
                return await client.send(Ping(q="x"))

        self.assertEqual(trio.run(main), {"path": "/ping?q=x"})

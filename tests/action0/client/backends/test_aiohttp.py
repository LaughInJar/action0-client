import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import ClassVar

import aiohttp

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import TimeoutError
from action0.client import TransportError
from action0.client import query
from action0.client.backends.aiohttp import AiohttpBackend
from action0.req import Request


class EchoHandler(BaseHTTPRequestHandler):
    """
    The local test endpoint: echoes method, path, headers and body as
    JSON. ``/slow`` stalls (for timeout tests), ``/status/<code>`` answers
    with that status, ``/redirect`` points at ``/target``.
    """

    # speak HTTP/1.1 (the stdlib default is 1.0); Content-Length is always
    # sent below, as 1.1 requires
    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        if self.path.startswith("/slow"):
            time.sleep(0.5)
        if self.path.startswith("/status/"):
            self.send_response(int(self.path.rsplit("/", 1)[1]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        payload = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": self.rfile.read(length).decode("utf-8") if length else "",
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        # two lines of the same field, to test multi-value preservation
        self.send_header("Set-Cookie", "a=1")
        self.send_header("Set-Cookie", "b=2")
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class Ping(JsonOperation[Any]):
    """The example operation used in the APIClient integration test."""

    path = "/ping"
    q: "str | None" = query(default=None)


class AiohttpBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.backends.aiohttp.AiohttpBackend`
    against a local HTTP server
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

    async def test_get_round_trip(self) -> None:
        """
        Test a plain GET: URL (with query), converted response, metadata.
        """
        request = Request(f"{self.base_url}/items", query={"page": 2})
        async with AiohttpBackend() as backend:
            response = await backend.send(request)

        body = response.body_str()
        assert body is not None
        seen = json.loads(body)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/items?page=2")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.http_version, "HTTP/1.1")
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])
        self.assertIs(response.request, request)

    async def test_post_with_body_and_headers(self) -> None:
        """
        Test that method, body and request headers arrive at the server.
        """
        request = Request(
            f"{self.base_url}/items",
            "POST",
            headers={"Content-Type": "application/json", "X-Tag": "yes"},
            body='{"a": 1}',
        )
        async with AiohttpBackend() as backend:
            response = await backend.send(request)
        body = response.body_str()
        assert body is not None
        seen = json.loads(body)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], '{"a": 1}')
        self.assertEqual(seen["headers"]["X-Tag"], "yes")
        self.assertEqual(seen["headers"]["Content-Type"], "application/json")

    async def test_error_status_is_not_an_exception(self) -> None:
        """
        Test that HTTP errors are responses, not raises.
        """
        async with AiohttpBackend() as backend:
            response = await backend.send(Request(f"{self.base_url}/status/503"))
        self.assertEqual(response.status, 503)

    async def test_redirects_followed_by_default_and_disablable(self) -> None:
        """
        Test both follow_redirects settings.
        """
        async with AiohttpBackend() as backend:
            followed = await backend.send(Request(f"{self.base_url}/redirect"))
        body = followed.body_str()
        assert body is not None
        self.assertEqual(json.loads(body)["path"], "/target")

        async with AiohttpBackend(follow_redirects=False) as backend:
            stopped = await backend.send(Request(f"{self.base_url}/redirect"))
        self.assertEqual(stopped.status, 302)
        self.assertEqual(stopped.headers["Location"], "/target")

    async def test_timeout_is_translated(self) -> None:
        """
        Test that an aiohttp timeout surfaces as the library's
        TimeoutError.
        """
        async with AiohttpBackend(timeout=0.05) as backend:
            with self.assertRaises(TimeoutError):
                await backend.send(Request(f"{self.base_url}/slow"))

    async def test_connection_error_is_translated(self) -> None:
        """
        Test that a refused connection surfaces as TransportError.
        """
        # grab a port nothing listens on
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        async with AiohttpBackend(timeout=1) as backend:
            with self.assertRaises(TransportError) as caught:
                await backend.send(Request(f"http://127.0.0.1:{port}/"))
        self.assertIsInstance(caught.exception.__cause__, aiohttp.ClientError)

    async def test_close_semantics(self) -> None:
        """
        Test that an owned session is closed and a passed one is not.
        """
        session = aiohttp.ClientSession()
        async with AiohttpBackend(session):
            pass
        self.assertFalse(session.closed)
        await session.close()

        owned = AiohttpBackend()
        await owned.send(Request(f"{self.base_url}/"))
        inner = owned._session
        assert inner is not None
        await owned.aclose()
        self.assertTrue(inner.closed)

    async def test_api_client_integration(self) -> None:
        """
        Test the full typed pipeline over aiohttp.
        """
        async with AiohttpBackend() as backend:
            client = APIClient(backend, self.base_url)
            seen = await client.send(Ping(q="x"))
        self.assertEqual(seen["path"], "/ping?q=x")

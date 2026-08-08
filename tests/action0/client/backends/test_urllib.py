import json
import socket
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import ClassVar
from typing import Iterator

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.urllib import UrllibBackend
from action0.client.backends.urllib import _merged_headers
from action0.client.backends.urllib import _request_data
from action0.req import Request
from action0.req.body import BodyProducer
from action0.req.body import BytesBody


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


class UrllibBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.urllib.UrllibBackend`
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

    def test_get_round_trip(self) -> None:
        """
        Test a plain GET: URL (with query), converted response, metadata.
        """
        request = Request(f"{self.base_url}/items", query={"page": 2})
        with UrllibBackend() as backend:
            response = backend.send(request)

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

    def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as a producer over the open
        connection and yields the same content a preloading send would.
        """
        with UrllibBackend(stream=True) as backend:
            response = backend.send(Request(f"{self.base_url}/items"))
            self.assertIsInstance(response.body, BodyProducer)

            body = response.body_bytes()  # joins the chunks
            assert body is not None
            self.assertEqual(json.loads(body)["path"], "/items")

    def test_post_with_body_and_headers(self) -> None:
        """
        Test that method, body and request headers arrive at the server
        (urllib normalizes header casing; the echo lookup is exact, so
        check case-insensitively via lowercased keys).
        """
        request = Request(
            f"{self.base_url}/items",
            "POST",
            headers={"Content-Type": "application/json", "X-Tag": "yes"},
            body='{"a": 1}',
        )
        with UrllibBackend() as backend:
            response = backend.send(request)
        body = response.body_str()
        assert body is not None
        seen = json.loads(body)
        seen_headers = {name.lower(): value for name, value in seen["headers"].items()}
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], '{"a": 1}')
        self.assertEqual(seen_headers["x-tag"], "yes")
        self.assertEqual(seen_headers["content-type"], "application/json")

    def test_error_status_is_not_an_exception(self) -> None:
        """
        Test that urllib's HTTPError is converted back into a Response —
        status policy belongs to the operation layer.
        """
        with UrllibBackend() as backend:
            response = backend.send(Request(f"{self.base_url}/status/503"))
        self.assertEqual(response.status, 503)

    def test_redirects_followed_by_default(self) -> None:
        """
        Test that 3xx responses are followed transparently.
        """
        with UrllibBackend() as backend:
            response = backend.send(Request(f"{self.base_url}/redirect"))
        body = response.body_str()
        assert body is not None
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["path"], "/target")

    def test_redirects_can_be_disabled(self) -> None:
        """
        Test that follow_redirects=False returns the 3xx itself.
        """
        with UrllibBackend(follow_redirects=False) as backend:
            response = backend.send(Request(f"{self.base_url}/redirect"))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/target")

    def test_timeout_is_translated(self) -> None:
        """
        Test that a socket timeout surfaces as the library's TimeoutError.
        """
        with UrllibBackend(timeout=0.05) as backend:
            with self.assertRaises(TimeoutError) as caught:
                backend.send(Request(f"{self.base_url}/slow"))
        self.assertIsNotNone(caught.exception.request)

    def test_connection_error_is_translated(self) -> None:
        """
        Test that a refused connection surfaces as TransportError.
        """
        # grab a port nothing listens on
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        with UrllibBackend(timeout=1) as backend:
            with self.assertRaises(TransportError):
                backend.send(Request(f"http://127.0.0.1:{port}/"))

    def test_custom_opener_is_used(self) -> None:
        """
        Test that a passed opener actually carries the sends.
        """
        seen: list[str] = []

        class Spy(urllib.request.BaseHandler):
            def http_request(self, request: urllib.request.Request) -> urllib.request.Request:
                seen.append(request.full_url)
                return request

        backend = UrllibBackend(urllib.request.build_opener(Spy()))
        backend.send(Request(f"{self.base_url}/spied"))
        self.assertEqual(seen, [f"{self.base_url}/spied"])


class ConversionTestCase(unittest.TestCase):
    """
    unit tests for the request-conversion helpers
    """

    def test_merged_headers_joins_repeated_lines(self) -> None:
        """
        Test the RFC 9110 comma-merge for the mapping urllib needs.
        """
        request = Request("https://example.com/", headers=[("X-A", ["1", "2"]), ("X-B", "3")])
        self.assertEqual(_merged_headers(request), {"X-A": "1, 2", "X-B": "3"})

    def test_request_data_forms(self) -> None:
        """
        Test the three body forms: none, in-memory, streaming.
        """
        self.assertIsNone(_request_data(Request("https://example.com/")))
        self.assertEqual(_request_data(Request("https://example.com/", body="text")), b"text")
        streamed = _request_data(Request("https://example.com/", body=BytesBody(b"chunked")))
        assert streamed is not None and not isinstance(streamed, bytes)
        self.assertIsInstance(streamed, Iterator)
        self.assertEqual(b"".join(streamed), b"chunked")

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import ClassVar
from typing import Iterator

import requests

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.requests import RequestsBackend
from action0.client.backends.requests import _merged_headers
from action0.client.backends.requests import _request_data
from action0.req import Request
from action0.req.body import BodyProducer
from action0.req.body import BytesBody


class EchoHandler(BaseHTTPRequestHandler):
    """
    The local test endpoint: echoes method, path, headers and body as
    JSON. ``/slow`` stalls (for timeout tests), ``/status/<code>`` answers
    with that status.
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


class RequestsBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.requests.RequestsBackend`
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

    def echo(self, request: Request) -> "dict[str, Any]":
        """
        Send a request to the echo endpoint and decode its JSON report.

        :param request: the request to send
        :return: what the server saw
        """
        with RequestsBackend() as backend:
            response = self.backend_response = backend.send(request)
        body = response.body_str()
        assert body is not None
        return json.loads(body)  # type: ignore[no-any-return]

    def test_get_round_trip(self) -> None:
        """
        Test a plain GET: URL (with query), converted response, metadata.
        """
        request = Request(f"{self.base_url}/items", query={"page": 2})
        seen = self.echo(request)

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/items?page=2")

        response = self.backend_response
        self.assertEqual(response.status, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.http_version, "HTTP/1.1")
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertIs(response.request, request)

    def test_multi_value_response_headers_are_preserved(self) -> None:
        """
        Test that repeated header lines survive the conversion.
        """
        self.echo(Request(f"{self.base_url}/"))
        self.assertEqual(self.backend_response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])

    def test_post_with_body_and_headers(self) -> None:
        """
        Test that method, body and request headers arrive at the server.
        """
        request = Request(
            f"{self.base_url}/items",
            "POST",
            headers={"Content-Type": "application/json", "X-Tag": "yes"},
            body='{"a": 1}',
        )
        seen = self.echo(request)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], '{"a": 1}')
        self.assertEqual(seen["headers"]["X-Tag"], "yes")
        self.assertEqual(seen["headers"]["Content-Type"], "application/json")

    def test_error_status_is_not_an_exception(self) -> None:
        """
        Test that HTTP errors are responses, not raises (that policy
        belongs to the operation layer).
        """
        with RequestsBackend() as backend:
            response = backend.send(Request(f"{self.base_url}/status/503"))
        self.assertEqual(response.status, 503)

    def test_timeout_is_translated(self) -> None:
        """
        Test that a read timeout surfaces as the library's TimeoutError.
        """
        with RequestsBackend(timeout=0.05) as backend:
            with self.assertRaises(TimeoutError) as caught:
                backend.send(Request(f"{self.base_url}/slow"))
        self.assertIsInstance(caught.exception.__cause__, requests.Timeout)
        self.assertIsNotNone(caught.exception.request)

    def test_connection_error_is_translated(self) -> None:
        """
        Test that a refused connection surfaces as TransportError.
        """
        # grab a port nothing listens on
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        with RequestsBackend(timeout=1) as backend:
            with self.assertRaises(TransportError):
                backend.send(Request(f"http://127.0.0.1:{port}/"))

    def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as a producer over the open
        connection and yields the same content a preloading send would.
        """
        with RequestsBackend(stream=True) as backend:
            response = backend.send(Request(f"{self.base_url}/items"))
            self.assertIsInstance(response.body, BodyProducer)
            self.assertNotIsInstance(response.body, (bytes, str))

            body = response.body_bytes()  # joins the chunks
            assert body is not None
            self.assertEqual(json.loads(body)["path"], "/items")

    def test_close_semantics(self) -> None:
        """
        Test that an owned session is closed and a passed one is not.
        """
        own_session = requests.Session()
        backend = RequestsBackend(own_session)
        backend.close()
        # still usable — the backend did not close what it doesn't own
        response = backend.send(Request(f"{self.base_url}/"))
        self.assertEqual(response.status, 200)
        own_session.close()


class ConversionTestCase(unittest.TestCase):
    """
    unit tests for the request-conversion helpers
    """

    def test_merged_headers_joins_repeated_lines(self) -> None:
        """
        Test the RFC 9110 comma-merge for the mapping requests needs.
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
        self.assertEqual(b"".join(streamed), b"chunked")

    def test_streaming_body_is_an_iterator(self) -> None:
        """
        Test that a producer body reaches requests as a chunk iterator
        (which requests sends chunked).
        """
        chunks = _request_data(Request("https://example.com/", body=BytesBody(b"x")))
        self.assertIsInstance(chunks, Iterator)

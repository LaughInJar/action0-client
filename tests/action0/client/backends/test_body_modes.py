"""
One test suite for the per-request body modes (preload, stream, spool),
run unchanged against every backend that talks to a real socket: a local
HTTP server plays the API, including a download that breaks off midway.

The Twisted backend is tested with fakes in ``test_twisted.py``.
"""

import gzip
import hashlib
import io
import json
import threading
import unittest
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from typing import ClassVar
from unittest import mock
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from action0.client import BodyMode
from action0.client import RequestOptions
from action0.client import TransportError
from action0.client.backends import _body
from action0.client.backends.aiohttp import AiohttpBackend
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.httpx2 import AsyncHttpx2Backend
from action0.client.backends.httpx2 import Httpx2Backend
from action0.client.backends.requests import RequestsBackend
from action0.client.backends.urllib import UrllibBackend
from action0.client.backends.urllib3 import Urllib3Backend
from action0.req import AsyncIterableBody
from action0.req import IterableBody
from action0.req import Request
from action0.req import Response
from action0.req import SpooledBody

_PATTERN = bytes(range(256)) * 256
"""64 KiB of non-repeating-looking bytes the blobs are cut from."""

REPORT = {"rows": list(range(2000))}
"""The payload behind ``/report.gz``."""


def blob(size: int) -> bytes:
    """
    The deterministic content of ``/blob?size=<size>``.

    :param size: the number of bytes
    :return: the bytes the server sends
    """
    return (_PATTERN * (size // len(_PATTERN) + 1))[:size]


class BodyHandler(BaseHTTPRequestHandler):
    """
    The local test endpoint:

    - ``/blob?size=N`` — N bytes; honours ``Range: bytes=K-`` with a 206.
      With ``&cut=K`` (and no Range header) it announces N bytes, sends
      only the first K and drops the connection.
    - ``/report.gz`` — a gzip file (served as a file, not as a
      content-encoding).
    - ``/empty`` — 204 without a body.
    - ``/error`` — 500 with an HTML body.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        params = {name: values[0] for name, values in parse_qs(url.query).items()}
        if url.path == "/blob":
            self._blob(int(params["size"]), int(params.get("cut", 0)))
        elif url.path == "/report.gz":
            self._send(200, gzip.compress(json.dumps(REPORT).encode()), "application/gzip")
        elif url.path == "/empty":
            self.send_response(204)
            self.end_headers()
        else:
            self._send(500, b"<html>internal error</html>", "text/html")

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _blob(self, size: int, cut: int) -> None:
        data = blob(size)
        requested = self.headers.get("Range")
        if requested is not None:
            start = int(requested.removeprefix("bytes=").rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.send_header("Content-Length", str(size - start))
            self.end_headers()
            self.wfile.write(data[start:])
        elif cut:
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(data[:cut])
            # ending the handler now closes the connection: the client
            # gets fewer bytes than announced
            self.close_connection = True
        else:
            self._send(200, data, "application/octet-stream")

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class QuietServer(ThreadingHTTPServer):
    """A server that does not print when a client drops a connection."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Clients abandoning a stream are part of these tests."""


class ServerTestCase(unittest.TestCase):
    """
    The shared fixture: the local server, and the helpers both suites
    use. Not collected itself — the concrete classes at the end of the
    module are.
    """

    __test__ = False

    server: ClassVar[QuietServer]
    thread: ClassVar[threading.Thread]
    base_url: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = QuietServer(("127.0.0.1", 0), BodyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path: str, mode: "BodyMode | None" = None, **options: Any) -> Request:
        """
        A GET request to the local server, with body options if given.

        :param path: the path and query to request
        :param mode: the body mode to ask for (``None``: the backend's
                     default)
        :param options: further :py:class:`RequestOptions` fields
        :return: the request
        """
        request = Request(f"{self.base_url}{path}")
        if mode is not None or options:
            RequestOptions(body=mode, **options).apply(request)
        return request

    def temporary_path(self, name: str) -> Path:
        """
        A path inside a directory that is removed after the test.

        :param name: the file name
        :return: the (not yet existing) path
        """
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / name

    @contextmanager
    def recorded_spools(self) -> Iterator[list[SpooledBody]]:
        """
        Record the spools the backends create, to look at the ones that
        never make it into a response.
        """
        spools: list[SpooledBody] = []
        real = _body.new_spool

        def recording(request: Request) -> SpooledBody:
            spools.append(real(request))
            return spools[-1]

        with mock.patch.object(_body, "new_spool", recording):
            yield spools


class SyncBodyModes(ServerTestCase):
    """
    The suite for synchronous backends. Subclasses provide
    :py:meth:`open_backend`.
    """

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        """
        Create the backend under test, registered for cleanup.

        :param kwargs: backend constructor arguments (``stream=...``)
        :return: its ``send``, returning the response itself
        """
        raise NotImplementedError

    def test_preloaded_bytes_are_the_default(self) -> None:
        """
        Test that without options the body is plain bytes.
        """
        send = self.open_backend()
        response = send(self.request("/blob?size=1000"))
        self.assertEqual(response.body, blob(1000))

    def test_stream_gives_status_and_headers_before_the_body(self) -> None:
        """
        Test a per-request stream on a default backend: the response is
        there with status and headers, the body is a single-use producer
        read afterwards.
        """
        send = self.open_backend()
        response = send(self.request("/blob?size=200000", BodyMode.STREAM))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Length"], "200000")
        self.assertIsInstance(response.body, IterableBody)
        with response:
            producer = response.body_producer()
            assert producer is not None
            chunks = list(producer.chunks())
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), blob(200000))

    def test_stream_can_be_closed_unread(self) -> None:
        """
        Test that a streamed response nobody reads is released by
        closing it, and that the backend is fine afterwards.
        """
        send = self.open_backend()
        with send(self.request("/blob?size=200000", BodyMode.STREAM)) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(send(self.request("/blob?size=10")).body, blob(10))

    def test_spool_stays_in_memory_when_small(self) -> None:
        """
        Test that a small spooled body never touches the disk, is
        re-readable and offers a seekable file.
        """
        send = self.open_backend()
        with send(self.request("/blob?size=1000", BodyMode.SPOOL)) as response:
            body = response.body
            assert isinstance(body, SpooledBody)
            self.assertFalse(body.rolled)
            self.assertEqual(response.body_bytes(), blob(1000))
            self.assertEqual(response.body_bytes(), blob(1000))
            file = response.body_file()
            assert file is not None
            file.seek(990)
            self.assertEqual(file.read(), blob(1000)[990:])
        self.assertTrue(body.closed)

    def test_spool_moves_to_a_file_beyond_the_threshold(self) -> None:
        """
        Test that a body larger than ``spool_max_memory`` ends up in the
        temporary file, complete, and is removed by closing the response.
        """
        send = self.open_backend()
        request = self.request("/blob?size=300000", BodyMode.SPOOL, spool_max_memory=65536)
        response = send(request)
        body = response.body
        assert isinstance(body, SpooledBody)
        self.assertTrue(body.rolled)
        self.assertEqual(body.content_length(), 300000)
        digest = hashlib.sha256()
        for chunk in body.chunks():
            digest.update(chunk)
        self.assertEqual(digest.digest(), hashlib.sha256(blob(300000)).digest())
        response.close()
        self.assertTrue(body.closed)

    def test_spooled_gzip_file_is_read_through_its_file_object(self) -> None:
        """
        Test the motivating use: a decompressor reading a spooled
        download through the file object.
        """
        send = self.open_backend()
        with send(self.request("/report.gz", BodyMode.SPOOL)) as response:
            with gzip.GzipFile(fileobj=response.body_file()) as report:
                self.assertEqual(json.load(report), REPORT)

    def test_request_option_beats_the_backend_default(self) -> None:
        """
        Test that ``stream=True`` on the backend is only the default: a
        request can still ask for preloaded bytes or a spool.
        """
        send = self.open_backend(stream=True)
        with send(self.request("/blob?size=1000")) as streamed:
            self.assertIsInstance(streamed.body, IterableBody)
            self.assertEqual(streamed.body_bytes(), blob(1000))
        preloaded = send(self.request("/blob?size=1000", BodyMode.PRELOAD))
        self.assertEqual(preloaded.body, blob(1000))
        with send(self.request("/blob?size=1000", BodyMode.SPOOL)) as spooled:
            self.assertIsInstance(spooled.body, SpooledBody)

    def test_empty_body(self) -> None:
        """
        Test that a response without content works in every mode.
        """
        send = self.open_backend()
        for mode in BodyMode:
            with self.subTest(mode=mode), send(self.request("/empty", mode)) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.body_bytes(), b"")

    def test_broken_stream_raises_a_translated_error(self) -> None:
        """
        Test that a stream breaking off midway raises the library's
        TransportError while the body is read — with the HTTP library's
        own exception as its cause and the request attached.
        """
        send = self.open_backend()
        request = self.request("/blob?size=200000&cut=70000", BodyMode.STREAM)
        response = send(request)
        self.assertEqual(response.status, 200)
        received = io.BytesIO()
        with self.assertRaises(TransportError) as raised:
            response.save_body(received)
        self.assertIsNotNone(raised.exception.__cause__)
        self.assertIs(raised.exception.request, request)
        self.assertEqual(received.getvalue(), blob(200000)[: len(received.getvalue())])
        self.assertLess(len(received.getvalue()), 200000)

    def test_broken_spool_fails_the_send_and_leaves_nothing_behind(self) -> None:
        """
        Test that a body breaking off while it is spooled fails ``send``
        with a TransportError and discards the half-filled spool.
        """
        send = self.open_backend()
        request = self.request("/blob?size=200000&cut=70000", BodyMode.SPOOL, spool_max_memory=0)
        with self.recorded_spools() as spools:
            with self.assertRaises(TransportError):
                send(request)
        self.assertEqual(len(spools), 1)
        self.assertTrue(spools[0].closed)

    def test_resumed_download(self) -> None:
        """
        Test the resume flow end to end: a streamed download into a file
        breaks off, a Range request continues it — appending only after
        status and Content-Range have been checked.
        """
        send = self.open_backend()
        path = self.temporary_path("feed.bin")

        with self.assertRaises(TransportError):
            with send(self.request("/blob?size=200000&cut=70000", BodyMode.STREAM)) as first:
                first.save_body(path)
        # how much of the data before the break a library hands out
        # differs (possibly nothing): the file size is what counts
        offset = path.stat().st_size
        self.assertLess(offset, 200000)
        self.assertEqual(path.read_bytes(), blob(200000)[:offset])

        resume = self.request("/blob?size=200000", BodyMode.STREAM)
        if offset:
            resume.headers["Range"] = f"bytes={offset}-"
        with send(resume) as second:
            if offset:
                self.assertEqual(second.status, 206)
                self.assertTrue(second.headers["Content-Range"].startswith(f"bytes {offset}-"))
            else:
                self.assertEqual(second.status, 200)
            self.assertEqual(second.save_body(path, append=True), 200000 - offset)
        self.assertEqual(path.read_bytes(), blob(200000))

    def test_error_body_stays_out_of_the_file(self) -> None:
        """
        Test that with a streamed body the caller decides before a byte
        is written: an error response leaves the file untouched.
        """
        send = self.open_backend()
        path = self.temporary_path("feed.bin")
        path.write_bytes(b"already downloaded")
        with send(self.request("/error", BodyMode.STREAM)) as response:
            if response.is_success:
                response.save_body(path, append=True)
        self.assertEqual(response.status, 500)
        self.assertEqual(path.read_bytes(), b"already downloaded")


class AsyncBodyModes(ServerTestCase, unittest.IsolatedAsyncioTestCase):
    """
    The suite for async backends. Subclasses provide
    :py:meth:`open_backend`.
    """

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Awaitable[Response]]:
        """
        Create the backend under test, registered for cleanup.

        :param kwargs: backend constructor arguments (``stream=...``)
        :return: its ``send``
        """
        raise NotImplementedError

    @staticmethod
    async def read(response: Response) -> bytes:
        """
        Read a response body through its asynchronous chunks.

        :param response: the response to read
        :return: the whole body
        """
        producer = response.body_producer()
        assert producer is not None
        return b"".join([chunk async for chunk in producer.achunks()])

    async def test_preloaded_bytes_are_the_default(self) -> None:
        """
        Test that without options the body is plain bytes.
        """
        send = self.open_backend()
        response = await send(self.request("/blob?size=1000"))
        self.assertEqual(response.body, blob(1000))

    async def test_stream_gives_status_and_headers_before_the_body(self) -> None:
        """
        Test a per-request stream on a default backend: status and
        headers first, the body an async producer read afterwards.
        """
        send = self.open_backend()
        response = await send(self.request("/blob?size=200000", BodyMode.STREAM))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Length"], "200000")
        self.assertIsInstance(response.body, AsyncIterableBody)
        async with response:
            self.assertEqual(await self.read(response), blob(200000))

    async def test_stream_can_be_closed_unread(self) -> None:
        """
        Test that a streamed response nobody reads is released by
        closing it, and that the backend is fine afterwards.
        """
        send = self.open_backend()
        async with await send(self.request("/blob?size=200000", BodyMode.STREAM)) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual((await send(self.request("/blob?size=10"))).body, blob(10))

    async def test_spool_stays_in_memory_when_small(self) -> None:
        """
        Test that a small spooled body never touches the disk and can be
        read synchronously (it is in memory) as well as asynchronously.
        """
        send = self.open_backend()
        async with await send(self.request("/blob?size=1000", BodyMode.SPOOL)) as response:
            body = response.body
            assert isinstance(body, SpooledBody)
            self.assertFalse(body.rolled)
            self.assertEqual(response.body_bytes(), blob(1000))
            self.assertEqual(await self.read(response), blob(1000))
        self.assertTrue(body.closed)

    async def test_spool_writes_to_disk_off_the_event_loop(self) -> None:
        """
        Test the no-blocking promise: while the body fits in memory the
        spool is written inline; the write that moves it to the temporary
        file and every write after it run in a worker thread.
        """
        loop_thread = threading.get_ident()
        max_memory = 65536
        writes: list[tuple[bool, bool]] = []
        real_write = SpooledBody.write

        def recording(spool: SpooledBody, data: bytes) -> int:
            touches_disk = spool.rolled or (spool.content_length() or 0) + len(data) > max_memory
            writes.append((touches_disk, threading.get_ident() == loop_thread))
            return real_write(spool, data)

        send = self.open_backend()
        request = self.request("/blob?size=300000", BodyMode.SPOOL, spool_max_memory=max_memory)
        with mock.patch.object(SpooledBody, "write", recording):
            response = await send(request)

        body = response.body
        assert isinstance(body, SpooledBody)
        self.assertTrue(body.rolled)
        on_disk = [on_loop for touches_disk, on_loop in writes if touches_disk]
        in_memory = [on_loop for touches_disk, on_loop in writes if not touches_disk]
        self.assertTrue(on_disk)
        self.assertFalse(any(on_disk), "a disk write ran on the event loop thread")
        self.assertTrue(all(in_memory), "an in-memory write was sent to a thread")

        self.assertEqual(await self.read(response), blob(300000))
        await response.aclose()
        self.assertTrue(body.closed)

    async def test_spooled_gzip_file_is_read_through_its_file_object(self) -> None:
        """
        Test the motivating use: a decompressor reading a spooled
        download through the file object.
        """
        send = self.open_backend()
        async with await send(self.request("/report.gz", BodyMode.SPOOL)) as response:
            with gzip.GzipFile(fileobj=response.body_file()) as report:
                self.assertEqual(json.load(report), REPORT)

    async def test_request_option_beats_the_backend_default(self) -> None:
        """
        Test that ``stream=True`` on the backend is only the default.
        """
        send = self.open_backend(stream=True)
        async with await send(self.request("/blob?size=1000")) as streamed:
            self.assertIsInstance(streamed.body, AsyncIterableBody)
            self.assertEqual(await self.read(streamed), blob(1000))
        preloaded = await send(self.request("/blob?size=1000", BodyMode.PRELOAD))
        self.assertEqual(preloaded.body, blob(1000))
        async with await send(self.request("/blob?size=1000", BodyMode.SPOOL)) as spooled:
            self.assertIsInstance(spooled.body, SpooledBody)

    async def test_empty_body(self) -> None:
        """
        Test that a response without content works in every mode.
        """
        send = self.open_backend()
        for mode in BodyMode:
            with self.subTest(mode=mode):
                async with await send(self.request("/empty", mode)) as response:
                    self.assertEqual(response.status, 204)
                    self.assertEqual(await self.read(response), b"")

    async def test_broken_stream_raises_a_translated_error(self) -> None:
        """
        Test that a stream breaking off midway raises the library's
        TransportError while the body is read.
        """
        send = self.open_backend()
        request = self.request("/blob?size=200000&cut=70000", BodyMode.STREAM)
        response = await send(request)
        received = io.BytesIO()
        with self.assertRaises(TransportError) as raised:
            await response.asave_body(received)
        self.assertIsNotNone(raised.exception.__cause__)
        self.assertIs(raised.exception.request, request)
        self.assertEqual(received.getvalue(), blob(200000)[: len(received.getvalue())])
        self.assertLess(len(received.getvalue()), 200000)

    async def test_broken_spool_fails_the_send_and_leaves_nothing_behind(self) -> None:
        """
        Test that a body breaking off while it is spooled fails ``send``
        with a TransportError and discards the half-filled spool.
        """
        send = self.open_backend()
        request = self.request("/blob?size=200000&cut=70000", BodyMode.SPOOL, spool_max_memory=0)
        with self.recorded_spools() as spools:
            with self.assertRaises(TransportError):
                await send(request)
        self.assertEqual(len(spools), 1)
        self.assertTrue(spools[0].closed)

    async def test_resumed_download(self) -> None:
        """
        Test the resume flow end to end with ``asave_body``.
        """
        send = self.open_backend()
        path = self.temporary_path("feed.bin")

        with self.assertRaises(TransportError):
            first = await send(self.request("/blob?size=200000&cut=70000", BodyMode.STREAM))
            async with first:
                await first.asave_body(path)
        # how much of the data before the break a library hands out
        # differs: aiohttp drops what it had buffered when the break is
        # noticed before the data was read, so nothing may have arrived
        offset = path.stat().st_size
        self.assertLess(offset, 200000)
        self.assertEqual(path.read_bytes(), blob(200000)[:offset])

        resume = self.request("/blob?size=200000", BodyMode.STREAM)
        if offset:
            resume.headers["Range"] = f"bytes={offset}-"
        async with await send(resume) as second:
            if offset:
                self.assertEqual(second.status, 206)
                self.assertTrue(second.headers["Content-Range"].startswith(f"bytes {offset}-"))
            else:
                self.assertEqual(second.status, 200)
            self.assertEqual(await second.asave_body(path, append=True), 200000 - offset)
        self.assertEqual(path.read_bytes(), blob(200000))

    async def test_error_body_stays_out_of_the_file(self) -> None:
        """
        Test that an error response leaves the download file untouched.
        """
        send = self.open_backend()
        path = self.temporary_path("feed.bin")
        path.write_bytes(b"already downloaded")
        async with await send(self.request("/error", BodyMode.STREAM)) as response:
            if response.is_success:
                await response.asave_body(path, append=True)
        self.assertEqual(response.status, 500)
        self.assertEqual(path.read_bytes(), b"already downloaded")


class RequestsBodyModesTestCase(SyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.requests.RequestsBackend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        backend = RequestsBackend(**kwargs)
        self.addCleanup(backend.close)
        return backend.send


class Urllib3BodyModesTestCase(SyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.urllib3.Urllib3Backend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        # retries=False: urllib3's own retries would hide the broken download
        backend = Urllib3Backend(retries=False, **kwargs)
        self.addCleanup(backend.close)
        return backend.send


class UrllibBodyModesTestCase(SyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.urllib.UrllibBackend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        return UrllibBackend(**kwargs).send


class HttpxBodyModesTestCase(SyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.httpx.HttpxBackend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        backend = HttpxBackend(**kwargs)
        self.addCleanup(backend.close)
        return backend.send


class Httpx2BodyModesTestCase(SyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.httpx2.Httpx2Backend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        backend = Httpx2Backend(**kwargs)
        self.addCleanup(backend.close)
        return backend.send


class ThreadPoolBodyModesTestCase(SyncBodyModes):
    """
    the body modes through
    :py:class:`~action0.client.backends.futures.ThreadPoolBackend` (around
    the stdlib backend): the options ride on the request into the worker
    thread
    """

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Response]:
        backend = ThreadPoolBackend(UrllibBackend(**kwargs))
        self.addCleanup(backend.close)
        return lambda request: backend.send(request).result(timeout=10)


class AsyncHttpxBodyModesTestCase(AsyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Awaitable[Response]]:
        backend = AsyncHttpxBackend(**kwargs)
        self.addAsyncCleanup(backend.aclose)
        return backend.send


class AsyncHttpx2BodyModesTestCase(AsyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Awaitable[Response]]:
        backend = AsyncHttpx2Backend(**kwargs)
        self.addAsyncCleanup(backend.aclose)
        return backend.send


class AiohttpBodyModesTestCase(AsyncBodyModes):
    """the body modes on :py:class:`~action0.client.backends.aiohttp.AiohttpBackend`"""

    __test__ = True

    def open_backend(self, **kwargs: Any) -> Callable[[Request], Awaitable[Response]]:
        backend = AiohttpBackend(**kwargs)
        self.addAsyncCleanup(backend.aclose)
        return backend.send

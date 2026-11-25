"""
One async backend object, several event loops — over real sockets, where
the loop affinity of the HTTP libraries actually bites: a connection
opened on one loop cannot be used on another.

Run unchanged against the httpx, httpx2 and aiohttp backends.
"""

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import ClassVar

import aiohttp
import httpx
import httpx2

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client.backends.aiohttp import AiohttpBackend
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx2 import AsyncHttpx2Backend
from action0.req import Request


class PingHandler(BaseHTTPRequestHandler):
    """The local test endpoint: answers every GET with a small JSON body."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = b'{"pong": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class QuietServer(ThreadingHTTPServer):
    """A server that does not print when a client drops a connection."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Dropped keep-alive connections are part of these tests."""


class Ping(JsonOperation[Any]):
    """The example operation."""

    path = "/ping"


class EventLoopSuite(unittest.TestCase):
    """
    The suite. Subclasses provide the backend; this class is not
    collected itself.
    """

    __test__ = False

    server: ClassVar[QuietServer]
    thread: ClassVar[threading.Thread]
    base_url: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = QuietServer(("127.0.0.1", 0), PingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def shared_backend(self) -> Any:
        """
        :return: a backend that owns its clients (none passed in)
        """
        raise NotImplementedError

    async def bound_backend(self) -> "tuple[Any, Any]":
        """
        :return: a backend given a single client created inside the
                 running loop, and the coroutine function closing that
                 client
        """
        raise NotImplementedError

    def test_consecutive_asyncio_runs(self) -> None:
        """
        Test the case that used to fail with "Event loop is closed": one
        long-lived backend, a fresh event loop for every call.
        """
        client = APIClient(self.shared_backend(), self.base_url)

        async def call() -> Any:
            return await client.send(Ping())

        for _ in range(3):
            self.assertEqual(asyncio.run(call()), {"pong": True})

    def test_two_threads_each_with_their_own_loop(self) -> None:
        """
        Test one backend shared by two worker threads that each run
        their own loop, with both loops alive (and holding a keep-alive
        connection) at the same time.
        """
        client = APIClient(self.shared_backend(), self.base_url)
        both_running = threading.Barrier(2, timeout=10)
        results: dict[str, Any] = {}

        async def calls() -> list[Any]:
            first = await client.send(Ping())
            await asyncio.to_thread(both_running.wait)
            return [first, await client.send(Ping())]

        def work(name: str) -> None:
            try:
                results[name] = asyncio.run(calls())
            except BaseException as error:  # reported by the assertion below
                results[name] = error

        threads = [threading.Thread(target=work, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(results, {"a": [{"pong": True}] * 2, "b": [{"pong": True}] * 2})

    def test_explicit_close_between_runs(self) -> None:
        """
        Test that closing the backend inside a loop is fine and leaves it
        usable: the next loop gets a new client.
        """
        backend = self.shared_backend()

        async def call() -> int:
            async with backend:
                status: int = (await backend.send(Request(f"{self.base_url}/ping"))).status
                return status

        self.assertEqual(asyncio.run(call()), 200)
        self.assertEqual(asyncio.run(call()), 200)

    def test_single_client_refuses_a_foreign_loop(self) -> None:
        """
        Test the clear error for a backend given one client: it works on
        the loop of its first use — before and after — while a use from
        another loop gets a RuntimeError naming the way out, never the
        HTTP library's internal failure.
        """
        request = Request(f"{self.base_url}/ping")
        first_use_done = threading.Event()
        foreign_use_done = threading.Event()
        seen: dict[str, Any] = {}

        async def owner() -> None:
            backend, close = await self.bound_backend()
            try:
                seen["backend"] = backend
                seen["before"] = (await backend.send(request)).status
                first_use_done.set()
                await asyncio.to_thread(foreign_use_done.wait, 10)
                seen["after"] = (await backend.send(request)).status
            finally:
                first_use_done.set()
                await close()

        async def intruder() -> None:
            await seen["backend"].send(request)

        thread = threading.Thread(target=lambda: asyncio.run(owner()))
        thread.start()
        try:
            self.assertTrue(first_use_done.wait(10))
            with self.assertRaisesRegex(RuntimeError, "_factory") as raised:
                asyncio.run(intruder())
        finally:
            foreign_use_done.set()
            thread.join(timeout=20)

        self.assertIn("bound to the event loop", str(raised.exception))
        self.assertIn("is a different one", str(raised.exception))
        self.assertEqual((seen["before"], seen["after"]), (200, 200))


class HttpxEventLoopTestCase(EventLoopSuite):
    """the suite on :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend`"""

    __test__ = True

    def shared_backend(self) -> Any:
        return AsyncHttpxBackend()

    async def bound_backend(self) -> "tuple[Any, Any]":
        client = httpx.AsyncClient()
        return AsyncHttpxBackend(client), client.aclose


class Httpx2EventLoopTestCase(EventLoopSuite):
    """the suite on :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`"""

    __test__ = True

    def shared_backend(self) -> Any:
        return AsyncHttpx2Backend()

    async def bound_backend(self) -> "tuple[Any, Any]":
        client = httpx2.AsyncClient()
        return AsyncHttpx2Backend(client), client.aclose


class AiohttpEventLoopTestCase(EventLoopSuite):
    """the suite on :py:class:`~action0.client.backends.aiohttp.AiohttpBackend`"""

    __test__ = True

    def shared_backend(self) -> Any:
        return AiohttpBackend()

    async def bound_backend(self) -> "tuple[Any, Any]":
        session = aiohttp.ClientSession()
        return AiohttpBackend(session), session.close

    def test_session_and_factory_exclude_each_other(self) -> None:
        """
        Test that the two ways of providing sessions cannot be mixed.
        """

        async def build() -> None:
            async with aiohttp.ClientSession() as session:
                AiohttpBackend(session, session_factory=aiohttp.ClientSession)

        with self.assertRaises(TypeError):
            asyncio.run(build())

    def test_session_factory(self) -> None:
        """
        Test that the factory creates the per-loop sessions: one per
        loop, inside it, closed when the loop shuts down.
        """
        created: list[aiohttp.ClientSession] = []

        def factory() -> aiohttp.ClientSession:
            created.append(aiohttp.ClientSession(headers={"X-From": "factory"}))
            return created[-1]

        backend = AiohttpBackend(session_factory=factory)

        async def call() -> int:
            await backend.send(Request(f"{self.base_url}/ping"))
            status: int = (await backend.send(Request(f"{self.base_url}/ping"))).status
            return status

        self.assertEqual(asyncio.run(call()), 200)
        self.assertEqual(asyncio.run(call()), 200)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(session.closed for session in created))

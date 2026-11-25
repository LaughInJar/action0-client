"""
One test suite for the httpx-family backends, run unchanged against
httpx and against httpx2 over each library's own ``MockTransport`` — the
two share their implementation, and this is where that is held to it.
"""

import asyncio
import threading
import unittest
from typing import Any
from typing import ClassVar

import httpx
import httpx2

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.httpx2 import AsyncHttpx2Backend
from action0.client.backends.httpx2 import Httpx2Backend
from action0.req import Request
from action0.req.body import BodyProducer
from action0.req.body import IterableBody

URL = "https://api.example.com/items"

# the two library modules as plain values: the suites are written against
# "the library under test", whichever it is
HTTPX: Any = httpx
HTTPX2: Any = httpx2


class FamilyTestCase(unittest.TestCase):
    """
    What the suites need to know about the library under test. The
    concrete classes at the end of the module fill it in; this class is
    not collected itself.
    """

    __test__ = False

    lib: ClassVar[Any]
    """The library module: ``httpx`` or ``httpx2``."""

    other: ClassVar[Any]
    """The other library of the family."""

    backend_class: ClassVar[Any]
    async_backend_class: ClassVar[Any]

    def echo(self, request: Any) -> Any:
        """
        The mock transport endpoint: echoes the request back as headers
        and body, plus repeated Set-Cookie lines.

        :param request: the library's request the transport received
        :return: the canned response
        """
        return self.lib.Response(
            200,
            headers=[
                ("X-Method", request.method),
                ("X-Url", str(request.url)),
                ("X-Tag", request.headers.get("X-Tag", "")),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
            content=request.read(),
        )

    def transport(self, handler: Any = None) -> Any:
        """
        A mock transport of the library under test.

        :param handler: the handler to use instead of the echo one
        :return: the transport
        """
        return self.lib.MockTransport(handler or self.echo)


class SyncSuite(FamilyTestCase):
    """
    The suite for the synchronous backend of the library under test.
    """

    def backend(self, handler: Any = None, **options: Any) -> Any:
        """
        A backend over the echo transport (or a custom handler).

        :param handler: the handler to use instead of the echo one
        :param options: further backend constructor arguments
        :return: the ready-made backend
        """
        client = self.lib.Client(transport=self.transport(handler))
        self.addCleanup(client.close)
        return self.backend_class(client, **options)

    def test_round_trip_conversion(self) -> None:
        """
        Test request and response conversion in one pass.
        """
        request = Request(
            URL, "POST", query={"page": 2}, headers={"X-Tag": "yes"}, body='{"a": 1}'
        )
        response = self.backend().send(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.http_version, "HTTP/1.1")
        self.assertEqual(response.headers["X-Method"], "POST")
        self.assertEqual(response.headers["X-Url"], "https://api.example.com/items?page=2")
        self.assertEqual(response.headers["X-Tag"], "yes")
        self.assertEqual(response.body_bytes(), b'{"a": 1}')
        self.assertIs(response.request, request)

    def test_multi_value_response_headers_are_preserved(self) -> None:
        """
        Test that repeated header lines survive the conversion.
        """
        response = self.backend().send(Request(URL))
        self.assertEqual(response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])

    def test_streaming_request_body(self) -> None:
        """
        Test that a BodyProducer body is streamed through the library.
        """
        request = Request(URL, "PUT", body=IterableBody([b"chu", b"nks"]))
        self.assertEqual(self.backend().send(request).body_bytes(), b"chunks")

    def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as a producer and yields the
        same content a preloading send would.
        """
        with self.backend(stream=True) as backend:
            response = backend.send(Request(URL, body="ping"))
            self.assertIsInstance(response.body, BodyProducer)
            self.assertEqual(response.body_bytes(), b"ping")

    def test_timeout_is_translated(self) -> None:
        """
        Test that the library's timeouts surface as the library-neutral
        TimeoutError, with the original as its cause.
        """

        def sleepy(request: Any) -> Any:
            raise self.lib.ReadTimeout("too slow", request=request)

        with self.assertRaises(TimeoutError) as caught:
            self.backend(sleepy).send(Request(URL))
        self.assertIsInstance(caught.exception.__cause__, self.lib.ReadTimeout)

    def test_transport_error_is_translated(self) -> None:
        """
        Test that the library's transport failures surface as
        TransportError.
        """

        def refuse(request: Any) -> Any:
            raise self.lib.ConnectError("refused", request=request)

        with self.assertRaises(TransportError) as caught:
            self.backend(refuse).send(Request(URL))
        self.assertNotIsInstance(caught.exception, TimeoutError)

    def test_exceptions_of_the_other_library_are_not_mistaken(self) -> None:
        """
        Test that each backend recognises the exception classes of its
        own library only: the other library's error is none of its
        business and passes through untouched.
        """

        def foreign(request: Any) -> Any:
            raise self.other.ReadTimeout("not ours")

        with self.assertRaises(self.other.ReadTimeout):
            self.backend(foreign).send(Request(URL))

    def test_redirects_are_followed_by_created_clients(self) -> None:
        """
        Test the follow_redirects default on the client the backend
        creates for itself.
        """

        def redirect(request: Any) -> Any:
            if request.url.path == "/old":
                return self.lib.Response(302, headers={"Location": "/new"})
            return self.lib.Response(200, content=b"moved in")

        with self.backend_class(timeout=1) as backend:
            # swap the transport of the created client, keeping its config
            backend.client._transport = self.transport(redirect)
            response = backend.send(Request("https://api.example.com/old"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body_bytes(), b"moved in")

    def test_client_is_exposed(self) -> None:
        """
        Test that the backend hands out the client it sends through: the
        one passed in, or the library's own class when it created one.
        """
        client = self.lib.Client(transport=self.transport())
        self.addCleanup(client.close)
        self.assertIs(self.backend_class(client).client, client)
        with self.backend_class() as created:
            self.assertIsInstance(created.client, self.lib.Client)

    def test_close_semantics(self) -> None:
        """
        Test that an owned client is closed and a passed one is not.
        """
        client = self.lib.Client(transport=self.transport())
        self.backend_class(client).close()
        self.assertFalse(client.is_closed)
        client.close()

        owned = self.backend_class()
        owned.close()
        self.assertTrue(owned.client.is_closed)

    def test_client_of_the_other_library_is_refused(self) -> None:
        """
        Test that a client of the other library is rejected when the
        backend is built — it would work until the first error, which
        the backend would not recognise.
        """
        foreign = self.other.Client()
        self.addCleanup(foreign.close)
        with self.assertRaisesRegex(TypeError, self.lib.__name__ + r"\.Client"):
            self.backend_class(foreign)


class AsyncSuite(FamilyTestCase, unittest.IsolatedAsyncioTestCase):
    """
    The suite for the async backend of the library under test, on the
    test's own event loop.
    """

    def backend(self, handler: Any = None, **options: Any) -> Any:
        """
        A backend over the echo transport (or a custom handler).

        :param handler: the handler to use instead of the echo one
        :param options: further backend constructor arguments
        :return: the ready-made backend
        """
        client = self.lib.AsyncClient(transport=self.transport(handler))
        self.addAsyncCleanup(client.aclose)
        return self.async_backend_class(client, **options)

    async def test_round_trip_conversion(self) -> None:
        """
        Test the async request/response conversion.
        """
        request = Request(URL, "POST", body=b"payload")
        response = await self.backend().send(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Method"], "POST")
        self.assertEqual(response.body_bytes(), b"payload")
        self.assertIs(response.request, request)

    async def test_streaming_request_body(self) -> None:
        """
        Test that a BodyProducer body is streamed via the async iterator.
        """
        request = Request(URL, "PUT", body=IterableBody([b"chu", b"nks"]))
        self.assertEqual((await self.backend().send(request)).body_bytes(), b"chunks")

    async def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as an async producer; the
        chunks stream, the sync accessors refuse.
        """
        async with self.backend(stream=True) as backend:
            response = await backend.send(Request(URL, body="ping"))
            self.assertIsInstance(response.body, BodyProducer)

            producer = response.body_producer()
            assert producer is not None
            chunks = [chunk async for chunk in producer.achunks()]
            self.assertEqual(b"".join(chunks), b"ping")
            with self.assertRaises(RuntimeError):
                response.body_bytes()  # an async body has no sync view

    async def test_timeout_is_translated(self) -> None:
        """
        Test that the library's timeouts surface as TimeoutError.
        """

        def sleepy(request: Any) -> Any:
            raise self.lib.PoolTimeout("no connection available", request=request)

        with self.assertRaises(TimeoutError) as caught:
            await self.backend(sleepy).send(Request(URL))
        self.assertIsInstance(caught.exception.__cause__, self.lib.PoolTimeout)

    async def test_exceptions_of_the_other_library_are_not_mistaken(self) -> None:
        """
        Test that the other library's error passes through untouched.
        """

        def foreign(request: Any) -> Any:
            raise self.other.ConnectError("not ours")

        with self.assertRaises(self.other.ConnectError):
            await self.backend(foreign).send(Request(URL))

    async def test_passed_client_is_exposed_and_never_closed(self) -> None:
        """
        Test that a client that was passed in is the one handed out, and
        that closing the backend leaves it open.
        """
        client = self.lib.AsyncClient(transport=self.transport())
        async with self.async_backend_class(client) as backend:
            self.assertIs(await backend.current_client(), client)
        self.assertFalse(client.is_closed)
        await client.aclose()

    async def test_own_client_is_created_per_loop_and_closed(self) -> None:
        """
        Test the default: the backend creates a client of its library on
        first use, reuses it within the loop, and aclose() closes it —
        after which the next send simply gets a new one.
        """
        backend = self.async_backend_class()
        first = await backend.current_client()
        self.assertIsInstance(first, self.lib.AsyncClient)
        self.assertIs(await backend.current_client(), first)

        await backend.aclose()
        self.assertTrue(first.is_closed)
        await backend.aclose()  # nothing left to close: a no-op

        second = await backend.current_client()
        self.assertIsNot(second, first)
        await backend.aclose()
        self.assertTrue(second.is_closed)

    async def test_client_and_factory_exclude_each_other(self) -> None:
        """
        Test that the two ways of providing clients cannot be mixed.
        """
        client = self.lib.AsyncClient()
        self.addAsyncCleanup(client.aclose)
        with self.assertRaises(TypeError):
            self.async_backend_class(client, client_factory=self.lib.AsyncClient)

    async def test_client_of_the_other_library_is_refused(self) -> None:
        """
        Test that an async client of the other library is rejected.
        """
        foreign = self.other.AsyncClient()
        self.addAsyncCleanup(foreign.aclose)
        with self.assertRaisesRegex(TypeError, self.lib.__name__ + r"\.AsyncClient"):
            self.async_backend_class(foreign)


class EventLoopSuite(FamilyTestCase):
    """
    The suite for one async backend object used from more than one event
    loop — the loops are the tests' own, so these are plain test methods.
    """

    def setUp(self) -> None:
        self.created: list[Any] = []

    def factory(self) -> Any:
        """
        The client factory handed to the backend: clients over the echo
        transport, recorded in creation order.

        :return: a new async client
        """
        client = self.lib.AsyncClient(transport=self.transport())
        self.created.append(client)
        return client

    def shared_backend(self) -> Any:
        """
        :return: a backend creating its clients with :py:meth:`factory`
        """
        return self.async_backend_class(client_factory=self.factory)

    def test_consecutive_asyncio_runs(self) -> None:
        """
        Test the long-lived backend with ``asyncio.run`` per call: every
        run works, gets its own client, and that client is closed when
        its loop shuts down — nothing piles up.
        """
        backend = self.shared_backend()

        async def call() -> int:
            status: int = (await backend.send(Request(URL))).status
            return status

        for expected in (1, 2, 3):
            self.assertEqual(asyncio.run(call()), 200)
            self.assertEqual(len(self.created), expected)
            self.assertTrue(self.created[-1].is_closed)
        self.assertEqual(len(backend._clients), 0)

    def test_one_client_within_a_loop(self) -> None:
        """
        Test that sends in the same loop — also concurrent first ones —
        share one client.
        """
        backend = self.shared_backend()

        async def calls() -> list[int]:
            responses = await asyncio.gather(*(backend.send(Request(URL)) for _ in range(5)))
            responses.append(await backend.send(Request(URL)))
            return [response.status for response in responses]

        self.assertEqual(asyncio.run(calls()), [200] * 6)
        self.assertEqual(len(self.created), 1)

    def test_two_threads_each_with_their_own_loop(self) -> None:
        """
        Test one backend used from two threads whose loops are alive at
        the same time: each loop gets its own client, both work.
        """
        backend = self.shared_backend()
        both_running = threading.Barrier(2, timeout=10)
        results: dict[str, Any] = {}

        async def call(name: str) -> None:
            first = await backend.send(Request(URL))
            # hold this loop open until the other thread's loop has sent too
            await asyncio.to_thread(both_running.wait)
            second = await backend.send(Request(URL))
            results[name] = (first.status, second.status, await backend.current_client())

        def work(name: str) -> None:
            try:
                asyncio.run(call(name))
            except BaseException as error:  # reported by the assertions below
                results[name] = error

        threads = [threading.Thread(target=work, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(results["a"][:2], (200, 200))
        self.assertEqual(results["b"][:2], (200, 200))
        self.assertIsNot(results["a"][2], results["b"][2])
        self.assertEqual(len(self.created), 2)
        self.assertTrue(all(client.is_closed for client in self.created))

    def test_loop_closed_without_shutting_down_its_generators(self) -> None:
        """
        Test the fallback for a loop that is closed the hard way: its
        client cannot be closed any more, but its entry is dropped on the
        next use and nothing breaks.
        """
        backend = self.shared_backend()
        loop = asyncio.new_event_loop()
        try:
            self.assertEqual(loop.run_until_complete(backend.send(Request(URL))).status, 200)
        finally:
            loop.close()
        self.assertEqual(len(backend._clients), 1)

        async def call() -> int:
            status: int = (await backend.send(Request(URL))).status
            self.assertEqual(len(backend._clients), 1)  # only this loop's
            return status

        self.assertEqual(asyncio.run(call()), 200)
        self.assertEqual(len(backend._clients), 0)

    def test_single_client_refuses_a_foreign_loop(self) -> None:
        """
        Test that a backend given one client says clearly what is wrong
        when it is used from a second event loop — and how to fix it —
        instead of failing somewhere inside the HTTP library.
        """
        client = self.lib.AsyncClient(transport=self.transport())
        backend = self.async_backend_class(client)

        async def call() -> int:
            status: int = (await backend.send(Request(URL))).status
            return status

        self.assertEqual(asyncio.run(call()), 200)
        with self.assertRaisesRegex(RuntimeError, "client_factory") as raised:
            asyncio.run(call())
        self.assertIn("has been closed", str(raised.exception))
        self.assertIn(type(backend).__name__, str(raised.exception))


class HttpxSyncTestCase(SyncSuite):
    """the sync suite on :py:class:`~action0.client.backends.httpx.HttpxBackend`"""

    __test__ = True
    lib, other = HTTPX, HTTPX2
    backend_class, async_backend_class = HttpxBackend, AsyncHttpxBackend


class HttpxAsyncTestCase(AsyncSuite):
    """the async suite on :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend`"""

    __test__ = True
    lib, other = HTTPX, HTTPX2
    backend_class, async_backend_class = HttpxBackend, AsyncHttpxBackend


class HttpxEventLoopTestCase(EventLoopSuite):
    """the event-loop suite on :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend`"""

    __test__ = True
    lib, other = HTTPX, HTTPX2
    backend_class, async_backend_class = HttpxBackend, AsyncHttpxBackend


class Httpx2SyncTestCase(SyncSuite):
    """the sync suite on :py:class:`~action0.client.backends.httpx2.Httpx2Backend`"""

    __test__ = True
    lib, other = HTTPX2, HTTPX
    backend_class, async_backend_class = Httpx2Backend, AsyncHttpx2Backend


class Httpx2AsyncTestCase(AsyncSuite):
    """the async suite on :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`"""

    __test__ = True
    lib, other = HTTPX2, HTTPX
    backend_class, async_backend_class = Httpx2Backend, AsyncHttpx2Backend


class Httpx2EventLoopTestCase(EventLoopSuite):
    """the event-loop suite on :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`"""

    __test__ = True
    lib, other = HTTPX2, HTTPX
    backend_class, async_backend_class = Httpx2Backend, AsyncHttpx2Backend

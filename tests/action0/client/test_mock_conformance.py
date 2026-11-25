"""
The proof that mocking does not depend on the backend: ONE suite of
tests, written against the mock router only, run unchanged against every
backend the library ships — and expecting the same results from each.

A backend is described by three things: how to build it, how to get the
value out of what its ``send`` returns, and how to read a response body.
"""

import asyncio
import unittest
from typing import Any

from twisted.internet.task import Clock

from action0.client import APIClient
from action0.client import BodyMode
from action0.client import Client
from action0.client import Hook
from action0.client import JsonOperation
from action0.client import RequestOptions
from action0.client import TimeoutError
from action0.client import TransportError
from action0.client import json_field
from action0.client import path_param
from action0.client.backends.aiohttp import AiohttpBackend
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.httpx2 import AsyncHttpx2Backend
from action0.client.backends.httpx2 import Httpx2Backend
from action0.client.backends.requests import RequestsBackend
from action0.client.backends.twisted import TwistedBackend
from action0.client.backends.urllib import UrllibBackend
from action0.client.backends.urllib3 import Urllib3Backend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import MockRouter
from action0.client.testing import StubBackend
from action0.client.testing import UnmockedRequestError
from action0.client.testing import deferred_result
from action0.req import Method
from action0.req import Request
from action0.req import Response
from action0.req import SpooledBody

API = "https://api.example.com/v1"


class GetItem(JsonOperation[Any]):
    """A typed GET."""

    path = "/items/{item_id}"
    item_id: int = path_param()


class Translate(JsonOperation[Any]):
    """A typed POST with a JSON body."""

    method = Method.POST
    path = "/translate"
    text: str = json_field()


class Recording(Hook):
    """A hook noting what it is told."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_request(self, request: Request) -> None:
        self.events.append(f"request {request.method} {request.url.path}")

    def on_response(self, request: Request, response: Response, elapsed: float) -> None:
        self.events.append(f"response {response.status}")

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        self.events.append(f"error {type(error).__name__}")


class Conformance(unittest.TestCase):
    """
    The suite. Not collected itself — the classes at the end of the
    module, one per backend, are.
    """

    __test__ = False

    streams = True
    """Whether the backend can hand out a streamed response body at all
    (the Twisted backend cannot, and says so — mocked or not)."""

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        """
        Build the backend under test.

        :param hooks: the hooks to run around its sends
        :return: the backend
        """
        raise NotImplementedError

    def result(self, sent: Any) -> Any:
        """
        The value inside what a ``send`` of this execution model returns.

        :param sent: the return value of ``send``
        :return: the response or parsed result (raises what it failed
                 with)
        """
        raise NotImplementedError

    def read(self, response: Response) -> bytes:
        """
        Read a response body completely, streamed or not.

        :param response: the response
        :return: the body bytes
        """
        producer = response.body_producer()
        return b"".join(producer.chunks()) if producer is not None else b""

    def setUp(self) -> None:
        self.hook = Recording()
        self.mock = MockRouter(assert_all_called=False)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.subject = self.backend([self.hook])

    def send(self, request: Request) -> Response:
        """Send a raw request through the backend under test."""
        response: Response = self.result(Client(self.subject).send(request))
        return response

    def test_typed_operation(self) -> None:
        """
        Test the everyday case: an operation sent through an APIClient
        gets the mocked answer, parsed, and the route recorded the
        request as the backend would have sent it.
        """
        route = self.mock.get(f"{API}/items/{{item_id}}").respond(json={"id": 7, "name": "Thing"})
        client = APIClient(self.subject, API, headers={"X-Client": "conformance"})

        self.assertEqual(self.result(client.send(GetItem(item_id=7))), {"id": 7, "name": "Thing"})

        self.assertEqual(route.call_count, 1)
        recorded = route.calls.last.request
        self.assertEqual(recorded.method, "GET")
        self.assertEqual(recorded.url.as_str(), f"{API}/items/7")
        self.assertEqual(recorded.headers["X-Client"], "conformance")
        self.assertEqual(recorded.headers["Accept"], "application/json")

    def test_matching_on_the_body(self) -> None:
        """
        Test that requests are told apart by their JSON body.
        """
        self.mock.post(f"{API}/translate", json={"text": "hello"}).respond(json={"text": "hallo"})
        self.mock.post(f"{API}/translate", json={"text": "world"}).respond(json={"text": "welt"})
        client = APIClient(self.subject, API)

        self.assertEqual(self.result(client.send(Translate(text="world"))), {"text": "welt"})
        self.assertEqual(self.result(client.send(Translate(text="hello"))), {"text": "hallo"})

    def test_unmocked_request_fails(self) -> None:
        """
        Test that a request no route matches fails with the router's
        assertion error — not with anything from the network.
        """
        self.mock.get(f"{API}/items/7").respond()
        with self.assertRaises(UnmockedRequestError):
            self.send(Request(f"{API}/items/8"))
        self.assertEqual([request.url.path for request in self.mock.unmatched], ["/v1/items/8"])
        self.assertEqual(
            self.hook.events, ["request GET /v1/items/8", "error UnmockedRequestError"]
        )

    def test_mocked_network_failure(self) -> None:
        """
        Test that a mocked timeout reaches the caller as the library's
        TimeoutError with the request attached, through the error hooks.
        """
        self.mock.get(f"{API}/slow").mock(side_effect=TimeoutError("too slow"))
        request = Request(f"{API}/slow")
        with self.assertRaises(TimeoutError) as raised:
            self.send(request)
        self.assertIsInstance(raised.exception, TransportError)
        self.assertEqual(str(raised.exception), "too slow")
        self.assertIsNotNone(raised.exception.request)
        self.assertEqual(self.hook.events, ["request GET /v1/slow", "error TimeoutError"])

    def test_sequence_of_answers(self) -> None:
        """
        Test that a sequence is handed out one answer per request.
        """
        self.mock.get(f"{API}/flaky").mock(
            side_effect=[Response(503), Response(200, body="recovered")]
        )
        first = self.send(Request(f"{API}/flaky"))
        second = self.send(Request(f"{API}/flaky"))
        self.assertEqual((first.status, second.status), (503, 200))
        self.assertEqual(self.read(second), b"recovered")
        self.assertEqual(
            self.hook.events,
            ["request GET /v1/flaky", "response 503", "request GET /v1/flaky", "response 200"],
        )

    def test_callable_answers(self) -> None:
        """
        Test dynamic answers from a plain and from an async callable.
        """

        def echo(request: Request, name: str) -> Response:
            return Response(200, headers={"X-Method": request.method}, body=f"sync {name}")

        async def aecho(request: Request, name: str) -> Response:
            return Response(200, body=f"async {name}")

        self.mock.get(f"{API}/sync/{{name}}").mock(side_effect=echo)
        self.mock.get(f"{API}/async/{{name}}").mock(side_effect=aecho)

        plain = self.send(Request(f"{API}/sync/a"))
        self.assertEqual((self.read(plain), plain.headers["X-Method"]), (b"sync a", "GET"))
        self.assertEqual(self.read(self.send(Request(f"{API}/async/b"))), b"async b")

    def test_response_details(self) -> None:
        """
        Test that status, reason, headers (repeated ones too) and the
        request reference of a mocked response arrive unchanged.
        """
        canned = Response(
            418,
            headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-One", "1")],
            body=b"\x00binary\xff",
            reason="Teapot",
        )
        self.mock.get(f"{API}/tea").mock(return_value=canned)
        request = Request(f"{API}/tea")
        response = self.send(request)
        self.assertEqual((response.status, response.phrase), (418, "Teapot"))
        self.assertEqual(response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])
        self.assertEqual(response.body, b"\x00binary\xff")
        self.assertIs(response.request, request)

    def test_spooled_body(self) -> None:
        """
        Test that a request asking for a spooled body gets one from the
        router, on every backend.
        """
        self.mock.get(f"{API}/feed").respond(chunks=[b"first, ", b"second"])
        request = RequestOptions(body=BodyMode.SPOOL, spool_max_memory=4).apply(
            Request(f"{API}/feed")
        )
        response = self.send(request)
        body = response.body
        assert isinstance(body, SpooledBody)
        self.assertTrue(body.rolled)
        file = response.body_file()
        assert file is not None
        self.assertEqual(file.read(), b"first, second")
        response.close()
        self.assertTrue(body.closed)

    def test_broken_spooled_body_fails_the_send(self) -> None:
        """
        Test that a mocked body breaking off fails a spooled (and a
        preloaded) send as a whole.
        """
        self.mock.get(f"{API}/feed").respond(chunks=[b"first", TransportError("connection lost")])
        for mode in (BodyMode.SPOOL, BodyMode.PRELOAD):
            with self.subTest(mode=mode):
                request = RequestOptions(body=mode).apply(Request(f"{API}/feed"))
                with self.assertRaisesRegex(TransportError, "connection lost"):
                    self.send(request)

    def test_streamed_body(self) -> None:
        """
        Test a streamed body, intact and breaking off midway — or, on a
        backend without a streamed mode, the same refusal a real send
        gets.
        """
        self.mock.get(f"{API}/feed").respond(chunks=[b"first, ", b"second"])
        self.mock.get(f"{API}/broken").respond(
            chunks=[b"first", TransportError("connection lost")]
        )
        whole = RequestOptions(body=BodyMode.STREAM).apply(Request(f"{API}/feed"))
        broken = RequestOptions(body=BodyMode.STREAM).apply(Request(f"{API}/broken"))

        if not self.streams:
            with self.assertRaisesRegex(ValueError, "cannot stream"):
                self.send(whole)
            return

        self.assertEqual(self.read(self.send(whole)), b"first, second")
        response = self.send(broken)  # status and headers arrive ...
        self.assertEqual(response.status, 200)
        with self.assertRaisesRegex(TransportError, "connection lost"):
            self.read(response)  # ... the break shows while reading

    def test_no_connection_is_opened(self) -> None:
        """
        Test that a mocked send does no network I/O at all: it works
        with every connection attempt blocked.
        """
        with MockRouter(block_network=True) as blocking:
            blocking.get(f"{API}/items/7").respond(json={"id": 7})
            client = APIClient(self.subject, API)
            self.assertEqual(self.result(client.send(GetItem(item_id=7))), {"id": 7})


class SyncModel(Conformance):
    """Backends whose ``send`` returns the response itself."""

    def result(self, sent: Any) -> Any:
        return sent


class AsyncModel(Conformance):
    """Backends whose ``send`` returns an awaitable."""

    def result(self, sent: Any) -> Any:
        async def main() -> Any:
            return await sent

        return asyncio.run(main())

    def read(self, response: Response) -> bytes:
        async def main() -> bytes:
            producer = response.body_producer()
            if producer is None:
                return b""
            return b"".join([chunk async for chunk in producer.achunks()])

        return asyncio.run(main())


class DeferredModel(Conformance):
    """Backends whose ``send`` returns a Deferred."""

    def result(self, sent: Any) -> Any:
        return deferred_result(sent)


class FuturesModel(Conformance):
    """Backends whose ``send`` returns a Future."""

    def result(self, sent: Any) -> Any:
        return sent.result(timeout=10)


class StubBackendConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.testing.StubBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return StubBackend(hooks=hooks or [])


class RequestsConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.backends.requests.RequestsBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        backend = RequestsBackend(hooks=hooks or [])
        self.addCleanup(backend.close)
        return backend


class Urllib3ConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.backends.urllib3.Urllib3Backend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        backend = Urllib3Backend(hooks=hooks or [])
        self.addCleanup(backend.close)
        return backend


class UrllibConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.backends.urllib.UrllibBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return UrllibBackend(hooks=hooks or [])


class HttpxConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.backends.httpx.HttpxBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        backend = HttpxBackend(hooks=hooks or [])
        self.addCleanup(backend.close)
        return backend


class Httpx2ConformanceTestCase(SyncModel):
    """the suite on :py:class:`~action0.client.backends.httpx2.Httpx2Backend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        backend = Httpx2Backend(hooks=hooks or [])
        self.addCleanup(backend.close)
        return backend


class AsyncStubBackendConformanceTestCase(AsyncModel):
    """the suite on :py:class:`~action0.client.testing.AsyncStubBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return AsyncStubBackend(hooks=hooks or [])


class AsyncHttpxConformanceTestCase(AsyncModel):
    """the suite on :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return AsyncHttpxBackend(hooks=hooks or [])


class AsyncHttpx2ConformanceTestCase(AsyncModel):
    """the suite on :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return AsyncHttpx2Backend(hooks=hooks or [])


class AiohttpConformanceTestCase(AsyncModel):
    """the suite on :py:class:`~action0.client.backends.aiohttp.AiohttpBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return AiohttpBackend(hooks=hooks or [])


class DeferredStubBackendConformanceTestCase(DeferredModel):
    """the suite on :py:class:`~action0.client.testing.DeferredStubBackend`"""

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        return DeferredStubBackend(hooks=hooks or [])


class TwistedConformanceTestCase(DeferredModel):
    """the suite on :py:class:`~action0.client.backends.twisted.TwistedBackend`"""

    __test__ = True
    streams = False

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        # a clock instead of the global reactor: nothing here needs one
        return TwistedBackend(reactor=Clock(), hooks=hooks or [])


class ThreadPoolConformanceTestCase(FuturesModel):
    """
    the suite on :py:class:`~action0.client.backends.futures.ThreadPoolBackend`
    — the mocked sends run on pool threads, which the process-wide
    router reaches
    """

    __test__ = True

    def backend(self, hooks: "list[Hook] | None" = None) -> Any:
        backend = ThreadPoolBackend(UrllibBackend(hooks=hooks or []), max_workers=2)
        self.addCleanup(backend.close)
        return backend

import unittest
from typing import Any

from twisted.internet.defer import Deferred
from twisted.internet.defer import fail
from twisted.internet.defer import succeed
from twisted.internet.error import DNSLookupError
from twisted.internet.task import Clock
from twisted.internet.task import TaskStopped
from twisted.python.failure import Failure
from twisted.web.client import RedirectAgent
from twisted.web.client import ResponseDone
from twisted.web.http_headers import Headers as TwistedHeaders
from twisted.web.iweb import UNKNOWN_LENGTH

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.twisted import TwistedBackend
from action0.client.backends.twisted import _RequestBodyProducer
from action0.client.backends.twisted import _twisted_headers
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req.body import BytesBody
from action0.req.body import IterableBody


class FakeTwistedResponse:
    """A minimal stand-in for twisted's IResponse."""

    def __init__(
        self,
        code: int = 200,
        phrase: bytes = b"OK",
        body: bytes = b"",
        headers: "list[tuple[str, str]] | None" = None,
        version: "tuple[bytes, int, int]" = (b"HTTP", 1, 1),
    ) -> None:
        self.code = code
        self.phrase = phrase
        self.version = version
        self.length = len(body)
        self.headers = TwistedHeaders()
        for name, value in headers or []:
            self.headers.addRawHeader(name, value)
        self._body = body

    def deliverBody(self, protocol: Any) -> None:
        """
        Feed the canned body to the reading protocol, like twisted would.

        :param protocol: the body-collecting protocol (from readBody)
        """
        protocol.dataReceived(self._body)
        protocol.connectionLost(Failure(ResponseDone("done")))  # type: ignore[no-untyped-call]


class FakeAgent:
    """A stand-in for twisted's Agent recording requests."""

    def __init__(self, result: Any) -> None:
        """
        :param result: the IResponse to succeed with, the Failure to fail
                       with, or a pending Deferred to return as-is
        """
        self.result = result
        self.calls: "list[tuple[bytes, bytes, Any, Any]]" = []

    def request(
        self, method: bytes, uri: bytes, headers: Any = None, bodyProducer: Any = None
    ) -> "Deferred[Any]":
        self.calls.append((method, uri, headers, bodyProducer))
        if isinstance(self.result, Failure):
            return fail(self.result)
        if isinstance(self.result, Deferred):
            return self.result
        return succeed(self.result)


class SendTestCase(unittest.TestCase):
    """
    tests for :py:meth:`action0.client.backends.twisted.TwistedBackend._send`
    with a fake agent (no reactor)
    """

    def backend(self, result: Any, timeout: "float | None" = None) -> "TwistedBackend":
        """
        A backend over a fake agent.

        :param result: what the fake agent answers
        :param timeout: the backend timeout (None: no timeout chain)
        :return: the ready-made backend
        """
        self.agent = FakeAgent(result)
        self.clock = Clock()
        return TwistedBackend(agent=self.agent, reactor=self.clock, timeout=timeout)

    def test_response_conversion(self) -> None:
        """
        Test status, reason, version, headers (multi-value) and body.
        """
        fake = FakeTwistedResponse(
            code=418,
            phrase=b"I'm a teapot",
            body=b"short and stout",
            headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-One", "1")],
        )
        request = Request("https://api.example.com/tea")
        response = deferred_result(self.backend(fake).send(request))

        self.assertEqual(response.status, 418)
        self.assertEqual(response.reason, "I'm a teapot")
        self.assertEqual(response.http_version, "HTTP/1.1")
        self.assertEqual(response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])
        self.assertEqual(response.headers["X-One"], "1")
        self.assertEqual(response.body_bytes(), b"short and stout")
        self.assertIs(response.request, request)

    def test_request_conversion(self) -> None:
        """
        Test method/URI bytes, header lines and the missing body producer.
        """
        backend = self.backend(FakeTwistedResponse())
        request = Request("https://api.example.com/items", "POST", headers=[("X-A", ["1", "2"])])
        deferred_result(backend.send(request))

        method, uri, headers, producer = self.agent.calls[0]
        self.assertEqual(method, b"POST")
        self.assertEqual(uri, b"https://api.example.com/items")
        self.assertEqual(headers.getRawHeaders("X-A"), ["1", "2"])
        self.assertIsNone(producer)

    def test_request_body_becomes_a_producer(self) -> None:
        """
        Test that a request body is wrapped as an IBodyProducer with the
        right length.
        """
        backend = self.backend(FakeTwistedResponse())
        deferred_result(backend.send(Request("https://x.example/", "PUT", body=b"1234")))
        producer = self.agent.calls[0][3]
        self.assertIsInstance(producer, _RequestBodyProducer)
        self.assertEqual(producer.length, 4)

    def test_non_ascii_urls_are_encoded(self) -> None:
        """
        Test IDNA and percent-encoding on the wire.
        """
        backend = self.backend(FakeTwistedResponse())
        deferred_result(backend.send(Request("https://bücher.example/a b")))
        uri = self.agent.calls[0][1]
        self.assertEqual(uri, b"https://xn--bcher-kva.example/a%20b")

    def test_failures_are_translated(self) -> None:
        """
        Test that twisted transport failures surface as TransportError
        with the original chained.
        """
        original = DNSLookupError("no such host")
        backend = self.backend(Failure(original))  # type: ignore[no-untyped-call]
        with self.assertRaises(TransportError) as caught:
            deferred_result(backend.send(Request("https://nope.example/")))
        self.assertIs(caught.exception.__cause__, original)

    def test_timeout_via_the_clock(self) -> None:
        """
        Test that a hanging request times out through the reactor clock
        and surfaces as the library's TimeoutError.
        """
        hanging: "Deferred[Any]" = Deferred()
        backend = self.backend(hanging, timeout=5)
        deferred = backend.send(Request("https://slow.example/"))

        self.clock.advance(6)

        with self.assertRaises(TimeoutError):
            deferred_result(deferred)

    def test_agent_construction_honors_follow_redirects(self) -> None:
        """
        Test that created agents are wrapped in a RedirectAgent (or not).
        """
        from twisted.internet import reactor

        following = TwistedBackend(reactor=reactor)
        self.assertIsInstance(following._agent, RedirectAgent)

        plain = TwistedBackend(reactor=reactor, follow_redirects=False)
        self.assertNotIsInstance(plain._agent, RedirectAgent)


class HeaderConversionTestCase(unittest.TestCase):
    """
    tests for :py:func:`action0.client.backends.twisted._twisted_headers`
    """

    def test_lines_survive_conversion(self) -> None:
        """
        Test that repeated lines and casing reach twisted's Headers.
        """
        request = Request(
            "https://example.com/", headers=[("X-A", ["1", "2"]), ("Content-Type", "text/x")]
        )
        headers = _twisted_headers(request)
        self.assertEqual(headers.getRawHeaders("X-A"), ["1", "2"])
        self.assertEqual(headers.getRawHeaders("content-type"), ["text/x"])


class ConsumerStub:
    """Collects everything written to it."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


class RequestBodyProducerTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.twisted._RequestBodyProducer`
    """

    def test_known_length(self) -> None:
        """
        Test that a sized body advertises its length.
        """
        self.assertEqual(_RequestBodyProducer(BytesBody(b"12345")).length, 5)

    def test_unknown_length(self) -> None:
        """
        Test that an unsized body advertises UNKNOWN_LENGTH (chunked).
        """
        producer = _RequestBodyProducer(IterableBody([b"a", b"b"]))
        self.assertIs(producer.length, UNKNOWN_LENGTH)

    def test_write_loop_writes_all_chunks(self) -> None:
        """
        Test the cooperative write loop chunk by chunk.
        """
        producer = _RequestBodyProducer(IterableBody([b"a", b"b", b"c"]))
        consumer = ConsumerStub()
        steps = list(producer._write(consumer))
        self.assertEqual(consumer.written, [b"a", b"b", b"c"])
        self.assertEqual(len(steps), 3)

    def test_stopped_filters_task_stopped(self) -> None:
        """
        Test that stopping is not treated as an error, other failures are.
        """
        self.assertIsNone(
            _RequestBodyProducer._stopped(Failure(TaskStopped()))  # type: ignore[no-untyped-call]
        )
        boom = Failure(RuntimeError("boom"))  # type: ignore[no-untyped-call]
        self.assertIs(_RequestBodyProducer._stopped(boom), boom)

    def test_lifecycle_without_task_is_a_noop(self) -> None:
        """
        Test pause/resume/stop before production started.
        """
        producer = _RequestBodyProducer(BytesBody(b"x"))
        producer.pauseProducing()
        producer.resumeProducing()
        producer.stopProducing()

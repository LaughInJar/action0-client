import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from twisted.internet.defer import Deferred
from twisted.internet.defer import fail
from twisted.internet.defer import succeed
from twisted.internet.error import ConnectionLost
from twisted.internet.error import DNSLookupError
from twisted.internet.task import Clock
from twisted.internet.task import TaskStopped
from twisted.python.failure import Failure
from twisted.web.client import RedirectAgent
from twisted.web.client import ResponseDone
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers as TwistedHeaders
from twisted.web.iweb import UNKNOWN_LENGTH

from action0.client import BodyMode
from action0.client import RequestOptions
from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends import _body
from action0.client.backends import twisted as twisted_backend
from action0.client.backends.twisted import TwistedBackend
from action0.client.backends.twisted import _RequestBodyProducer
from action0.client.backends.twisted import _twisted_headers
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response
from action0.req import SpooledBody
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


class ManualTwistedResponse(FakeTwistedResponse):
    """
    A response whose body the test delivers by hand, chunk by chunk,
    through the protocol the backend registered.
    """

    protocol: Any = None

    def deliverBody(self, protocol: Any) -> None:
        self.protocol = protocol
        self.transport = RecordingTransport()
        protocol.makeConnection(self.transport)

    def data(self, chunk: bytes) -> None:
        """Deliver a chunk of the body."""
        self.protocol.dataReceived(chunk)

    def end(self, reason: "Exception | None" = None) -> None:
        """End the body: completely by default, or with the given reason."""
        self.protocol.connectionLost(Failure(reason or ResponseDone("done")))  # type: ignore[no-untyped-call]


class RecordingTransport:
    """The transport of a body protocol, recording what is asked of it."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def pauseProducing(self) -> None:
        self.events.append("pause")

    def resumeProducing(self) -> None:
        self.events.append("resume")

    def abortConnection(self) -> None:
        self.events.append("abort")


class FakeThreadPool:
    """
    A thread pool that runs nothing by itself: the test decides when a
    submitted call "finishes" — so everything that can happen during a
    write is testable deterministically.
    """

    def __init__(self) -> None:
        self.calls: "list[tuple[Callable[..., None], Callable[..., Any], tuple[Any, ...]]]" = []

    def callInThreadWithCallback(
        self, on_result: "Callable[..., None]", function: "Callable[..., Any]", *args: Any
    ) -> None:
        self.calls.append((on_result, function, args))

    def run_next(self) -> None:
        """Run the oldest submitted call and report its outcome."""
        on_result, function, args = self.calls.pop(0)
        try:
            result = function(*args)
        except Exception:
            on_result(False, Failure())  # type: ignore[no-untyped-call]
        else:
            on_result(True, result)


class ThreadedClock(Clock):
    """A test clock that also offers the thread-pool side of a reactor."""

    def __init__(self) -> None:
        super().__init__()
        self.pool = FakeThreadPool()

    def getThreadPool(self) -> FakeThreadPool:
        return self.pool

    def callFromThread(self, function: "Callable[..., Any]", *args: Any) -> None:
        function(*args)


class SpooledBodyTestCase(unittest.TestCase):
    """
    tests for the body modes of
    :py:class:`action0.client.backends.twisted.TwistedBackend`: spooling
    through ``_SpoolingProtocol`` and the refusal to stream
    """

    URL = "https://api.example.com/feed"

    def request(self, mode: BodyMode = BodyMode.SPOOL, **options: Any) -> Request:
        """
        A request asking for the given body mode.

        :param mode: the body mode
        :param options: further :py:class:`RequestOptions` fields
        :return: the request
        """
        return RequestOptions(body=mode, **options).apply(Request(self.URL))

    def start(
        self, max_memory: int = 4, timeout: "float | None" = None
    ) -> "tuple[ManualTwistedResponse, ThreadedClock, list[Any]]":
        """
        Send a spooling request whose body the test delivers by hand, on
        a clock with a (manually driven) thread pool.

        :param max_memory: the spool's memory threshold
        :param timeout: the backend timeout
        :return: the response to feed, the clock, and the list the
                 outcome (a Response or a Failure) is appended to
        """
        fake = ManualTwistedResponse(headers=[("Content-Type", "application/octet-stream")])
        clock = ThreadedClock()
        backend = TwistedBackend(agent=FakeAgent(fake), reactor=clock, timeout=timeout)
        outcome: list[Any] = []
        backend.send(self.request(spool_max_memory=max_memory)).addBoth(outcome.append)
        return fake, clock, outcome

    def recorded_spools(self) -> list[SpooledBody]:
        """
        Record the spools the backend creates for the rest of the test.

        :return: the list the spools are appended to
        """
        spools: list[SpooledBody] = []
        real = _body.new_spool

        def recording(request: Request) -> SpooledBody:
            spools.append(real(request))
            return spools[-1]

        patcher = mock.patch.object(twisted_backend, "new_spool", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spools

    def test_spooled_body(self) -> None:
        """
        Test that a request asking for a spool gets a SpooledBody with
        the whole body, and that the rest of the response is converted
        as usual.
        """
        fake = FakeTwistedResponse(code=206, phrase=b"Partial Content", body=b"spooled data")
        backend = TwistedBackend(agent=FakeAgent(fake), reactor=Clock())
        response = deferred_result(backend.send(self.request()))

        self.assertEqual(response.status, 206)
        body = response.body
        assert isinstance(body, SpooledBody)
        self.assertFalse(body.rolled)
        self.assertEqual(response.body_bytes(), b"spooled data")
        response.close()
        self.assertTrue(body.closed)

    def test_reactor_without_threads_writes_inline(self) -> None:
        """
        Test that on a reactor without a thread pool (a plain test clock)
        the body still reaches the temporary file.
        """
        fake = FakeTwistedResponse(body=b"spooled data")
        backend = TwistedBackend(agent=FakeAgent(fake), reactor=Clock())
        with deferred_result(backend.send(self.request(spool_max_memory=4))) as response:
            body = response.body
            assert isinstance(body, SpooledBody)
            self.assertTrue(body.rolled)
            self.assertEqual(response.body_bytes(), b"spooled data")

    def test_preload_stays_the_default(self) -> None:
        """
        Test that without options (or with PRELOAD) the body is bytes.
        """
        for request in (Request(self.URL), self.request(BodyMode.PRELOAD)):
            fake = FakeTwistedResponse(body=b"plain")
            backend = TwistedBackend(agent=FakeAgent(fake), reactor=Clock())
            self.assertEqual(deferred_result(backend.send(request)).body, b"plain")

    def test_stream_is_refused_before_sending(self) -> None:
        """
        Test that asking for a streamed body fails the Deferred with a
        ValueError naming the alternatives, without a request going out.
        """
        agent = FakeAgent(FakeTwistedResponse())
        backend = TwistedBackend(agent=agent, reactor=Clock())
        with self.assertRaisesRegex(ValueError, "BodyMode.SPOOL"):
            deferred_result(backend.send(self.request(BodyMode.STREAM)))
        self.assertEqual(agent.calls, [])

    def test_disk_writes_run_in_the_thread_pool(self) -> None:
        """
        Test the no-blocking strategy step by step: in-memory chunks are
        written inline; from the rollover on, writes go to the thread
        pool with the transport paused, data arriving meanwhile is
        batched into the next write, and the transport resumes when the
        writes have caught up.
        """
        fake, clock, outcome = self.start(max_memory=4)

        fake.data(b"abcd")  # fits in memory
        self.assertEqual(clock.pool.calls, [])
        self.assertEqual(fake.transport.events, [])

        fake.data(b"ef")  # exceeds the threshold: a threaded write
        self.assertEqual(len(clock.pool.calls), 1)
        self.assertEqual(fake.transport.events, ["pause"])

        fake.data(b"gh")  # arrives during the write: waits its turn
        fake.data(b"ij")
        self.assertEqual(len(clock.pool.calls), 1)

        clock.pool.run_next()  # the first write is done: the batch follows
        self.assertEqual(len(clock.pool.calls), 1)
        self.assertEqual(clock.pool.calls[0][2], (b"ghij",))
        self.assertEqual(fake.transport.events, ["pause"])

        clock.pool.run_next()  # caught up
        self.assertEqual(fake.transport.events, ["pause", "resume"])
        self.assertEqual(outcome, [])

        fake.end()
        response = outcome[0]
        assert isinstance(response, Response)
        body = response.body
        assert isinstance(body, SpooledBody)
        self.assertTrue(body.rolled)
        self.assertEqual(response.body_bytes(), b"abcdefghij")
        response.close()

    def test_response_ending_during_a_write_waits_for_it(self) -> None:
        """
        Test that the Deferred fires only when the last write has landed,
        although the response ended before.
        """
        fake, clock, outcome = self.start(max_memory=0)
        fake.data(b"abc")
        fake.end()
        self.assertEqual(outcome, [])

        clock.pool.run_next()
        response = outcome[0]
        assert isinstance(response, Response)
        with response:
            self.assertEqual(response.body_bytes(), b"abc")

    def test_incomplete_response_fails_and_discards_the_spool(self) -> None:
        """
        Test that a connection lost midway fails the Deferred with a
        TransportError and closes the half-filled spool.
        """
        spools = self.recorded_spools()
        fake, _clock, outcome = self.start(max_memory=1024)
        fake.data(b"abc")
        fake.end(ConnectionLost("gone"))

        failure = outcome[0]
        assert isinstance(failure, Failure)
        self.assertIsInstance(failure.value, TransportError)
        self.assertTrue(spools[0].closed)

    def test_body_delimited_by_the_connection_end_is_complete(self) -> None:
        """
        Test that PotentialDataLoss — a body without Content-Length,
        ended by closing the connection — counts as complete, like for
        preloaded bodies.
        """
        fake, _clock, outcome = self.start(max_memory=1024)
        fake.data(b"abc")
        fake.end(PotentialDataLoss())
        response = outcome[0]
        assert isinstance(response, Response)
        with response:
            self.assertEqual(response.body_bytes(), b"abc")

    def test_timeout_while_spooling_aborts_the_connection(self) -> None:
        """
        Test that the backend timeout also covers the body: it aborts the
        connection, discards the spool and fails with a TimeoutError.
        Data still trickling in is ignored.
        """
        spools = self.recorded_spools()
        fake, clock, outcome = self.start(max_memory=1024, timeout=5.0)
        fake.data(b"abc")
        clock.advance(5.0)

        failure = outcome[0]
        assert isinstance(failure, Failure)
        self.assertIsInstance(failure.value, TimeoutError)
        self.assertEqual(fake.transport.events, ["abort"])
        self.assertTrue(spools[0].closed)

        fake.data(b"late")
        fake.end()
        self.assertEqual(len(outcome), 1)

    def test_timeout_during_a_write_closes_the_spool_afterwards(self) -> None:
        """
        Test that a spool being written to by a worker thread is not
        closed under it: the cancellation waits for the write to return.
        """
        spools = self.recorded_spools()
        fake, clock, outcome = self.start(max_memory=0, timeout=5.0)
        fake.data(b"abc")
        clock.advance(5.0)
        self.assertIsInstance(outcome[0].value, TimeoutError)
        self.assertFalse(spools[0].closed)

        clock.pool.run_next()
        self.assertTrue(spools[0].closed)

    def test_failed_write_fails_the_response(self) -> None:
        """
        Test that an error of the file write (here: the directory for the
        temporary file does not exist) aborts the connection and fails
        the Deferred with that error.
        """
        fake = ManualTwistedResponse()
        clock = ThreadedClock()
        backend = TwistedBackend(agent=FakeAgent(fake), reactor=clock, timeout=None)
        request = self.request(spool_max_memory=0, spool_dir="/nonexistent/spool/dir")
        outcome: list[Any] = []
        backend.send(request).addBoth(outcome.append)

        fake.data(b"abc")
        clock.pool.run_next()

        failure = outcome[0]
        assert isinstance(failure, Failure)
        self.assertIsInstance(failure.value, FileNotFoundError)
        self.assertEqual(fake.transport.events, ["pause", "abort"])

"""
The `Twisted <https://twisted.org/>`_ backend — a
:py:data:`~action0.client.backend.DeferredBackend` driving a
:py:class:`twisted.web.client.Agent`.

Requires the ``twisted`` extra: ``pip install "action0-client[twisted]"``
(which includes Twisted's ``tls`` extra, so ``https://`` URLs work).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import Iterator

from twisted.internet import defer
from twisted.internet import error as internet_error
from twisted.internet.task import TaskStopped
from twisted.internet.task import cooperate
from twisted.web.client import Agent
from twisted.web.client import PartialDownloadError
from twisted.web.client import RedirectAgent
from twisted.web.client import ResponseFailed
from twisted.web.client import readBody
from twisted.web.http_headers import Headers as TwistedHeaders
from twisted.web.iweb import UNKNOWN_LENGTH
from twisted.web.iweb import IBodyProducer
from zope.interface import implementer  # type: ignore[import-untyped]

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyProducer

from ..backend import BaseDeferredBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

if TYPE_CHECKING:
    from twisted.internet.defer import Deferred
    from twisted.internet.task import CooperativeTask
    from twisted.python.failure import Failure

DEFAULT_TIMEOUT = 30.0
"""The default total number of seconds from sending until the response
body finished arriving."""


@implementer(IBodyProducer)
class _RequestBodyProducer:
    """
    Adapts an :py:class:`action0.req.body.BodyProducer` to Twisted's
    ``IBodyProducer``: the chunks are written to the consumer through a
    cooperative task (pausable, resumable, stoppable), modeled after
    Twisted's own ``FileBodyProducer``.
    """

    def __init__(self, producer: BodyProducer) -> None:
        """
        :param producer: the body producer to stream
        """
        length = producer.content_length()
        self.length = UNKNOWN_LENGTH if length is None else length
        self._producer = producer
        self._task: CooperativeTask[Iterator[None]] | None = None

    def startProducing(self, consumer: Any) -> Deferred[None]:
        """
        Start writing the chunks to the consumer.

        :param consumer: the ``IConsumer`` (the request transport) to write
                         to — typed loosely because zope interfaces and
                         static checkers don't mix
        :return: a Deferred firing (with ``None``) once all chunks are
                 written
        """
        self._task = cooperate(self._write(consumer))
        done = self._task.whenDone()
        return done.addCallbacks(lambda _: None, self._stopped)

    def _write(self, consumer: Any) -> Iterator[None]:
        """
        The cooperative write loop: one chunk per iteration.

        :param consumer: the ``IConsumer`` to write to
        :return: an iterator yielding after every written chunk
        """
        for chunk in self._producer.chunks():
            consumer.write(chunk)
            yield None

    @staticmethod
    def _stopped(reason: Failure) -> Failure | None:
        """
        Swallow the failure a stopped task reports — stopping is normal
        request cancellation, not an error.

        :param reason: the task failure
        :return: ``None`` for a stop, the failure otherwise
        """
        if reason.check(TaskStopped):  # type: ignore[no-untyped-call]
            return None
        return reason

    def pauseProducing(self) -> None:
        """Pause the write loop (transport buffer is full)."""
        if self._task is not None:
            self._task.pause()

    def resumeProducing(self) -> None:
        """Resume the paused write loop."""
        if self._task is not None:
            self._task.resume()

    def stopProducing(self) -> None:
        """Abort the write loop (the request was cancelled)."""
        if self._task is not None:
            self._task.stop()


def _twisted_headers(request: Request) -> TwistedHeaders:
    """
    The request headers as Twisted's ``Headers``, line by line so multiple
    lines per field stay intact. (``Agent`` adds the ``Host`` header from
    the URL itself if none is set.)

    :param request: the request whose headers to convert
    :return: the converted headers
    """
    headers = TwistedHeaders()
    for name, value in request.headers.as_lines():
        headers.addRawHeader(name, value)
    return headers


class TwistedBackend(BaseDeferredBackend):
    """
    A Twisted backend: :py:meth:`~action0.client.backend.BaseDeferredBackend.send`
    returns a ``Deferred[Response]`` driven by a
    :py:class:`twisted.web.client.Agent`.

    Example::

        from twisted.internet import reactor
        from action0.client import Client
        from action0.client.backends.twisted import TwistedBackend
        from action0.req import Request

        client = Client(TwistedBackend())
        deferred = client.send(Request("https://example.com/"))
        deferred.addCallback(lambda response: print(response.status))
        deferred.addBoth(lambda _: reactor.stop())
        reactor.run()

    Streaming request bodies work: a
    :py:class:`~action0.req.body.BodyProducer` body is streamed through a
    cooperative task. The response body is always read in full before the
    Deferred fires.
    """

    def __init__(
        self,
        agent: Any = None,
        *,
        reactor: Any = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param agent: the ``IAgent`` to send through — configure connection
                      pooling, proxies, custom TLS policies etc. there;
                      ``None`` creates a plain ``Agent`` (wrapped in a
                      ``RedirectAgent`` if ``follow_redirects`` — the
                      argument only applies to the created agent). Typed
                      loosely because zope interfaces and static checkers
                      don't mix.
        :param reactor: the reactor for the created agent and the timeout
                        clock; ``None`` uses the global reactor (imported
                        lazily here, not at module import time)
        :param timeout: the total seconds from sending until the response
                        body finished arriving; ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        if reactor is None:
            # deliberately imported here: importing the global reactor at
            # module import time would install it as a side effect
            from twisted.internet import reactor as global_reactor

            reactor = global_reactor
        self._clock = reactor
        if agent is None:
            agent = Agent(reactor)  # type: ignore[no-untyped-call]
            if follow_redirects:
                # ty ignore: it cannot see that @implementer(IAgent) classes
                # provide the zope interface
                agent = RedirectAgent(agent)  # ty: ignore[invalid-argument-type]
        self._agent = agent
        self._timeout = timeout

    def _send(self, request: Request) -> Deferred[Response]:
        """
        Start the request via the agent; the returned Deferred fires once
        the response *body* has arrived completely.

        :param request: the request to send
        :return: a Deferred firing with the response
        """
        body = request.body_producer()
        producer = _RequestBodyProducer(body) if body is not None else None
        deferred = self._agent.request(
            request.method.encode("ascii"),
            # as_str() percent-encodes and IDNA-encodes, so ascii is safe
            request.url.as_str().encode("ascii"),
            _twisted_headers(request),
            # ty ignore: it cannot see that @implementer(IBodyProducer)
            # classes provide the zope interface
            producer,  # ty: ignore[invalid-argument-type]
        )
        result: Deferred[Response] = deferred.addCallback(self._read_body, request)
        if self._timeout is not None:
            result.addTimeout(self._timeout, self._clock)
        return result

    def _read_body(self, answer: Any, request: Request) -> Deferred[Response]:
        """
        Read the response body in full and convert the response.

        :param answer: the Twisted ``IResponse`` (headers arrived, body
                       pending) — typed loosely because zope interfaces
                       and static checkers don't mix
        :param request: the request that produced it
        :return: a Deferred firing with the converted response
        """
        return readBody(answer).addCallback(self._convert, answer, request)

    def _convert(self, body: bytes, answer: Any, request: Request) -> Response:
        """
        Convert a fully-read Twisted response into an
        :py:class:`~action0.req.response.Response`.

        :param body: the response body
        :param answer: the Twisted ``IResponse``
        :param request: the request that produced it
        :return: the converted response
        """
        name, major, minor = answer.version
        header_lines = [
            (header_name.decode("latin-1"), value.decode("latin-1"))
            for header_name, values in answer.headers.getAllRawHeaders()
            for value in values
        ]
        return Response(
            answer.code,
            headers=header_lines,
            body=body,
            reason=answer.phrase.decode("latin-1") or None,
            http_version=f"{name.decode('latin-1')}/{major}.{minor}",
            request=request,
        )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize Twisted's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception the send failed with
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        if isinstance(error, (defer.TimeoutError, internet_error.TimeoutError)):
            return TimeoutError(str(error) or type(error).__name__, request=request)
        if isinstance(
            error,
            (
                internet_error.ConnectError,
                internet_error.ConnectionClosed,
                internet_error.ConnectingCancelledError,
                internet_error.DNSLookupError,
                ResponseFailed,
                PartialDownloadError,
            ),
        ):
            return TransportError(str(error) or type(error).__name__, request=request)
        return error

    def __repr__(self) -> str:
        """
        :return: the backend class name (no configuration secrets)
        """
        return f"{self.__class__.__name__}()"

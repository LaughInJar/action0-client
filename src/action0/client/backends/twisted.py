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
from twisted.internet.protocol import Protocol
from twisted.internet.protocol import connectionDone
from twisted.internet.task import TaskStopped
from twisted.internet.task import cooperate
from twisted.internet.threads import deferToThreadPool
from twisted.web.client import Agent
from twisted.web.client import PartialDownloadError
from twisted.web.client import RedirectAgent
from twisted.web.client import ResponseDone
from twisted.web.client import ResponseFailed
from twisted.web.client import readBody
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers as TwistedHeaders
from twisted.web.iweb import UNKNOWN_LENGTH
from twisted.web.iweb import IBodyProducer
from zope.interface import implementer  # type: ignore[import-untyped]

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyProducer
from action0.req.body import BodyTypes
from action0.req.body import SpooledBody

from ..backend import BaseDeferredBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook
from ..options import BodyMode
from ._body import body_mode
from ._body import new_spool
from ._body import spool_memory

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


class _SpoolingProtocol(Protocol):
    """
    The receiving end of ``response.deliverBody()`` for a spooled body:
    writes the body into a :py:class:`~action0.req.body.SpooledBody`
    while it arrives and fires :py:attr:`finished` with the spool once
    the response is complete.

    The reactor is never blocked by file I/O: while the body fits in
    memory, chunks are appended inline; once it is in the temporary file
    every write runs in the reactor's thread pool. During such a write
    the transport is paused, and what still arrives is collected for the
    next write — so writes never overlap and memory use stays bounded by
    the transport's own buffering.
    """

    def __init__(self, spool: SpooledBody, max_memory: int, reactor: Any) -> None:
        """
        :param spool: the (empty) spool to fill
        :param max_memory: the spool's memory threshold — up to it the
                           writes are known not to touch the disk
        :param reactor: the reactor whose thread pool does the file
                        writes; one without a thread pool (a test clock)
                        makes them happen inline
        """
        self.finished: Deferred[SpooledBody] = defer.Deferred(self._cancel)
        """Fires with the filled spool, or fails with the reason the
        response ended early. Cancelling it aborts the connection."""
        self._spool = spool
        self._max_memory = max_memory
        self._reactor = reactor
        self._pending: list[bytes] = []
        self._writing = False
        self._paused = False
        self._lost: Failure | None = None
        self._cancelled = False

    def dataReceived(self, data: bytes) -> None:
        """
        Take a chunk of the body.

        :param data: the bytes that arrived
        """
        if self._cancelled:
            return
        in_memory = not self._spool.rolled and self._size() + len(data) <= self._max_memory
        if in_memory and not self._writing:
            self._spool.write(data)
            return
        self._pending.append(data)
        if not self._writing:
            self._write_pending()

    def _size(self) -> int:
        """
        :return: the number of bytes in the spool so far
        """
        return self._spool.content_length() or 0

    def _write_pending(self) -> None:
        """
        Hand everything collected so far to the thread pool as one write
        and pause the transport until it is done.
        """
        data = b"".join(self._pending)
        self._pending.clear()
        get_pool = getattr(self._reactor, "getThreadPool", None)
        if get_pool is None:
            self._spool.write(data)
            return
        self._writing = True
        self._pause(True)
        written = deferToThreadPool(self._reactor, get_pool(), self._spool.write, data)
        written.addCallbacks(self._written, self._write_failed)

    def _written(self, _: int) -> None:
        """
        A write finished: continue with what arrived meanwhile, finish if
        the response ended meanwhile, otherwise let the transport go on.
        """
        self._writing = False
        if self._cancelled:
            self._spool.close()
        elif self._pending:
            self._write_pending()
        elif self._lost is not None:
            self._finish(self._lost)
        else:
            self._pause(False)

    def _pause(self, paused: bool) -> None:
        """
        Stop or restart the delivery of body data.

        :param paused: whether the transport should hold back
        """
        if paused == self._paused:
            return
        self._paused = paused
        # Any: the transport of a body protocol is a producer proxy that
        # ITransport does not describe
        transport: Any = self.transport
        if transport is not None:
            if paused:
                transport.pauseProducing()
            else:
                transport.resumeProducing()

    def _write_failed(self, failure: Failure) -> None:
        """
        A write failed (disk full, ...): give up on the response.

        :param failure: what the write raised
        """
        self._writing = False
        self._spool.close()
        if self._cancelled:
            return
        self._cancelled = True
        self._abort()
        self.finished.errback(failure)

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        """
        The body ended — completely or not, see :py:meth:`_finish`.

        :param reason: why no more data is coming
        """
        if self._cancelled:
            return
        if self._writing:
            # the last write is still running: finish when it is done
            self._lost = reason
        else:
            self._finish(reason)

    def _finish(self, reason: Failure) -> None:
        """
        Fire :py:attr:`finished`: with the spool if the response was
        delivered in full (``ResponseDone``, or ``PotentialDataLoss`` for
        a body delimited by the end of the connection — the same rule
        ``readBody`` applies), otherwise with the reason.

        :param reason: why no more data is coming
        """
        if reason.check(ResponseDone, PotentialDataLoss):  # type: ignore[no-untyped-call]
            self.finished.callback(self._spool)
        else:
            self._spool.close()
            self.finished.errback(reason)

    def _abort(self) -> None:
        """
        Drop the connection the body is arriving on, if it can be.
        """
        abort = getattr(self.transport, "abortConnection", None)
        if abort is not None:
            abort()

    def _cancel(self, _: Deferred[SpooledBody]) -> None:
        """
        The canceller of :py:attr:`finished` (a timeout, or the caller
        giving up): abort the connection and discard the spool.
        """
        self._cancelled = True
        self._abort()
        if not self._writing:
            # otherwise the running write's callback closes it
            self._spool.close()


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
    cooperative task.

    The response body is always read in full before the Deferred fires:
    into memory by default, or — for a request asking for
    :py:attr:`BodyMode.SPOOL <action0.client.options.BodyMode.SPOOL>` —
    into a :py:class:`~action0.req.body.SpooledBody`, with the file
    writes done in the reactor's thread pool so the reactor is never
    blocked. A streamed body (``BodyMode.STREAM``) is not supported; such
    a request fails with a :py:class:`ValueError` before anything is
    sent.
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
        mode = body_mode(request, False)
        if mode is BodyMode.STREAM:
            raise ValueError(
                "TwistedBackend cannot stream response bodies: ask for BodyMode.SPOOL"
                " (the body is written to a temporary file while it arrives) or"
                " BodyMode.PRELOAD"
            )
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
        result: Deferred[Response] = deferred.addCallback(self._read_body, request, mode)
        if self._timeout is not None:
            result.addTimeout(self._timeout, self._clock)
        return result

    def _read_body(self, answer: Any, request: Request, mode: BodyMode) -> Deferred[Response]:
        """
        Read the response body in full — into memory, or into a spool —
        and convert the response.

        :param answer: the Twisted ``IResponse`` (headers arrived, body
                       pending) — typed loosely because zope interfaces
                       and static checkers don't mix
        :param request: the request that produced it
        :param mode: ``BodyMode.PRELOAD`` or ``BodyMode.SPOOL``
        :return: a Deferred firing with the converted response
        """
        if mode is BodyMode.SPOOL:
            protocol = _SpoolingProtocol(new_spool(request), spool_memory(request), self._clock)
            answer.deliverBody(protocol)
            return protocol.finished.addCallback(self._convert, answer, request)
        return readBody(answer).addCallback(self._convert, answer, request)

    def _convert(self, body: BodyTypes, answer: Any, request: Request) -> Response:
        """
        Convert a fully-read Twisted response into an
        :py:class:`~action0.req.response.Response`.

        :param body: the response body, as bytes or as the filled spool
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

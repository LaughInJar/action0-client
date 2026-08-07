"""
Test doubles for writing tests against API clients — yours or ones built
with this library — without any network I/O.

One stub backend per execution model, all sharing the same behavior:

- they are constructed with the :py:class:`~action0.req.response.Response`
  (or responses) to answer with — or callables producing them,
- they record every request in :py:attr:`~StubBackend.requests`,
- they run the regular :py:class:`~action0.client.hooks.Hook` machinery,
  because they subclass the real backend base classes.

Example::

    >>> from action0.req import Request, Response
    >>>
    >>> backend = StubBackend(Response(200, body="pong"))
    >>> backend.send(Request("https://api.example.com/ping")).body_str()
    'pong'
    >>> backend.requests[0]
    Request(GET https://api.example.com/ping)
"""

from collections.abc import Callable
from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar
from typing import cast

from action0.req import Request
from action0.req import Response

from .backend import BaseAsyncBackend
from .backend import BaseDeferredBackend
from .backend import BaseSyncBackend
from .hooks import Hook

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

T = TypeVar("T")

Responder = Callable[[Request], Response]
"""A callable producing the response for a request — the dynamic
alternative to canned :py:class:`~action0.req.response.Response` instances
for the stub backends. May raise to exercise error paths."""


class _Script:
    """
    The canned-response logic shared by the three stub backends: a
    sequence of responses (or responders) handed out one per request, with
    the last one repeating forever — so a single-response stub answers any
    number of requests predictably.
    """

    def __init__(self, responses: "tuple[Response | Responder, ...]") -> None:
        """
        :param responses: the responses (or responders) to hand out, in
                          order; empty means "always a plain 200"
        """
        self.responses: "list[Response | Responder]" = list(responses) or [Response()]
        self.requests: list[Request] = []

    def next(self, request: Request) -> Response:
        """
        Record the request and produce the next response.

        A canned :py:class:`~action0.req.response.Response` is copied (so
        callers mutating it cannot affect later answers) and gets the
        request attached as
        :py:attr:`~action0.req.response.Response.request`; a responder
        callable is invoked and its result returned as-is.

        :param request: the request the backend was asked to send
        :return: the response to answer with
        """
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        scripted = self.responses[index]
        if isinstance(scripted, Response):
            return scripted.copy(request=request)
        return scripted(request)


class StubBackend(BaseSyncBackend):
    """
    A :py:data:`~action0.client.backend.SyncBackend` test double: answers
    with canned responses and records the requests.

    Example — scripted responses are handed out in order, the last one
    repeats::

        >>> from action0.req import Request, Response
        >>>
        >>> backend = StubBackend(Response(200), Response(503))
        >>> request = Request("https://api.example.com/health")
        >>> [backend.send(request).status for _ in range(3)]
        [200, 503, 503]

    A callable stands in for dynamic behavior, including raising::

        >>> def flaky(request: Request) -> Response:
        ...     raise ConnectionResetError("nope")
        >>> backend = StubBackend(flaky)
        >>> backend.send(request)
        Traceback (most recent call last):
            ...
        ConnectionResetError: nope
    """

    def __init__(self, *responses: "Response | Responder", hooks: Iterable[Hook] = ()) -> None:
        """
        :param responses: the responses (or responder callables) to answer
                          with, in order — the last one repeats; none means
                          "always a plain 200"
        :param hooks: the instrumentation hooks to run around every send,
                      like on any real backend
        """
        super().__init__(hooks)
        self._script = _Script(responses)

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return self._script.requests

    def _send(self, request: Request) -> Response:
        """
        Answer from the script instead of doing I/O.

        :param request: the request to answer
        :return: the next scripted response
        """
        return self._script.next(request)

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``StubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self.requests)} requests)"


class AsyncStubBackend(BaseAsyncBackend):
    """
    An :py:data:`~action0.client.backend.AsyncBackend` test double:
    behaves exactly like :py:class:`StubBackend`, but ``send`` returns a
    coroutine like a real async backend.

    Example::

        >>> import asyncio
        >>> from action0.req import Request, Response
        >>>
        >>> backend = AsyncStubBackend(Response(204))
        >>> asyncio.run(backend.send(Request("https://api.example.com/ping"))).status
        204
    """

    def __init__(self, *responses: "Response | Responder", hooks: Iterable[Hook] = ()) -> None:
        """
        :param responses: the responses (or responder callables) to answer
                          with, in order — the last one repeats; none means
                          "always a plain 200"
        :param hooks: the instrumentation hooks to run around every send,
                      like on any real backend
        """
        super().__init__(hooks)
        self._script = _Script(responses)

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return self._script.requests

    async def _send(self, request: Request) -> Response:
        """
        Answer from the script instead of doing I/O.

        :param request: the request to answer
        :return: the next scripted response
        """
        return self._script.next(request)

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``AsyncStubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self.requests)} requests)"


class DeferredStubBackend(BaseDeferredBackend):
    """
    A :py:data:`~action0.client.backend.DeferredBackend` test double:
    behaves exactly like :py:class:`StubBackend`, but ``send`` returns an
    already-fired :py:class:`~twisted.internet.defer.Deferred` like a real
    Twisted backend. The class is importable without twisted installed;
    calling ``send`` requires it.

    Example (:py:func:`deferred_result` extracts fired results in tests)::

        >>> from action0.req import Request, Response
        >>>
        >>> backend = DeferredStubBackend(Response(204))
        >>> deferred = backend.send(Request("https://api.example.com/ping"))
        >>> deferred_result(deferred).status
        204
    """

    def __init__(self, *responses: "Response | Responder", hooks: Iterable[Hook] = ()) -> None:
        """
        :param responses: the responses (or responder callables) to answer
                          with, in order — the last one repeats; none means
                          "always a plain 200"
        :param hooks: the instrumentation hooks to run around every send,
                      like on any real backend
        """
        super().__init__(hooks)
        self._script = _Script(responses)

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return self._script.requests

    def _send(self, request: Request) -> "Deferred[Response]":
        """
        Answer from the script instead of doing I/O, as a fired Deferred.
        A raising responder is surfaced through the Deferred by the base
        class, matching real Twisted backend behavior.

        :param request: the request to answer
        :return: a Deferred already fired with the next scripted response
        """
        from twisted.internet.defer import succeed

        return succeed(self._script.next(request))

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``DeferredStubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self.requests)} requests)"


def deferred_result(deferred: "Deferred[T]") -> T:
    """
    The result of an already-fired Deferred — the assertion helper for
    testing Twisted code paths without running a reactor: the stub backend
    (and error cases of the real one) fire their Deferreds synchronously.

    :param deferred: the fired Deferred to unwrap
    :return: the value the Deferred fired with
    :raises BaseException: the exception the Deferred failed with, if it
                           failed
    :raises AssertionError: if the Deferred has not fired yet
    """
    from twisted.python.failure import Failure

    results: list[Any] = []
    deferred.addBoth(results.append)
    if not results:
        raise AssertionError("the Deferred has not fired yet")
    result = results[0]
    if isinstance(result, Failure):
        result.raiseException()
    return cast("T", result)

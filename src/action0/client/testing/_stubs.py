"""
The stub backends: one test double per execution model, for tests that
inject the backend into the code under test.

Each stub owns a private :py:class:`~action0.client.testing.MockRouter`
(never activated globally) and answers through it, so injection and
process-wide mocking share one API: ``backend.router`` takes routes like
any router, and the responses given to the constructor are what requests
get that no such route matches.
"""

import threading
from collections.abc import Callable
from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar
from typing import cast

from action0.req import Request
from action0.req import Response

from ..backend import BaseAsyncBackend
from ..backend import BaseDeferredBackend
from ..backend import BaseSyncBackend
from ..hooks import Hook
from ._router import MockRouter
from ._router import Route
from ._router import _Pattern

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
    The scripted answers of a stub backend: a sequence of responses (or
    responders) handed out one per request, with the last one repeating
    forever — so a single-response stub answers any number of requests
    predictably. Used as the side effect of the fallback route of the
    stub's router.
    """

    def __init__(self, responses: "tuple[Response | Responder, ...]") -> None:
        """
        :param responses: the responses (or responders) to hand out, in
                          order; empty means "always a plain 200"
        """
        self._responses: "list[Response | Responder]" = list(responses) or [Response()]
        self._served = 0
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> Response:
        """
        Produce the next scripted answer.

        :param request: the request the backend was asked to send
        :return: the canned response (the router copies it and attaches
                 the request), or what the responder returned
        """
        with self._lock:
            index = min(self._served, len(self._responses) - 1)
            self._served += 1
        scripted = self._responses[index]
        if isinstance(scripted, Response):
            return scripted
        return scripted(request)


def _stub_router(responses: "tuple[Response | Responder, ...]") -> MockRouter:
    """
    The private router of a stub backend: no routes yet, and the script
    as the answer for everything no route matches.

    :param responses: the scripted responses of the stub
    :return: the router
    """
    router = MockRouter(assert_all_called=False)
    fallback = Route(_Pattern(None, None, {}, None), name="script")
    fallback.side_effect = _Script(responses)
    router._fallback = fallback
    return router


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

    For answers that depend on the request, add routes to the stub's
    :py:attr:`router` — the script then answers only what no route
    matches::

        >>> backend = StubBackend(Response(404))
        >>> _ = backend.router.get("/health").respond(200, text="fine")
        >>> backend.send(Request("https://api.example.com/health")).body_str()
        'fine'
        >>> backend.send(Request("https://api.example.com/other")).status
        404
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
        self._router = _stub_router(responses)

    @property
    def router(self) -> MockRouter:
        """The stub's own router: add routes to answer by request, and
        use its call records and assertions."""
        return self._router

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return [call.request for call in self._router.calls]

    def _send(self, request: Request) -> Response:
        """
        Answer from the stub's router instead of doing I/O.

        :param request: the request to answer
        :return: the response of the matching route, or the next
                 scripted one
        """
        return self._router._dispatch_sync(self, request)

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``StubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self._router.calls)} requests)"


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
        self._router = _stub_router(responses)

    @property
    def router(self) -> MockRouter:
        """The stub's own router: add routes to answer by request, and
        use its call records and assertions."""
        return self._router

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return [call.request for call in self._router.calls]

    async def _send(self, request: Request) -> Response:
        """
        Answer from the stub's router instead of doing I/O.

        :param request: the request to answer
        :return: the response of the matching route, or the next
                 scripted one
        """
        return await self._router._dispatch_async(self, request)

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``AsyncStubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self._router.calls)} requests)"


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
        self._router = _stub_router(responses)

    @property
    def router(self) -> MockRouter:
        """The stub's own router: add routes to answer by request, and
        use its call records and assertions."""
        return self._router

    @property
    def requests(self) -> list[Request]:
        """Every request sent through this backend, in order."""
        return [call.request for call in self._router.calls]

    def _send(self, request: Request) -> "Deferred[Response]":
        """
        Answer from the stub's router instead of doing I/O, as a fired
        Deferred. A raising responder is surfaced through the Deferred by
        the base class, matching real Twisted backend behavior.

        :param request: the request to answer
        :return: a Deferred already fired with the response
        """
        return self._router._dispatch_deferred(self, request)

    def __repr__(self) -> str:
        """
        :return: the backend with its request count, e.g.
                 ``DeferredStubBackend(2 requests)``
        """
        return f"{self.__class__.__name__}({len(self._router.calls)} requests)"


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

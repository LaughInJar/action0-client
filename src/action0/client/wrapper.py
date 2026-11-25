"""
Base classes for backend wrappers: backends that pass every request on
to another backend and do something of their own around that — wait for
a rate limiter, retry, answer from a cache, switch between two backends.

A wrapper is the tool for everything a :py:class:`~action0.client.hooks.Hook`
cannot do: hooks are plain synchronous callbacks, so they can neither
``await`` nor send a request a second time nor skip the send. A wrapper's
``send`` is ordinary code of its execution model, so it can.

There is one base class per execution model. Each is itself a backend of
that model — it plugs into :py:class:`~action0.client.client.Client` and
:py:class:`~action0.client.api.APIClient` like the backend it wraps, and
the clients' static return types stay what they were. A subclass
overrides ``send`` and reaches the wrapped backend through ``inner``;
``map`` and ``repr()`` are taken care of.

The library's own wrappers are built on these classes:
:py:class:`~action0.client.retry.RetryingSyncBackend`,
:py:class:`~action0.client.caching.CachingSyncBackend` and their async
and Deferred counterparts.
"""

from typing import TYPE_CHECKING
from typing import Awaitable
from typing import Callable
from typing import TypeVar
from typing import cast

from action0.req import Request
from action0.req import Response

from .backend import Backend

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

T = TypeVar("T")
S = TypeVar("S")


class SyncBackendWrapper:
    """
    The base class for a wrapper around a synchronous backend — itself a
    ``Backend[Response]``.

    Example — a throttle that blocks until the next request is allowed::

        >>> from collections.abc import Callable
        >>> from action0.client import Backend, Client
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> class Throttled(SyncBackendWrapper):
        ...     def __init__(self, inner: Backend[Response], wait: Callable[[], None]) -> None:
        ...         super().__init__(inner)
        ...         self._wait = wait
        ...
        ...     def send(self, request: Request) -> Response:
        ...         self._wait()
        ...         return self.inner.send(request)
        >>>
        >>> backend = Throttled(StubBackend(Response(204)), wait=lambda: print("waiting"))
        >>> Client(backend).send(Request("https://api.example.com/")).status
        waiting
        204
        >>> backend
        Throttled(StubBackend(1 requests))
    """

    def __init__(self, inner: Backend[Response]) -> None:
        """
        :param inner: the backend to wrap — the one that actually sends,
                      or another wrapper
        """
        self._inner = inner

    @property
    def inner(self) -> Backend[Response]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    def send(self, request: Request) -> Response:
        """
        Send the request through the wrapped backend. The method to
        override: do your part and call ``self.inner.send(request)``.

        :param request: the request to send
        :return: the response
        """
        return self._inner.send(request)

    def map(self, result: T, fn: Callable[[T], S]) -> S:
        """
        Apply a function to a result of :py:meth:`send`, the way the
        wrapped backend does it — synchronously that is ``fn(result)``.

        :param result: a value as returned by :py:meth:`send`
        :param fn: the function to apply
        :return: the return value of ``fn``
        """
        # the protocol types map() loosely (see Backend.map); the wrapper
        # is of the same execution model, so its precise signature holds
        return cast("S", self._inner.map(result, fn))

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend, e.g.
                 ``Throttled(RequestsBackend())``
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class AsyncBackendWrapper:
    """
    The base class for a wrapper around an async backend — itself a
    ``Backend[Awaitable[Response]]``. Its ``send`` is a coroutine
    function, so a subclass can ``await`` before, after and instead of
    the wrapped send.

    Example — at most two requests in flight, the others wait their
    turn::

        >>> import asyncio
        >>> from collections.abc import Awaitable
        >>> from action0.client import Backend, Client
        >>> from action0.client.testing import AsyncStubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> class Throttled(AsyncBackendWrapper):
        ...     def __init__(self, inner: Backend[Awaitable[Response]], limit: int) -> None:
        ...         super().__init__(inner)
        ...         self._slots = asyncio.Semaphore(limit)
        ...
        ...     async def send(self, request: Request) -> Response:
        ...         async with self._slots:
        ...             return await self.inner.send(request)
        >>>
        >>> async def main() -> list[int]:
        ...     client = Client(Throttled(AsyncStubBackend(Response(204)), limit=2))
        ...     request = Request("https://api.example.com/")
        ...     responses = await asyncio.gather(*(client.send(request) for _ in range(5)))
        ...     return [response.status for response in responses]
        >>>
        >>> asyncio.run(main())
        [204, 204, 204, 204, 204]
    """

    def __init__(self, inner: Backend[Awaitable[Response]]) -> None:
        """
        :param inner: the backend to wrap — the one that actually sends,
                      or another wrapper
        """
        self._inner = inner

    @property
    def inner(self) -> Backend[Awaitable[Response]]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    async def send(self, request: Request) -> Response:
        """
        Send the request through the wrapped backend. The method to
        override: do your part — awaiting whatever it takes — and
        ``await self.inner.send(request)``.

        :param request: the request to send
        :return: (an awaitable of) the response
        """
        return await self._inner.send(request)

    def map(self, result: Awaitable[T], fn: Callable[[T], S]) -> Awaitable[S]:
        """
        Apply a function inside an awaitable result of :py:meth:`send`,
        the way the wrapped backend does it.

        :param result: an awaitable as returned by :py:meth:`send`
        :param fn: the function to apply to the awaited value
        :return: an awaitable of the return value of ``fn``
        """
        # see SyncBackendWrapper.map for the cast
        return cast("Awaitable[S]", self._inner.map(result, fn))

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend, e.g.
                 ``Throttled(AsyncHttpxBackend())``
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class DeferredBackendWrapper:
    """
    The base class for a wrapper around a Twisted backend — itself a
    ``Backend[Deferred[Response]]``. The class is importable without
    twisted installed.

    Example — wait for a Deferred (here: an already fired one) before
    every send::

        >>> from collections.abc import Callable
        >>> from action0.client import Backend, Client
        >>> from action0.client.testing import DeferredStubBackend, deferred_result
        >>> from action0.req import Request, Response
        >>> from twisted.internet.defer import Deferred, succeed
        >>>
        >>> class Throttled(DeferredBackendWrapper):
        ...     def __init__(
        ...         self, inner: Backend[Deferred[Response]], acquire: Callable[[], Deferred[None]]
        ...     ) -> None:
        ...         super().__init__(inner)
        ...         self._acquire = acquire
        ...
        ...     def send(self, request: Request) -> Deferred[Response]:
        ...         return self._acquire().addCallback(lambda _: self.inner.send(request))
        >>>
        >>> backend = Throttled(DeferredStubBackend(Response(204)), acquire=lambda: succeed(None))
        >>> deferred_result(Client(backend).send(Request("https://api.example.com/"))).status
        204
    """

    def __init__(self, inner: "Backend[Deferred[Response]]") -> None:
        """
        :param inner: the backend to wrap — the one that actually sends,
                      or another wrapper
        """
        self._inner = inner

    @property
    def inner(self) -> "Backend[Deferred[Response]]":
        """The wrapped backend doing the actual sends."""
        return self._inner

    def send(self, request: Request) -> "Deferred[Response]":
        """
        Send the request through the wrapped backend. The method to
        override: do your part and return (or chain onto)
        ``self.inner.send(request)``.

        :param request: the request to send
        :return: a Deferred firing with the response
        """
        return self._inner.send(request)

    def map(self, result: "Deferred[T]", fn: Callable[[T], S]) -> "Deferred[S]":
        """
        Apply a function inside a Deferred result of :py:meth:`send`, the
        way the wrapped backend does it — Twisted's ``addCallback``.

        :param result: a Deferred as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Deferred firing with the return value of ``fn``
        """
        # see SyncBackendWrapper.map for the cast
        return cast("Deferred[S]", self._inner.map(result, fn))

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend, e.g.
                 ``Throttled(TwistedBackend())``
        """
        return f"{self.__class__.__name__}({self._inner!r})"

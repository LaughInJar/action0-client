"""
The generic API client (:py:class:`APIClient`): binds a backend, a base URL
and default headers, and sends typed
:py:class:`~action0.client.operation.Operation` instances.
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import TYPE_CHECKING
from typing import Any
from typing import Awaitable
from typing import Generic
from typing import TypeVar
from typing import overload

from action0.req import Headers
from action0.req import Request
from action0.req import Response
from action0.req.headers import HeaderTypes
from action0.url import Url

from .backend import Backend
from .backend import BackendT_co
from .operation import Operation

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

R = TypeVar("R")
"""The parsed result type of the operation being sent."""


class APIClient(Generic[BackendT_co]):
    """
    A client for one HTTP API: it holds the backend, the base URL and the
    default headers, and :py:meth:`send` turns an
    :py:class:`~action0.client.operation.Operation` into a request, sends
    it and parses the response — through :py:meth:`Operation.parse
    <action0.client.operation.Operation.parse>`, attached via the backend's
    ``map``.

    The result type follows the operation *and* the backend: for an
    ``Operation[Item]``, ``send`` returns

    - ``Item`` with a sync backend,
    - ``Awaitable[Item]`` with an async backend,
    - ``Deferred[Item]`` with a Twisted backend

    — and the type checker knows it. The same client class serves every
    execution model; only the backend changes. (The three shipped models
    are typed precisely; a backend with any other wrapper type works the
    same way at runtime, its ``send`` result is just typed ``Any`` — see
    :py:meth:`send`.)

    Example (with the test-double backend standing in for a real one)::

        >>> from typing import Any
        >>> from action0.client import JsonOperation, query
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Response
        >>>
        >>> class SearchItems(JsonOperation[Any]):
        ...     path = "/items"
        ...     q: str = query()
        >>>
        >>> backend = StubBackend(Response(200, body='{"hits": 2}'))
        >>> client = APIClient(backend, "https://api.example.com/v1")
        >>> client.send(SearchItems(q="thing"))
        {'hits': 2}
        >>> backend.requests[0].url.as_str()
        'https://api.example.com/v1/items?q=thing'

    The same operations sent asynchronously — only the backend differs::

        >>> import asyncio
        >>> from action0.client.testing import AsyncStubBackend
        >>>
        >>> client = APIClient(AsyncStubBackend(Response(200, body="[]")), "https://api.example.com/v1")
        >>> asyncio.run(client.send(SearchItems(q="thing")))
        []

    Real API clients usually subclass, fixing base URL and auth (keep the
    backend type variable so the typed overloads keep working)::

        class ExampleClient(APIClient[BackendT_co]):
            def __init__(self, backend: BackendT_co, token: str) -> None:
                super().__init__(
                    backend,
                    "https://api.example.com/v1",
                    headers={"Authorization": f"Bearer {token}"},
                )
    """

    def __init__(
        self,
        backend: BackendT_co,
        base_url: str | Url,
        *,
        headers: HeaderTypes | None = None,
    ) -> None:
        """
        :param backend: the backend performing the HTTP I/O — any
                        implementation of the
                        :py:class:`~action0.client.backend.Backend`
                        protocol, whatever its execution model
        :param base_url: the URL the operations' paths are appended to,
                         e.g. ``"https://api.example.com/v2"`` (a given
                         ``Url`` instance is copied)
        :param headers: default header lines added to every request that
                        does not set them itself — the typical place for
                        ``Authorization`` and friends
        """
        self._backend = backend
        self.base_url = Url(base_url) if isinstance(base_url, str) else base_url.copy()
        """The URL every operation path is appended to."""
        self.headers = Headers(headers)
        """The default headers, added to requests that don't set them."""

    @property
    def backend(self) -> BackendT_co:
        """The backend this client sends through (as its concrete type)."""
        return self._backend

    def prepare(self, request: Request) -> Request:
        """
        Last touches before a request is sent: the default
        :py:attr:`headers` are added — per header field, only if the
        request does not set that field itself.

        Override this for dynamic per-request work like signing or
        token refresh (call ``super().prepare(request)`` to keep the
        default-header behavior)::

            class SignedClient(APIClient[BackendT_co]):
                def prepare(self, request: Request) -> Request:
                    request = super().prepare(request)
                    request.headers["X-Signature"] = self._sign(request)
                    return request

        :param request: the request built from an operation
        :return: the request to actually send
        """
        for name in self.headers:
            if name not in request.headers:
                request.headers.add(name, self.headers.get_all(name))
        return request

    # The implementation below is execution-model-agnostic; only this
    # typing facade is not: "the backend's wrapper, around R" cannot be
    # expressed for an arbitrary wrapper (Python has no higher-kinded
    # types), so the shipped execution models are spelled out as overloads
    # — Deferred before Awaitable, because a Deferred *is* awaitable and
    # the narrower claim must win. Every other wrapper type falls through
    # to the last overload and is typed Any.
    @overload
    def send(self: APIClient[Backend[Response]], operation: Operation[R]) -> R: ...

    @overload
    def send(
        self: APIClient[Backend[Deferred[Response]]], operation: Operation[R]
    ) -> Deferred[R]: ...

    @overload
    def send(
        self: APIClient[Backend[Awaitable[Response]]], operation: Operation[R]
    ) -> Awaitable[R]: ...

    @overload
    def send(self: APIClient[Backend[Future[Response]]], operation: Operation[R]) -> Future[R]: ...

    @overload
    def send(self, operation: Operation[R]) -> Any: ...

    def send(self, operation: Operation[Any]) -> Any:
        """
        Send an operation: build its request (:py:meth:`Operation.as_request
        <action0.client.operation.Operation.as_request>` with this client's
        base URL, then :py:meth:`prepare`), send it through the backend,
        and parse the response via :py:meth:`Operation.parse
        <action0.client.operation.Operation.parse>` — attached with the
        backend's ``map``, so it runs inside whatever wrapper the backend
        returns.

        :param operation: the operation to execute
        :return: the parsed result, wrapped according to the backend's
                 execution model: plain for a sync backend, awaitable for
                 an async backend, a Deferred for a Twisted backend, a
                 Future for a thread-pool backend — those four are typed
                 precisely; any other execution model works the same way
                 but is typed ``Any`` (for precise typing of a custom
                 wrapper, subclass and re-declare ``send`` — see the
                 *Other execution models* section of the guide)
        :raises action0.client.errors.ClientError: transport failures and
                response parsing failures (for async and Twisted backends
                they arrive at ``await`` time / in the errback instead of
                being raised here)
        """
        request = self.prepare(operation.as_request(self.base_url))
        backend = self._backend
        return backend.map(backend.send(request), operation.parse)

    def __repr__(self) -> str:
        """
        :return: the client with its base URL and backend, e.g.
                 ``APIClient(https://api.example.com/v1 via StubBackend())``
        """
        return f"{self.__class__.__name__}({self.base_url.as_str()} via {self._backend!r})"

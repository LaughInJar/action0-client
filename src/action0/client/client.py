"""
The generic HTTP client (:py:class:`Client`): one class, any backend, with
the return type of :py:meth:`Client.send` following the backend's execution
model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Awaitable
from typing import Generic
from typing import TypeVar
from typing import cast
from typing import overload

from action0.req import Request
from action0.req import Response

from .backend import AsyncBackend
from .backend import DeferredBackend
from .backend import SyncBackend

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

BackendT_co = TypeVar(
    "BackendT_co", bound="SyncBackend | AsyncBackend | DeferredBackend", covariant=True
)
"""The backend a client is parameterized with; covariant so that e.g. a
``Client[RequestsBackend]`` is also a ``Client[SyncBackend]`` — that is what
resolves the ``send`` overloads to the right return type."""


class Client(Generic[BackendT_co]):
    """
    A thin, fully typed facade over a backend: ``Client(backend).send(request)``
    sends a raw :py:class:`~action0.req.request.Request` and returns the
    :py:class:`~action0.req.response.Response` in whatever wrapper the
    backend's execution model dictates — the type checker knows which:

    - ``Client(RequestsBackend()).send(request)`` is a ``Response``,
    - ``await Client(AsyncHttpxBackend()).send(request)`` is a ``Response``,
    - ``Client(TwistedBackend()).send(request)`` is a ``Deferred[Response]``.

    For talking to a specific API with typed operations, use
    :py:class:`~action0.client.api.APIClient` instead — this class is the
    raw-request building block.

    Example (with the test-double backend standing in for a real one)::

        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> client = Client(StubBackend(Response(204)))
        >>> client.send(Request("https://api.example.com/ping")).status
        204

    The backend decides the execution model, the code stays the same::

        >>> import asyncio
        >>> from action0.client.testing import AsyncStubBackend
        >>>
        >>> client = Client(AsyncStubBackend(Response(204)))
        >>> asyncio.run(client.send(Request("https://api.example.com/ping"))).status
        204
    """

    def __init__(self, backend: BackendT_co) -> None:
        """
        :param backend: the backend performing the HTTP I/O — an
                        implementation of
                        :py:class:`~action0.client.backend.SyncBackend`,
                        :py:class:`~action0.client.backend.AsyncBackend` or
                        :py:class:`~action0.client.backend.DeferredBackend`
        """
        self._backend = backend

    @property
    def backend(self) -> BackendT_co:
        """The backend this client sends through (as its concrete type)."""
        return self._backend

    @overload
    def send(self: Client[SyncBackend], request: Request) -> Response: ...

    @overload
    def send(self: Client[DeferredBackend], request: Request) -> Deferred[Response]: ...

    @overload
    def send(self: Client[AsyncBackend], request: Request) -> Awaitable[Response]: ...

    def send(self, request: Request) -> Any:
        """
        Send the request through the backend.

        :param request: the request to send
        :return: the response, wrapped according to the backend's execution
                 model: the plain :py:class:`~action0.req.response.Response`
                 for a sync backend, an ``Awaitable[Response]`` for an async
                 backend, a ``Deferred[Response]`` for a Twisted backend
        :raises action0.client.errors.TransportError: if no response could
                be obtained (for async and Twisted backends the error
                arrives at ``await`` time / in the errback instead of being
                raised here)
        """
        # all three backend protocols share the same send() call shape; the
        # checker cannot type the union call generically, so the
        # implementation picks one protocol pro forma — the overloads above
        # give callers the precise types
        return cast("SyncBackend", self._backend).send(request)

    def __repr__(self) -> str:
        """
        :return: the client with its backend, e.g. ``Client(StubBackend())``
        """
        return f"{self.__class__.__name__}({self._backend!r})"

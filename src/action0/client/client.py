"""
The generic HTTP client (:py:class:`Client`): one class, any backend —
the return type of :py:meth:`Client.send` is exactly what the given
backend's ``send`` returns.
"""

from typing import Generic

from action0.req import Request

from .backend import Backend
from .backend import SendResultT_co


class Client(Generic[SendResultT_co]):
    """
    A thin, fully typed facade over a backend: ``Client(backend).send(request)``
    sends a raw :py:class:`~action0.req.request.Request` and returns the
    :py:class:`~action0.req.response.Response` in whatever wrapper the
    backend's execution model dictates. The wrapper type is *derived from
    the backend* (the class is generic over
    :py:data:`~action0.client.backend.SendResultT_co`), not enumerated
    anywhere — so this works for any execution model, including ones this
    library has never heard of:

    - ``Client(RequestsBackend()).send(request)`` is a ``Response``,
    - ``await Client(AsyncHttpxBackend()).send(request)`` is a ``Response``,
    - ``Client(TwistedBackend()).send(request)`` is a ``Deferred[Response]``,
    - with your own ``Backend[SomeWrapper[Response]]``, ``send`` returns
      a ``SomeWrapper[Response]``.

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

    def __init__(self, backend: Backend[SendResultT_co]) -> None:
        """
        :param backend: the backend performing the HTTP I/O — any
                        implementation of the
                        :py:class:`~action0.client.backend.Backend`
                        protocol, whatever its execution model
        """
        self._backend = backend

    @property
    def backend(self) -> Backend[SendResultT_co]:
        """The backend this client sends through, as the
        :py:class:`~action0.client.backend.Backend` protocol. (The client
        is generic over the backend's *wrapper type*, not its concrete
        class — keep your own reference for backend-specific API like
        ``close()``.)"""
        return self._backend

    def send(self, request: Request) -> SendResultT_co:
        """
        Send the request through the backend.

        :param request: the request to send
        :return: exactly what the backend's ``send`` returns: the response,
                 wrapped according to the backend's execution model — the
                 plain :py:class:`~action0.req.response.Response` for a
                 sync backend, an ``Awaitable[Response]`` for an async
                 backend, a ``Deferred[Response]`` for a Twisted backend
        :raises action0.client.errors.TransportError: if no response could
                be obtained (async-style backends deliver the error
                through their wrapper instead of raising here)
        """
        return self._backend.send(request)

    def __repr__(self) -> str:
        """
        :return: the client with its backend, e.g.
                 ``Client(StubBackend(0 requests))``
        """
        return f"{self.__class__.__name__}({self._backend!r})"

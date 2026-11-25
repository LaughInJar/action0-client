"""
The implementation shared by the httpx and the httpx2 backends.

`httpx2 <https://pypi.org/project/httpx2/>`_ is a fork of httpx with the
same API under another module name. The backends for the two libraries
are therefore one implementation, parameterised by the library module:
each concrete class in :py:mod:`~action0.client.backends.httpx` and
:py:mod:`~action0.client.backends.httpx2` sets ``_lib`` to its module and
the type parameter to that library's client class — so the static types
stay precise (an ``Httpx2Backend`` takes and returns ``httpx2.Client``),
and each backend creates clients and recognises exceptions of its own
library only.

This module imports neither library. That is what makes it work whether
or not an application has called ``httpx2.alias_httpx()``, which makes
``import httpx`` load httpx2.
"""

from typing import Any
from typing import Callable
from typing import ClassVar
from typing import Generic
from typing import Iterable
from typing import Self
from typing import TypeVar

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyTypes

from ..backend import BaseAsyncBackend
from ..backend import BaseSyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook
from ..options import BodyMode
from ._body import CHUNK_SIZE
from ._body import adeliver
from ._body import body_mode
from ._body import deliver
from ._loops import LoopBound
from ._loops import PerLoop

DEFAULT_TIMEOUT = 30.0
"""The default number of seconds the library waits (connect, read, write
and pool acquisition each)."""

ClientT = TypeVar("ClientT")
"""The synchronous client class of the library — ``httpx.Client`` or
``httpx2.Client``."""

AsyncClientT = TypeVar("AsyncClientT")
"""The asynchronous client class of the library — ``httpx.AsyncClient``
or ``httpx2.AsyncClient``."""

#: the top-level module names of the two libraries of the family
_FAMILY = frozenset({"httpx", "httpx2"})


def _request_arguments(request: Request, async_: bool) -> dict[str, Any]:
    """
    The keyword arguments for the client's ``request`` /
    ``build_request`` describing the given request. The body is passed
    as ``content``: in-memory bodies as bytes, a streaming
    :py:class:`~action0.req.body.BodyProducer` as the chunk iterator
    native to the client's execution model.

    :param request: the request to convert
    :param async_: whether the async chunk iterator is wanted for a
                   streaming body
    :return: the keyword arguments (without the body for bodyless requests)
    """
    arguments: dict[str, Any] = {
        "method": request.method,
        "url": request.url.as_str(),
        # a list of pairs keeps multiple header lines per field intact
        "headers": request.headers.as_lines(),
    }
    if request.body is not None:
        if isinstance(request.body, (bytes, str)):
            arguments["content"] = request.body_bytes()
        else:
            arguments["content"] = request.body.achunks() if async_ else request.body.chunks()
    return arguments


def _convert_response(request: Request, answer: Any, body: "BodyTypes") -> Response:
    """
    Convert the library's response back into an
    :py:class:`~action0.req.response.Response`.

    :param request: the request that produced the response
    :param answer: the ``httpx.Response`` / ``httpx2.Response``
    :param body: the response body — the preloaded bytes, a streaming
                 producer over the still-open connection, or a spool
    :return: the converted response
    """
    return Response(
        answer.status_code,
        # multi_items() keeps multiple lines per field intact
        headers=answer.headers.multi_items(),
        body=body,
        reason=answer.reason_phrase or None,
        http_version=answer.http_version,
        request=request,
    )


def _translate(lib: Any, error: Exception, request: Request) -> BaseException:
    """
    Normalize the library's exceptions into the
    :py:class:`~action0.client.errors.TransportError` family.

    :param lib: the library module whose exception classes to recognise
    :param error: the exception raised while sending
    :param request: the request that was being sent
    :return: the normalized exception (unknown types pass through)
    """
    if isinstance(error, lib.TimeoutException):
        return TimeoutError(str(error) or type(error).__name__, request=request)
    if isinstance(error, (lib.TransportError, lib.HTTPError)):
        return TransportError(str(error) or type(error).__name__, request=request)
    return error


def _refuse_foreign_client(backend: object, lib: Any, expected: type, client: object) -> None:
    """
    Fail early when a backend is handed the client of the *other*
    library of the family. It would mostly work — the APIs are the same —
    until the first error, which the backend would not recognise and
    pass on untranslated.

    Only clients that recognisably belong to the other library are
    refused; stand-ins of any other origin are none of this check's
    business.

    :param backend: the backend being constructed
    :param lib: the backend's library module
    :param expected: the client class of that library
    :param client: the client that was passed in
    :raises TypeError: if the client is the other library's
    """
    if isinstance(client, expected):
        return
    origin = type(client).__module__.partition(".")[0]
    if origin in _FAMILY:
        raise TypeError(
            f"{type(backend).__name__} needs a {lib.__name__}.{expected.__name__}, got a"
            f" {type(client).__module__}.{type(client).__qualname__} — use the backend"
            f" of the {origin} library for it"
        )


class SyncHttpxFamilyBackend(BaseSyncBackend, Generic[ClientT]):
    """
    The synchronous backend of the httpx family, generic over the
    library's client class. Use the concrete
    :py:class:`~action0.client.backends.httpx.HttpxBackend` or
    :py:class:`~action0.client.backends.httpx2.Httpx2Backend`.
    """

    _lib: ClassVar[Any]
    """The library module — set by the concrete class."""

    def __init__(
        self,
        client: "ClientT | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        stream: bool = False,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param client: the library's client to send through — configure
                       connection limits, HTTP/2, proxies etc. there;
                       ``None`` creates (and owns) a default one, closed
                       again by :py:meth:`close`. The ``timeout`` and
                       ``follow_redirects`` arguments only apply to the
                       created client.
        :param timeout: the seconds the library waits (for connect, read,
                        write and pool acquisition each); ``None`` waits
                        forever
        :param follow_redirects: whether 3xx responses are followed
        :param stream: whether response bodies are streamed by default
                       (``send`` then returns at headers arrival); a
                       request's own
                       :py:attr:`~action0.client.options.RequestOptions.body`
                       option takes precedence
        :param hooks: the instrumentation hooks to run around every send
        :raises TypeError: if the client belongs to the other library of
                           the httpx family
        """
        super().__init__(hooks)
        lib = self._lib
        self._owns_client = client is None
        if client is not None:
            _refuse_foreign_client(self, lib, lib.Client, client)
        # Any: the client class is a type parameter, so its methods are
        # not known here; ``client`` returns it precisely typed
        self._client: Any = (
            client
            if client is not None
            else lib.Client(timeout=timeout, follow_redirects=follow_redirects)
        )
        self._stream = stream

    @property
    def client(self) -> ClientT:
        """The library client this backend sends through — the one
        passed in, or the one it created."""
        client: ClientT = self._client
        return client

    def _send(self, request: Request) -> Response:
        """
        Send via the library's client and convert the response back.

        :param request: the request to send
        :return: the response
        """
        arguments = _request_arguments(request, async_=False)
        mode = body_mode(request, self._stream)
        client = self._client
        if mode is BodyMode.PRELOAD:
            answer = client.request(**arguments)
            return _convert_response(request, answer, answer.content)
        answer = client.send(client.build_request(**arguments), stream=True)
        body = deliver(
            request, mode, answer.iter_bytes(CHUNK_SIZE), answer.close, self.translate_error
        )
        return _convert_response(request, answer, body)

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize the library's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        return _translate(self._lib, error, request)

    def close(self) -> None:
        """
        Close the underlying client — but only if this backend created
        it; a client that was passed in is left to its owner.
        """
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        """
        :return: the backend itself, closed again when the ``with`` block
                 ends
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """
        Close the backend on leaving the ``with`` block.

        :param exc_info: the exception leaving the block, if any (ignored)
        """
        self.close()

    def __repr__(self) -> str:
        """
        :return: the backend class name (no configuration secrets)
        """
        return f"{self.__class__.__name__}()"


class AsyncHttpxFamilyBackend(BaseAsyncBackend, Generic[AsyncClientT]):
    """
    The asyncio backend of the httpx family, generic over the library's
    async client class. Use the concrete
    :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend` or
    :py:class:`~action0.client.backends.httpx2.AsyncHttpx2Backend`.
    """

    _lib: ClassVar[Any]
    """The library module — set by the concrete class."""

    def __init__(
        self,
        client: "AsyncClientT | None" = None,
        *,
        client_factory: "Callable[[], AsyncClientT] | None" = None,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        stream: bool = False,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param client: a single client of the library to send through.
                       It stays the caller's: the backend never closes
                       it. An async client is bound to the event loop it
                       is first used on, and so is a backend given one —
                       using it from another loop raises a
                       :py:class:`RuntimeError`.
        :param client_factory: creates the clients this backend owns —
                               one per event loop, on first use in that
                               loop, closed when the loop shuts down.
                               The way to configure connection limits,
                               HTTP/2, proxies etc. for a backend that is
                               shared between event loops. Cannot be
                               combined with ``client``.
        :param timeout: the seconds the library waits (for connect, read,
                        write and pool acquisition each); ``None`` waits
                        forever. Applies to the default clients only —
                        not with ``client`` or ``client_factory``.
        :param follow_redirects: whether 3xx responses are followed;
                                 applies to the default clients only
        :param stream: whether response bodies are streamed by default
                       (``send`` then returns at headers arrival); a
                       request's own
                       :py:attr:`~action0.client.options.RequestOptions.body`
                       option takes precedence
        :param hooks: the instrumentation hooks to run around every send
        :raises TypeError: if both ``client`` and ``client_factory`` are
                           given, or the client belongs to the other
                           library of the httpx family
        """
        super().__init__(hooks)
        lib = self._lib
        if client is not None and client_factory is not None:
            raise TypeError("pass either client or client_factory, not both")
        self._stream = stream
        # Any: the client class is a type parameter, so its methods are
        # not known here; current_client() returns it precisely typed
        self._client: Any = client
        self._bound: "LoopBound | None" = None
        self._clients: "PerLoop[Any] | None" = None
        if client is not None:
            _refuse_foreign_client(self, lib, lib.AsyncClient, client)
            name = type(self).__name__
            self._bound = LoopBound(
                f"The {lib.__name__}.AsyncClient passed to {name}",
                "pass client_factory=... instead of client=... (or neither)",
            )
        else:

            def default_client() -> Any:
                return lib.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)

            self._clients = PerLoop(client_factory or default_client, _aclose_client)

    async def current_client(self) -> AsyncClientT:
        """
        The library client used for the running event loop: the one that
        was passed in, or this loop's own client — created now if the
        backend has not sent anything on this loop yet.

        :return: the client
        :raises RuntimeError: if a single client was passed in and this
                              is not the loop it is bound to
        """
        if self._clients is not None:
            owned: AsyncClientT = await self._clients.get()
            return owned
        assert self._bound is not None
        self._bound.check()
        passed: AsyncClientT = self._client
        return passed

    async def _send(self, request: Request) -> Response:
        """
        Send via the library's client and convert the response back.

        :param request: the request to send
        :return: (an awaitable of) the response
        """
        # Any: see __init__
        client: Any = await self.current_client()
        arguments = _request_arguments(request, async_=True)
        mode = body_mode(request, self._stream)
        if mode is BodyMode.PRELOAD:
            answer = await client.request(**arguments)
            return _convert_response(request, answer, answer.content)
        answer = await client.send(client.build_request(**arguments), stream=True)
        body = await adeliver(
            request, mode, answer.aiter_bytes(CHUNK_SIZE), answer.aclose, self.translate_error
        )
        return _convert_response(request, answer, body)

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize the library's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        return _translate(self._lib, error, request)

    async def aclose(self) -> None:
        """
        Close the client this backend created for the running event
        loop. Clients of other loops are closed when their loop shuts
        down (or by calling this inside that loop); a client that was
        passed in is left to its owner.
        """
        if self._clients is not None:
            await self._clients.aclose()

    async def __aenter__(self) -> Self:
        """
        :return: the backend itself, closed again when the ``async with``
                 block ends
        """
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """
        Close the backend on leaving the ``async with`` block.

        :param exc_info: the exception leaving the block, if any (ignored)
        """
        await self.aclose()

    def __repr__(self) -> str:
        """
        :return: the backend class name (no configuration secrets)
        """
        return f"{self.__class__.__name__}()"


async def _aclose_client(client: Any) -> None:
    """
    Close an async client of the family.

    :param client: the ``AsyncClient`` to close
    """
    await client.aclose()

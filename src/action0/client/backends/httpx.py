"""
The `httpx <https://www.python-httpx.org/>`_ backends — a
:py:class:`~action0.client.backend.SyncBackend` and an
:py:class:`~action0.client.backend.AsyncBackend` sharing one conversion
logic, since httpx offers both execution models over one API.

Requires the ``httpx`` extra: ``pip install "action0-client[httpx]"``.
"""

from typing import Any
from typing import Iterable

import httpx

from action0.req import Request
from action0.req import Response

from ..backend import BaseAsyncBackend
from ..backend import BaseSyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

DEFAULT_TIMEOUT = 30.0
"""The default number of seconds httpx waits (connect, read, write and
pool acquisition each)."""


def _request_arguments(request: Request, async_: bool) -> dict[str, Any]:
    """
    The keyword arguments for ``httpx.Client.request`` /
    ``httpx.AsyncClient.request`` describing the given request. The body is
    passed as ``content``: in-memory bodies as bytes, a streaming
    :py:class:`~action0.req.body.BodyProducer` as the chunk iterator native
    to the client's execution model.

    :param request: the request to convert
    :param async_: whether the async chunk iterator is wanted for a
                   streaming body
    :return: the keyword arguments (without the body for bodyless requests)
    """
    arguments: dict[str, Any] = {
        "method": request.method,
        "url": request.url.as_str(),
        # httpx accepts header lines as a list of pairs, keeping multiple
        # lines per field intact
        "headers": request.headers.as_lines(),
    }
    if request.body is not None:
        if isinstance(request.body, (bytes, str)):
            arguments["content"] = request.body_bytes()
        else:
            arguments["content"] = request.body.achunks() if async_ else request.body.chunks()
    return arguments


def _convert_response(request: Request, answer: httpx.Response) -> Response:
    """
    Convert an ``httpx.Response`` (with the body already read) back into an
    :py:class:`~action0.req.response.Response`.

    :param request: the request that produced the response
    :param answer: the httpx response
    :return: the converted response
    """
    return Response(
        answer.status_code,
        # multi_items() keeps multiple lines per field intact
        headers=answer.headers.multi_items(),
        body=answer.content,
        reason=answer.reason_phrase or None,
        http_version=answer.http_version,
        request=request,
    )


def _translate(error: Exception, request: Request) -> BaseException:
    """
    Normalize httpx's exceptions into the
    :py:class:`~action0.client.errors.TransportError` family.

    :param error: the exception raised while sending
    :param request: the request that was being sent
    :return: the normalized exception (unknown types pass through)
    """
    if isinstance(error, httpx.TimeoutException):
        return TimeoutError(str(error) or type(error).__name__, request=request)
    if isinstance(error, (httpx.TransportError, httpx.HTTPError)):
        return TransportError(str(error) or type(error).__name__, request=request)
    return error


class HttpxBackend(BaseSyncBackend):
    """
    A synchronous backend driving an :py:class:`httpx.Client`.

    Example::

        from action0.client import Client
        from action0.client.backends.httpx import HttpxBackend
        from action0.req import Request

        with HttpxBackend() as backend:
            response = Client(backend).send(Request("https://example.com/"))
            print(response.status)
    """

    def __init__(
        self,
        client: "httpx.Client | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param client: the httpx client to send through — configure
                       connection limits, HTTP/2, proxies etc. there;
                       ``None`` creates (and owns) a default one, closed
                       again by :py:meth:`close`. The ``timeout`` and
                       ``follow_redirects`` arguments only apply to the
                       created client.
        :param timeout: the seconds httpx waits (for connect, read, write
                        and pool acquisition each); ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        self._client = (
            client
            if client is not None
            else httpx.Client(timeout=timeout, follow_redirects=follow_redirects)
        )
        self._owns_client = client is None

    def _send(self, request: Request) -> Response:
        """
        Send via the httpx client and convert the response back.

        :param request: the request to send
        :return: the response
        """
        arguments = _request_arguments(request, async_=False)
        return _convert_response(request, self._client.request(**arguments))

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize httpx's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        return _translate(error, request)

    def close(self) -> None:
        """
        Close the underlying httpx client — but only if this backend
        created it; a client that was passed in is left to its owner.
        """
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpxBackend":
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


class AsyncHttpxBackend(BaseAsyncBackend):
    """
    An asyncio backend driving an :py:class:`httpx.AsyncClient`.

    Example::

        import asyncio
        from action0.client import Client
        from action0.client.backends.httpx import AsyncHttpxBackend
        from action0.req import Request


        async def main() -> None:
            async with AsyncHttpxBackend() as backend:
                response = await Client(backend).send(Request("https://example.com/"))
                print(response.status)


        asyncio.run(main())
    """

    def __init__(
        self,
        client: "httpx.AsyncClient | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param client: the httpx client to send through — configure
                       connection limits, HTTP/2, proxies etc. there;
                       ``None`` creates (and owns) a default one, closed
                       again by :py:meth:`aclose`. The ``timeout`` and
                       ``follow_redirects`` arguments only apply to the
                       created client.
        :param timeout: the seconds httpx waits (for connect, read, write
                        and pool acquisition each); ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
        )
        self._owns_client = client is None

    async def _send(self, request: Request) -> Response:
        """
        Send via the httpx client and convert the response back.

        :param request: the request to send
        :return: (an awaitable of) the response
        """
        arguments = _request_arguments(request, async_=True)
        return _convert_response(request, await self._client.request(**arguments))

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize httpx's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        return _translate(error, request)

    async def aclose(self) -> None:
        """
        Close the underlying httpx client — but only if this backend
        created it; a client that was passed in is left to its owner.
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AsyncHttpxBackend":
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

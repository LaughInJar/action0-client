"""
The `aiohttp <https://docs.aiohttp.org/>`_ backend — an
:py:data:`~action0.client.backend.AsyncBackend`.

Requires the ``aiohttp`` extra: ``pip install "action0-client[aiohttp]"``.
"""

import asyncio
from typing import Any
from typing import Iterable

import aiohttp

from action0.req import Request
from action0.req import Response

from ..backend import BaseAsyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

DEFAULT_TIMEOUT = 30.0
"""The default total number of seconds from sending until the response
body finished arriving (aiohttp's ``ClientTimeout(total=...)``)."""


class AiohttpBackend(BaseAsyncBackend):
    """
    An asyncio backend driving an :py:class:`aiohttp.ClientSession`.

    Example::

        import asyncio
        from action0.client import Client
        from action0.client.backends.aiohttp import AiohttpBackend
        from action0.req import Request


        async def main() -> None:
            async with AiohttpBackend() as backend:
                response = await Client(backend).send(Request("https://example.com/"))
                print(response.status)


        asyncio.run(main())

    A session of its own is created lazily on the first send (an
    ``aiohttp.ClientSession`` must be created inside a running event
    loop), and closed again by :py:meth:`aclose`. Streaming request
    bodies work: a :py:class:`~action0.req.body.BodyProducer` body is
    handed to aiohttp as its async chunk iterator.
    """

    def __init__(
        self,
        session: "aiohttp.ClientSession | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param session: the session to send through — configure connectors,
                        proxies, cookie jars etc. there; ``None`` creates
                        (and owns) one lazily on the first send, closed
                        again by :py:meth:`aclose`. The ``timeout``
                        argument only applies to the created session.
        :param timeout: the total seconds from sending until the response
                        body finished arriving; ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._follow_redirects = follow_redirects

    def _live_session(self) -> aiohttp.ClientSession:
        """
        The session to send through, creating the owned one on first use.

        :return: the session
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    async def _send(self, request: Request) -> Response:
        """
        Send via the session and convert the response back.

        :param request: the request to send
        :return: (an awaitable of) the response
        """
        body: Any = None
        if request.body is not None:
            if isinstance(request.body, (bytes, str)):
                body = request.body_bytes()
            else:
                body = request.body.achunks()

        async with self._live_session().request(
            request.method,
            request.url.as_str(),
            # a list of pairs keeps multiple lines per field intact
            headers=request.headers.as_lines(),
            data=body,
            allow_redirects=self._follow_redirects,
        ) as answer:
            content = await answer.read()
            version = answer.version
            return Response(
                answer.status,
                # aiohttp's CIMultiDict keeps multiple lines per field
                headers=list(answer.headers.items()),
                body=content,
                reason=answer.reason,
                http_version=f"HTTP/{version.major}.{version.minor}"
                if version is not None
                else "HTTP/1.1",
                request=request,
            )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize aiohttp's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        # timeouts first: aiohttp's timeout errors subclass TimeoutError
        # (and some of them ClientError as well)
        if isinstance(error, asyncio.TimeoutError):
            return TimeoutError(str(error) or type(error).__name__, request=request)
        if isinstance(error, aiohttp.ClientError):
            return TransportError(str(error) or type(error).__name__, request=request)
        return error

    async def aclose(self) -> None:
        """
        Close the underlying session — but only if this backend created
        it; a session that was passed in is left to its owner.
        """
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "AiohttpBackend":
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

"""
The `aiohttp <https://docs.aiohttp.org/>`_ backend — an
:py:data:`~action0.client.backend.AsyncBackend`.

Requires the ``aiohttp`` extra: ``pip install "action0-client[aiohttp]"``.
"""

import asyncio
from typing import Any
from typing import Callable
from typing import Iterable

import aiohttp

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyTypes

from ..backend import BaseAsyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook
from ..options import BodyMode
from ._body import CHUNK_SIZE
from ._body import adeliver
from ._body import body_mode
from ._loops import LoopBound
from ._loops import PerLoop

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

    **Event loops.** An ``aiohttp.ClientSession`` is bound to the event
    loop it was created in. A backend created without a session
    therefore keeps one session *per event loop* — created on first use
    in a loop, closed when that loop shuts down — and can be shared
    freely: by code that calls ``asyncio.run()`` for every request, and
    by threads that each run their own loop. Pass ``session_factory=``
    to configure those sessions. A backend given a single ``session=``
    belongs to that session's loop; using it from another loop raises a
    :py:class:`RuntimeError` saying so.

    Streaming request bodies work: a
    :py:class:`~action0.req.body.BodyProducer` body is handed to aiohttp
    as its async chunk iterator.

    The response body is preloaded bytes by default. A request asks for a
    streamed or a spooled body with
    :py:attr:`RequestOptions.body <action0.client.options.RequestOptions.body>`
    (see :py:class:`~action0.client.options.BodyMode`); ``stream=True``
    makes streaming this backend's default.
    """

    def __init__(
        self,
        session: "aiohttp.ClientSession | None" = None,
        *,
        session_factory: "Callable[[], aiohttp.ClientSession] | None" = None,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        stream: bool = False,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param session: a single session to send through. It stays the
                        caller's: the backend never closes it. A session
                        is bound to the event loop it was created in, and
                        so is a backend given one — using it from another
                        loop raises a :py:class:`RuntimeError`.
        :param session_factory: creates the sessions this backend owns —
                                one per event loop, inside that loop on
                                first use, closed when the loop shuts
                                down. The way to configure connectors,
                                proxies, cookie jars etc. for a backend
                                that is shared between event loops.
                                Cannot be combined with ``session``.
        :param timeout: the total seconds from sending until the response
                        body finished arriving; ``None`` waits forever.
                        Applies to the default sessions only — not with
                        ``session`` or ``session_factory``.
                        NOTE: for a streamed body this budget spans the
                        body consumption too — for long-lived streams pass
                        a session with a tailored ``ClientTimeout`` (e.g.
                        ``sock_read`` instead of ``total``)
        :param follow_redirects: whether 3xx responses are followed
        :param stream: whether response bodies are streamed by default
                       (``send`` then returns at headers arrival); a
                       request's own
                       :py:attr:`~action0.client.options.RequestOptions.body`
                       option takes precedence
        :param hooks: the instrumentation hooks to run around every send
        :raises TypeError: if both ``session`` and ``session_factory`` are
                           given
        """
        super().__init__(hooks)
        if session is not None and session_factory is not None:
            raise TypeError("pass either session or session_factory, not both")
        self._follow_redirects = follow_redirects
        self._stream = stream
        self._session = session
        self._bound: "LoopBound | None" = None
        self._sessions: "PerLoop[aiohttp.ClientSession] | None" = None
        if session is not None:
            self._bound = LoopBound(
                f"The aiohttp.ClientSession passed to {type(self).__name__}",
                "pass session_factory=... instead of session=... (or neither)",
            )
        else:

            def default_session() -> aiohttp.ClientSession:
                return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

            self._sessions = PerLoop(session_factory or default_session, _close_session)

    async def current_session(self) -> aiohttp.ClientSession:
        """
        The session used for the running event loop: the one that was
        passed in, or this loop's own session — created now if the
        backend has not sent anything on this loop yet.

        :return: the session
        :raises RuntimeError: if a single session was passed in and this
                              is not the loop it is bound to
        """
        if self._sessions is not None:
            return await self._sessions.get()
        assert self._bound is not None and self._session is not None
        self._bound.check()
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

        mode = body_mode(request, self._stream)
        session = await self.current_session()
        answer = await session.request(
            request.method,
            request.url.as_str(),
            # a list of pairs keeps multiple lines per field intact
            headers=request.headers.as_lines(),
            data=body,
            allow_redirects=self._follow_redirects,
        )
        content: BodyTypes
        if mode is BodyMode.PRELOAD:
            try:
                content = await answer.read()
            finally:
                answer.release()
        else:

            async def release() -> None:
                # aiohttp's release is safe in every state: it keeps the
                # connection only if the body was read to its end
                answer.release()

            # a streamed body keeps the connection until it is consumed
            # or closed, a spooled one releases it before returning
            content = await adeliver(
                request,
                mode,
                answer.content.iter_chunked(CHUNK_SIZE),
                release,
                self.translate_error,
            )
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
        Close the session this backend created for the running event
        loop. Sessions of other loops are closed when their loop shuts
        down (or by calling this inside that loop); a session that was
        passed in is left to its owner.
        """
        if self._sessions is not None:
            await self._sessions.aclose()

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


async def _close_session(session: aiohttp.ClientSession) -> None:
    """
    Close an aiohttp session.

    :param session: the session to close
    """
    await session.close()

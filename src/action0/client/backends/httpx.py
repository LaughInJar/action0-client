"""
The `httpx <https://www.python-httpx.org/>`_ backends — a
:py:data:`~action0.client.backend.SyncBackend` and an
:py:data:`~action0.client.backend.AsyncBackend`, since httpx offers both
execution models over one API.

Requires the ``httpx`` extra: ``pip install "action0-client[httpx]"``.

The implementation is shared with the
:py:mod:`httpx2 backends <action0.client.backends.httpx2>`; the two
behave alike and take the same options, so switching between the
libraries means constructing the other backend.
"""

from typing import Any
from typing import ClassVar

import httpx

from ._httpx_family import DEFAULT_TIMEOUT
from ._httpx_family import AsyncHttpxFamilyBackend
from ._httpx_family import SyncHttpxFamilyBackend

__all__ = ["DEFAULT_TIMEOUT", "AsyncHttpxBackend", "HttpxBackend"]


class HttpxBackend(SyncHttpxFamilyBackend[httpx.Client]):
    """
    A synchronous backend driving an :py:class:`httpx.Client`.

    Example::

        from action0.client import Client
        from action0.client.backends.httpx import HttpxBackend
        from action0.req import Request

        with HttpxBackend() as backend:
            response = Client(backend).send(Request("https://example.com/"))
            print(response.status)

    The response body is preloaded bytes by default. A request asks for a
    streamed or a spooled body with
    :py:attr:`RequestOptions.body <action0.client.options.RequestOptions.body>`
    (see :py:class:`~action0.client.options.BodyMode`); ``stream=True``
    makes streaming this backend's default.
    """

    _lib: ClassVar[Any] = httpx


class AsyncHttpxBackend(AsyncHttpxFamilyBackend[httpx.AsyncClient]):
    """
    An asyncio backend driving :py:class:`httpx.AsyncClient`.

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

    **Event loops.** An ``httpx.AsyncClient`` is bound to the event loop
    it is first used on. A backend created without a client therefore
    keeps one client *per event loop* — created on first use in a loop,
    closed when that loop shuts down — and can be shared freely: by code
    that calls ``asyncio.run()`` for every request, and by threads that
    each run their own loop. Pass ``client_factory=`` to configure those
    clients. A backend given a single ``client=`` belongs to that
    client's loop; using it from another loop raises a
    :py:class:`RuntimeError` saying so.

    The response body is preloaded bytes by default. A request asks for a
    streamed or a spooled body with
    :py:attr:`RequestOptions.body <action0.client.options.RequestOptions.body>`
    (see :py:class:`~action0.client.options.BodyMode`); ``stream=True``
    makes streaming this backend's default.
    """

    _lib: ClassVar[Any] = httpx

"""
The `httpx2 <https://pypi.org/project/httpx2/>`_ backends — a
:py:data:`~action0.client.backend.SyncBackend` and an
:py:data:`~action0.client.backend.AsyncBackend`.

httpx2 is a maintained fork of httpx 0.28 with the same API under the
module name ``httpx2`` (and ``httpcore2`` as its transport). These
backends share their implementation with the
:py:mod:`httpx backends <action0.client.backends.httpx>`: they take the
same constructor options and behave the same, so an application switches
between the two libraries by constructing the other backend — nothing
else changes.

Requires the ``httpx2`` extra: ``pip install "action0-client[httpx2]"``.

Two things to know:

- **TLS trust.** httpx2 verifies server certificates against the
  operating system's trust store (through ``truststore``), where httpx
  uses the ``certifi`` bundle. These backends leave that as it is. A
  host with a private CA installed system-wide is therefore trusted by
  the httpx2 backends and not by the httpx ones — and the other way
  round for a CA that is only in certifi's bundle. To pin it either way,
  pass a client created with ``verify=`` (an ``ssl.SSLContext``).
- **Aliasing.** ``httpx2.alias_httpx()`` makes ``import httpx`` load
  httpx2. These backends work with and without it; with it, the httpx
  backends drive httpx2 as well, since that is what ``httpx`` then is.
"""

from typing import Any
from typing import ClassVar

import httpx2

from ._httpx_family import DEFAULT_TIMEOUT
from ._httpx_family import AsyncHttpxFamilyBackend
from ._httpx_family import SyncHttpxFamilyBackend

__all__ = ["DEFAULT_TIMEOUT", "AsyncHttpx2Backend", "Httpx2Backend"]


class Httpx2Backend(SyncHttpxFamilyBackend[httpx2.Client]):
    """
    A synchronous backend driving an ``httpx2.Client``.

    Example::

        from action0.client import Client
        from action0.client.backends.httpx2 import Httpx2Backend
        from action0.req import Request

        with Httpx2Backend() as backend:
            response = Client(backend).send(Request("https://example.com/"))
            print(response.status)

    Server certificates are verified against the operating system's
    trust store, not certifi's bundle — see the
    :py:mod:`module notes <action0.client.backends.httpx2>`.

    The response body is preloaded bytes by default. A request asks for a
    streamed or a spooled body with
    :py:attr:`RequestOptions.body <action0.client.options.RequestOptions.body>`
    (see :py:class:`~action0.client.options.BodyMode`); ``stream=True``
    makes streaming this backend's default.
    """

    _lib: ClassVar[Any] = httpx2


class AsyncHttpx2Backend(AsyncHttpxFamilyBackend[httpx2.AsyncClient]):
    """
    An asyncio backend driving ``httpx2.AsyncClient``.

    Example::

        import asyncio
        from action0.client import Client
        from action0.client.backends.httpx2 import AsyncHttpx2Backend
        from action0.req import Request


        async def main() -> None:
            async with AsyncHttpx2Backend() as backend:
                response = await Client(backend).send(Request("https://example.com/"))
                print(response.status)


        asyncio.run(main())

    **Event loops.** Like for httpx, an async client is bound to the
    event loop it is first used on. A backend created without a client
    keeps one client *per event loop* — created on first use in a loop,
    closed when that loop shuts down — and can be shared between
    ``asyncio.run()`` calls and between threads that each run their own
    loop. Pass ``client_factory=`` to configure those clients. A backend
    given a single ``client=`` belongs to that client's loop; using it
    from another loop raises a :py:class:`RuntimeError` saying so.

    Server certificates are verified against the operating system's
    trust store, not certifi's bundle — see the
    :py:mod:`module notes <action0.client.backends.httpx2>`.

    The response body is preloaded bytes by default. A request asks for a
    streamed or a spooled body with
    :py:attr:`RequestOptions.body <action0.client.options.RequestOptions.body>`
    (see :py:class:`~action0.client.options.BodyMode`); ``stream=True``
    makes streaming this backend's default.
    """

    _lib: ClassVar[Any] = httpx2

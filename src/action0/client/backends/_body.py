"""
The response body handling shared by the backends: which
:py:class:`~action0.client.options.BodyMode` a request gets, and how a
library's chunk iterator becomes a streamed or a spooled body.

Preloading is not here — every HTTP library does that best itself.
"""

from typing import AsyncIterable
from typing import AsyncIterator
from typing import Awaitable
from typing import Callable
from typing import Iterable
from typing import Iterator

from action0.req import Request
from action0.req.body import AsyncIterableBody
from action0.req.body import BodyTypes
from action0.req.body import IterableBody
from action0.req.body import SpooledBody

from ..options import BodyMode
from ..options import RequestOptions

CHUNK_SIZE = 65536
"""The chunk size the backends ask their library for when streaming or
spooling. For a spooled body on an async backend it is also the amount of
data per worker-thread hop."""

DEFAULT_SPOOL_MEMORY = 1048576
"""How many bytes of a spooled body stay in memory unless the request
says otherwise (:py:attr:`RequestOptions.spool_max_memory
<action0.client.options.RequestOptions.spool_max_memory>`)."""

Translate = Callable[[Exception, Request], BaseException]
"""A backend's ``translate_error`` method."""


def body_mode(request: Request, stream_default: bool) -> BodyMode:
    """
    The body mode for a request: what the request asks for, otherwise
    the backend's default.

    :param request: the request about to be sent
    :param stream_default: the backend's ``stream`` constructor argument
    :return: the mode to deliver the response body in
    """
    chosen = RequestOptions.of(request).body
    if chosen is not None:
        return chosen
    return BodyMode.STREAM if stream_default else BodyMode.PRELOAD


def spool_memory(request: Request) -> int:
    """
    How many bytes of the request's spooled response body stay in memory.

    :param request: the request whose response body is to be spooled
    :return: the threshold in bytes
    """
    max_memory = RequestOptions.of(request).spool_max_memory
    return DEFAULT_SPOOL_MEMORY if max_memory is None else max_memory


def new_spool(request: Request) -> SpooledBody:
    """
    An empty spool configured by the request's options.

    :param request: the request whose response body is to be spooled
    :return: the spool to write the body into
    """
    return SpooledBody(
        max_memory=spool_memory(request),
        dir=RequestOptions.of(request).spool_dir,
        chunk_size=CHUNK_SIZE,
    )


def deliver(
    request: Request,
    mode: BodyMode,
    chunks: Iterable[bytes],
    close: Callable[[], None],
    translate: Translate,
) -> BodyTypes:
    """
    Turn the chunk iterator of an open response into the body the
    request asked for (anything but preloaded bytes).

    - ``STREAM``: a single-use producer over the open connection. Errors
      raised while it is read are translated like errors raised by
      ``send``; ``close`` runs once the body is consumed, abandoned or
      closed.
    - ``SPOOL``: the body is read to its end into a spool right here and
      the connection is released before returning. An error on the way
      discards the spool and propagates (to be translated by the
      backend's ``send`` like any other).

    :param request: the request that was sent
    :param mode: ``BodyMode.STREAM`` or ``BodyMode.SPOOL``
    :param chunks: the library's iterator over the body, read lazily
    :param close: releases the response's connection; must be safe to
                  call whether or not the body was read to its end
    :param translate: the backend's ``translate_error``
    :return: the body for the :py:class:`~action0.req.response.Response`
    """
    if mode is BodyMode.STREAM:
        return IterableBody(_translated(chunks, request, translate), close=close)
    spool = new_spool(request)
    try:
        for chunk in chunks:
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    finally:
        close()
    return spool


async def adeliver(
    request: Request,
    mode: BodyMode,
    achunks: AsyncIterable[bytes],
    aclose: Callable[[], Awaitable[None]],
    translate: Translate,
) -> BodyTypes:
    """
    :py:func:`deliver` for async backends. A spooled body is written with
    :py:meth:`SpooledBody.awrite() <action0.req.body.SpooledBody.awrite>`:
    inline while it fits in memory, in a worker thread once it is in the
    temporary file — so the event loop is never blocked by file I/O.

    :param request: the request that was sent
    :param mode: ``BodyMode.STREAM`` or ``BodyMode.SPOOL``
    :param achunks: the library's async iterator over the body
    :param aclose: releases the response's connection; must be safe to
                   call whether or not the body was read to its end
    :param translate: the backend's ``translate_error``
    :return: the body for the :py:class:`~action0.req.response.Response`
    """
    if mode is BodyMode.STREAM:
        return AsyncIterableBody(_atranslated(achunks, request, translate), aclose=aclose)
    spool = new_spool(request)
    try:
        async for chunk in achunks:
            await spool.awrite(chunk)
    except BaseException:
        # synchronously on purpose: this also runs on cancellation, where
        # awaiting once more may not be possible
        spool.close()
        raise
    finally:
        await aclose()
    return spool


def _translated(
    chunks: Iterable[bytes], request: Request, translate: Translate
) -> Iterator[bytes]:
    """
    Pass the chunks through, translating what the library raises midway
    into the library-independent errors.

    :param chunks: the library's iterator over the body
    :param request: the request that was sent
    :param translate: the backend's ``translate_error``
    :return: the same chunks
    """
    try:
        yield from chunks
    except Exception as error:
        translated = translate(error, request)
        if translated is error:
            raise
        raise translated from error


async def _atranslated(
    achunks: AsyncIterable[bytes], request: Request, translate: Translate
) -> AsyncIterator[bytes]:
    """
    The async twin of :py:func:`_translated`.

    :param achunks: the library's async iterator over the body
    :param request: the request that was sent
    :param translate: the backend's ``translate_error``
    :return: the same chunks
    """
    try:
        async for chunk in achunks:
            yield chunk
    except Exception as error:
        translated = translate(error, request)
        if translated is error:
            raise
        raise translated from error

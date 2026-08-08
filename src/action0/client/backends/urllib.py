"""
The stdlib `urllib <https://docs.python.org/3/library/urllib.request.html>`_
backend — a :py:data:`~action0.client.backend.SyncBackend` without any
third-party dependency, so a bare ``pip install action0-client`` can
already talk HTTP.

For anything demanding (connection pooling, retries, cookies, proxies
beyond the environment defaults), prefer the requests or httpx backend.
"""

import http.client
import socket
import urllib.error
import urllib.request
from typing import Any
from typing import Iterable
from typing import Iterator

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyTypes
from action0.req.body import IterableBody

from ..backend import BaseSyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

DEFAULT_TIMEOUT = 30.0
"""The default number of seconds to wait for the connection and each
socket operation."""

_CHUNK_SIZE = 65536
"""The chunk size for streamed response bodies."""

# http.client reports the HTTP version of the response as an int
_HTTP_VERSIONS = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1"}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that doesn't: 3xx responses are returned as-is."""

    def redirect_request(self, *args: object, **kwargs: object) -> "urllib.request.Request | None":
        """
        :param args: the redirect details (ignored)
        :param kwargs: the redirect details (ignored)
        :return: always ``None`` — urllib then raises the 3xx as an
                 ``HTTPError``, which the backend converts to a Response
        """
        return None


class UrllibBackend(BaseSyncBackend):
    """
    A synchronous backend driving a stdlib
    :py:class:`urllib.request.OpenerDirector` — zero dependencies.

    Example::

        from action0.client import Client
        from action0.client.backends.urllib import UrllibBackend
        from action0.req import Request

        response = Client(UrllibBackend()).send(Request("https://example.com/"))
        print(response.status)

    Notes on fidelity:

    - Non-2xx statuses are returned as responses (urllib's ``HTTPError``
      is converted back), matching the other backends — status policy
      belongs to the operation layer.
    - Streaming request bodies work: a
      :py:class:`~action0.req.body.BodyProducer` body is handed to urllib
      as a chunk iterator (sent with chunked transfer encoding).
    - Streaming *response* bodies are opt-in: with ``stream=True`` the
      response body is an :py:class:`~action0.req.body.IterableBody`
      producing the bytes as they arrive instead of preloaded bytes; the
      connection is held until the body is consumed (or the producer is
      garbage-collected).
    - Multiple response header lines with the same name are preserved.
    - Multiple *request* header lines are merged into one comma-separated
      line, and urllib normalizes request header casing (``X-Api-key``
      style) — semantically equivalent per RFC 9110.
    """

    def __init__(
        self,
        opener: "urllib.request.OpenerDirector | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        stream: bool = False,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param opener: the opener to send through — configure proxy or
                       auth handlers there; ``None`` builds a default one.
                       The ``follow_redirects`` argument only applies to
                       the built opener.
        :param timeout: the seconds to wait for the connection and each
                        socket operation; ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param stream: whether response bodies arrive as streaming
                       producers instead of preloaded bytes (``send``
                       then returns at headers arrival)
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        if opener is None:
            handlers = () if follow_redirects else (_NoRedirects(),)
            opener = urllib.request.build_opener(*handlers)
        self._opener = opener
        self._timeout = timeout
        self._stream = stream

    def _send(self, request: Request) -> Response:
        """
        Send via the opener and convert the response back — including
        ``HTTPError``, which urllib raises for every non-2xx status but
        which *is* the response.

        :param request: the request to send
        :return: the response
        """
        raw = urllib.request.Request(
            request.url.as_str(),
            method=request.method,
            headers=_merged_headers(request),
            data=_request_data(request),
        )
        try:
            answer = self._opener.open(raw, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            answer = error
        try:
            status = answer.status
            assert status is not None  # always set for HTTP responses
            body: BodyTypes = _streamed_body(answer) if self._stream else answer.read()
            return Response(
                status,
                # Message.items() keeps multiple lines per field intact
                headers=answer.headers.items(),
                body=body,
                reason=answer.reason if isinstance(answer.reason, str) else None,
                http_version=_HTTP_VERSIONS.get(getattr(answer, "version", 11), "HTTP/1.1"),
                request=request,
            )
        finally:
            # a preloaded response is done with its connection here; a
            # streamed one keeps it until the body producer finishes
            if not self._stream:
                answer.close()

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize urllib's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        # socket.timeout is the built-in TimeoutError; urllib raises it
        # bare or wrapped in a URLError, depending on where it hits
        if isinstance(error, socket.timeout):
            return TimeoutError(str(error) or "timed out", request=request)
        if isinstance(error, urllib.error.URLError):
            if isinstance(error.reason, socket.timeout):
                return TimeoutError(str(error.reason) or "timed out", request=request)
            return TransportError(str(error.reason), request=request)
        if isinstance(error, (http.client.HTTPException, ConnectionError)):
            return TransportError(str(error) or type(error).__name__, request=request)
        return error

    def close(self) -> None:
        """
        Close the opener (and with it any handler-held connections).
        """
        self._opener.close()

    def __enter__(self) -> "UrllibBackend":
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


def _streamed_body(answer: Any) -> IterableBody:
    """
    The response body as a streaming producer: chunks are read from the
    open connection on demand, and the connection is closed once the body
    is consumed (or the producer is garbage-collected).

    :param answer: the open urllib response (an ``addinfourl`` or an
                   ``HTTPError`` — both read and close alike, and neither
                   is precisely typed in typeshed, hence ``Any``)
    :return: the body producer
    """

    def chunks() -> Iterator[bytes]:
        try:
            while chunk := answer.read(_CHUNK_SIZE):
                yield chunk
        finally:
            answer.close()

    return IterableBody(chunks())


def _merged_headers(request: Request) -> dict[str, str]:
    """
    The request headers as the mapping urllib wants: multiple lines of
    one field are merged into a single comma-separated value (RFC 9110
    list syntax).

    :param request: the request whose headers to convert
    :return: the headers as a plain dictionary
    """
    return {name: ", ".join(values) for name, values in request.headers.as_dict().items()}


def _request_data(request: Request) -> "bytes | Iterator[bytes] | None":
    """
    The request body in the form urllib sends most faithfully: in-memory
    bodies as bytes, streaming producers as their chunk iterator
    (http.client sends iterables without a length chunked).

    :param request: the request whose body to convert
    :return: the body for ``urllib.request.Request(data=...)``
    """
    if request.body is None:
        return None
    if isinstance(request.body, (bytes, str)):
        return request.body_bytes()
    return request.body.chunks()

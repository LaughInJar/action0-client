"""
The `urllib3 <https://urllib3.readthedocs.io/>`_ backend — a
:py:data:`~action0.client.backend.SyncBackend` for projects that use
urllib3's pools directly, without requests on top.

Requires the ``urllib3`` extra: ``pip install "action0-client[urllib3]"``.
"""

from typing import Any
from typing import Iterable
from typing import Iterator

import urllib3

from action0.req import Request
from action0.req import Response

from ..backend import BaseSyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

DEFAULT_TIMEOUT = 30.0
"""The default total number of seconds to wait for connect + read."""

# urllib3 reports the HTTP version of the response as an int
_HTTP_VERSIONS = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}


class Urllib3Backend(BaseSyncBackend):
    """
    A synchronous backend driving a :py:class:`urllib3.PoolManager`.

    Example::

        from action0.client import Client
        from action0.client.backends.urllib3 import Urllib3Backend
        from action0.req import Request

        with Urllib3Backend() as backend:
            response = Client(backend).send(Request("https://example.com/"))
            print(response.status)

    Notes on fidelity:

    - Non-2xx statuses are returned as responses (urllib3 never raises for
      them) — status policy belongs to the operation layer.
    - Streaming request bodies work: a
      :py:class:`~action0.req.body.BodyProducer` body is handed to urllib3
      as a chunk iterator.
    - Multiple response header lines with the same name are preserved
      (urllib3's header dict keeps them apart).
    - Multiple *request* header lines are merged into one comma-separated
      line, because urllib3 only accepts a mapping.
    """

    def __init__(
        self,
        pool: "urllib3.PoolManager | None" = None,
        *,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        retries: "urllib3.Retry | bool | int | None" = None,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param pool: the pool manager to send through — configure
                     connection limits, TLS, proxies etc. there; ``None``
                     creates (and owns) a default one, emptied again by
                     :py:meth:`close`
        :param timeout: the total seconds to wait for connect + read;
                        ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
        :param retries: urllib3's retry policy, passed through per request
                        (a :py:class:`urllib3.util.Retry`, a count, or
                        ``False`` to raise transport errors immediately);
                        ``None`` uses urllib3's default
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        self._pool = pool if pool is not None else urllib3.PoolManager()
        self._owns_pool = pool is None
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._retries = retries

    def _send(self, request: Request) -> Response:
        """
        Send via the pool manager and convert the response back.

        :param request: the request to send
        :return: the response
        """
        arguments: dict[str, Any] = {
            "body": _request_data(request),
            "headers": _merged_headers(request),
            "timeout": urllib3.Timeout(total=self._timeout),
            "redirect": self._follow_redirects,
            "preload_content": True,
        }
        if self._retries is not None:
            arguments["retries"] = self._retries
        answer = self._pool.urlopen(request.method, request.url.as_str(), **arguments)
        return Response(
            answer.status,
            # urllib3's HTTPHeaderDict keeps multiple lines per field
            headers=list(answer.headers.items()),
            body=answer.data,
            reason=answer.reason,
            http_version=_HTTP_VERSIONS.get(answer.version, "HTTP/1.1"),
            request=request,
        )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize urllib3's exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        # a timeout may arrive bare or wrapped in a MaxRetryError once the
        # retry budget is exhausted — surface both as the timeout family
        if isinstance(error, urllib3.exceptions.TimeoutError):
            return TimeoutError(str(error) or type(error).__name__, request=request)
        if isinstance(error, urllib3.exceptions.MaxRetryError) and isinstance(
            error.reason, urllib3.exceptions.TimeoutError
        ):
            return TimeoutError(str(error.reason), request=request)
        if isinstance(error, urllib3.exceptions.HTTPError):
            return TransportError(str(error) or type(error).__name__, request=request)
        return error

    def close(self) -> None:
        """
        Empty the pool manager (closing its kept-alive connections) — but
        only if this backend created it; a pool that was passed in is left
        to its owner.
        """
        if self._owns_pool:
            self._pool.clear()

    def __enter__(self) -> "Urllib3Backend":
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


def _merged_headers(request: Request) -> dict[str, str]:
    """
    The request headers as the mapping urllib3 wants: multiple lines of
    one field are merged into a single comma-separated value (RFC 9110
    list syntax).

    :param request: the request whose headers to convert
    :return: the headers as a plain dictionary
    """
    return {name: ", ".join(values) for name, values in request.headers.as_dict().items()}


def _request_data(request: Request) -> "bytes | Iterator[bytes] | None":
    """
    The request body in the form urllib3 sends most faithfully: in-memory
    bodies as bytes, streaming producers as their chunk iterator (sent
    chunked).

    :param request: the request whose body to convert
    :return: the body for ``urlopen(body=...)``
    """
    if request.body is None:
        return None
    if isinstance(request.body, (bytes, str)):
        return request.body_bytes()
    return request.body.chunks()

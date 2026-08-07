"""
The `requests <https://requests.readthedocs.io/>`_ backend — a
:py:data:`~action0.client.backend.SyncBackend`.

Requires the ``requests`` extra: ``pip install "action0-client[requests]"``.
"""

from typing import Iterable
from typing import Iterator

import requests

from action0.req import Request
from action0.req import Response

from ..backend import BaseSyncBackend
from ..errors import TimeoutError
from ..errors import TransportError
from ..hooks import Hook

DEFAULT_TIMEOUT = 30.0
"""The default total number of seconds to wait for connect + read."""

# requests reports the HTTP version of the response as a urllib3 int
_HTTP_VERSIONS = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}


class RequestsBackend(BaseSyncBackend):
    """
    A synchronous backend driving a :py:class:`requests.Session`.

    Example::

        from action0.client import Client
        from action0.client.backends.requests import RequestsBackend
        from action0.req import Request

        with RequestsBackend() as backend:
            response = Client(backend).send(Request("https://example.com/"))
            print(response.status)

    Notes on fidelity:

    - Streaming request bodies work: a
      :py:class:`~action0.req.body.BodyProducer` body is handed to requests
      as a chunk iterator (sent with chunked transfer encoding).
    - Multiple response header lines with the same name are preserved when
      urllib3's raw headers are available; requests itself would merge them.
    - Multiple *request* header lines with the same name are merged into
      one comma-separated line, because requests only accepts a mapping.
      (``Cookie`` is the only field where this could matter in practice.)
    """

    def __init__(
        self,
        session: "requests.Session | None" = None,
        *,
        timeout: "float | tuple[float, float] | None" = DEFAULT_TIMEOUT,
        follow_redirects: bool = True,
        hooks: Iterable[Hook] = (),
    ) -> None:
        """
        :param session: the session to send through — configure retries,
                        proxies, certificates etc. there; ``None`` creates
                        (and owns) a fresh one, closed again by
                        :py:meth:`close`
        :param timeout: the seconds to wait, either one number for connect
                        and read together or a (connect, read) tuple;
                        ``None`` waits forever
        :param follow_redirects: whether 3xx responses are followed
                                 (transparently, like a browser)
        :param hooks: the instrumentation hooks to run around every send
        """
        super().__init__(hooks)
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._timeout = timeout
        self._follow_redirects = follow_redirects

    def _send(self, request: Request) -> Response:
        """
        Send via the session: build a ``PreparedRequest`` from the given
        request and convert the ``requests.Response`` back.

        :param request: the request to send
        :return: the response
        """
        prepared = self._session.prepare_request(
            requests.Request(
                method=request.method,
                url=request.url.as_str(),
                headers=_merged_headers(request),
                data=_request_data(request),
            )
        )
        answer = self._session.send(
            prepared,
            timeout=self._timeout,
            allow_redirects=self._follow_redirects,
        )
        return Response(
            answer.status_code,
            headers=_response_headers(answer),
            body=answer.content,
            reason=answer.reason,
            http_version=_HTTP_VERSIONS.get(answer.raw.version, "HTTP/1.1")
            if answer.raw is not None
            else "HTTP/1.1",
            request=request,
        )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Normalize requests' exceptions into the
        :py:class:`~action0.client.errors.TransportError` family.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the normalized exception (unknown types pass through)
        """
        if isinstance(error, requests.Timeout):
            return TimeoutError(str(error), request=request)
        if isinstance(error, requests.RequestException):
            return TransportError(str(error), request=request)
        return error

    def close(self) -> None:
        """
        Close the underlying session — but only if this backend created it;
        a session that was passed in is left to its owner.
        """
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "RequestsBackend":
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
    The request headers as the mapping requests wants: multiple lines of
    one field are merged into a single comma-separated value (RFC 9110
    list syntax).

    :param request: the request whose headers to convert
    :return: the headers as a plain dictionary
    """
    return {name: ", ".join(values) for name, values in request.headers.as_dict().items()}


def _request_data(request: Request) -> "bytes | Iterator[bytes] | None":
    """
    The request body in the form requests sends most faithfully: in-memory
    bodies as bytes, streaming producers as their chunk iterator (sent
    chunked).

    :param request: the request whose body to convert
    :return: the body for ``requests.Request(data=...)``
    """
    if request.body is None:
        return None
    if isinstance(request.body, (bytes, str)):
        return request.body_bytes()
    return request.body.chunks()


def _response_headers(answer: requests.Response) -> list[tuple[str, str]]:
    """
    The response header lines, preserving repeated fields when urllib3
    exposes the raw lines (requests' own view merges repeated fields).

    :param answer: the requests response
    :return: the header lines as name/value tuples
    """
    raw = getattr(answer, "raw", None)
    raw_headers = getattr(raw, "headers", None)
    if raw_headers is not None:
        return list(raw_headers.items())
    return list(answer.headers.items())

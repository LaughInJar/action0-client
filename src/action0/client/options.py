"""
Per-request options: choices made for one request (or for all requests
of one operation) rather than for a whole backend or policy.

They travel with the request in its ``meta`` dict — never on the wire —
as one typed, immutable :py:class:`RequestOptions` object under one
namespaced key. Backends and wrappers read them with
:py:meth:`RequestOptions.of`; application code sets them with
:py:meth:`RequestOptions.apply`, or declaratively on an operation class
via :py:attr:`Operation.request_options
<action0.client.operation.Operation.request_options>`.

Example::

    >>> from action0.req import Request
    >>>
    >>> request = Request("https://api.example.com/translate", "POST", body="hello")
    >>> RequestOptions.of(request).retry_safe is None  # nothing set: the policy decides
    True
    >>> RequestOptions(retry_safe=True).apply(request)
    Request(POST https://api.example.com/translate)
    >>> RequestOptions.of(request)
    RequestOptions(retry_safe=True)
"""

import dataclasses
import os
from dataclasses import dataclass
from enum import Enum

from action0.req import Request

_META_KEY = "action0-client"
"""The ``request.meta`` key the options of a request are stored under."""


class BodyMode(Enum):
    """
    How a backend hands over the body of a response — chosen per request
    with :py:attr:`RequestOptions.body`.
    """

    PRELOAD = "preload"
    """Read the whole body into memory before ``send`` returns; the
    response body is ``bytes``. The connection is released by then. The
    default of every backend."""

    STREAM = "stream"
    """Return from ``send`` as soon as the status and the headers are
    there; the response body is a single-use producer
    (:py:class:`~action0.req.body.IterableBody`, or
    :py:class:`~action0.req.body.AsyncIterableBody` on async backends)
    reading from the open connection on demand. The connection is held
    until the body has been consumed or the response is closed."""

    SPOOL = "spool"
    """Write the body into a :py:class:`~action0.req.body.SpooledBody`
    while it arrives — in memory up to a threshold, in a temporary file
    beyond it — and release the connection before ``send`` returns. The
    body is re-readable and offers a seekable file object; closing the
    response removes the temporary file."""


@dataclass(frozen=True, kw_only=True)
class RequestOptions:
    """
    The options of one request. Every field defaults to ``None``, meaning
    "not decided here": the backend's or the policy's own setting
    applies.

    Instances are immutable, so one can be shared by any number of
    requests — an operation class holds a single instance for all the
    requests it creates.
    """

    body: "BodyMode | None" = None
    """How the response body is handed over, see :py:class:`BodyMode`.
    ``None`` uses the backend's default: preloaded bytes, or a stream for
    a backend constructed with ``stream=True``."""

    spool_max_memory: "int | None" = None
    """For :py:attr:`BodyMode.SPOOL`: how many bytes of the body are kept
    in memory before it is moved to a temporary file. ``0`` writes to the
    file from the first byte on; ``None`` means 1 MiB."""

    spool_dir: "str | os.PathLike[str] | None" = None
    """For :py:attr:`BodyMode.SPOOL`: the directory for the temporary
    file; ``None`` uses the system's (see :py:func:`tempfile.gettempdir`)."""

    retry_safe: "bool | None" = None
    """Whether this request may be sent again by a retrying backend.
    ``True`` allows retries although the method is not in the policy's
    :py:attr:`~action0.client.retry.RetryPolicy.methods` — for a ``POST``
    that only reads, like a search or translation endpoint. ``False``
    forbids them for a request whose method would be retried. ``None``
    leaves the decision to the policy's method gate."""

    cacheable: "bool | None" = None
    """Whether a caching backend may answer this request from its cache
    and store the response. ``True`` makes a request cacheable although
    its method is not in the policy's
    :py:attr:`~action0.client.caching.CachePolicy.methods` — for a
    ``POST`` that only reads; its body then becomes part of the cache
    key. ``False`` keeps a request out of the cache. ``None`` leaves the
    decision to the policy's method gate."""

    cache_key: "str | None" = None
    """The cache key for this request, used as given instead of the one
    the policy computes from method, URL, vary headers and body. For
    requests the computed key does not fit — because something volatile
    is part of the URL or body, or because the body is a stream that
    cannot be hashed. Whoever sets it is responsible for it telling
    apart everything that changes the response."""

    cache_ttl: "float | None" = None
    """How many seconds the response to this request may be served from
    the cache; ``None`` uses the policy's
    :py:attr:`~action0.client.caching.CachePolicy.ttl`."""

    def __post_init__(self) -> None:
        """
        :raises ValueError: if ``spool_max_memory`` is negative
        """
        if self.spool_max_memory is not None and self.spool_max_memory < 0:
            raise ValueError(f"spool_max_memory must not be negative, got {self.spool_max_memory}")

    def apply(self, request: Request) -> Request:
        """
        Set these options on a request. Options already on the request
        are merged field by field: what is set here (not ``None``) wins,
        everything else is kept — so options applied later refine those
        applied earlier.

        Example::

            >>> request = Request("https://api.example.com/search", "POST")
            >>> _ = RequestOptions(retry_safe=True).apply(request)
            >>> _ = RequestOptions().apply(request)  # sets nothing, changes nothing
            >>> RequestOptions.of(request).retry_safe
            True

        :param request: the request to set the options on; modified in
                        place
        :return: the same request, for chaining
        """
        current = request.meta.get(_META_KEY)
        if isinstance(current, RequestOptions):
            decided = {
                field.name: value
                for field in dataclasses.fields(self)
                if (value := getattr(self, field.name)) is not None
            }
            request.meta[_META_KEY] = dataclasses.replace(current, **decided)
        else:
            request.meta[_META_KEY] = self
        return request

    @staticmethod
    def of(request: Request) -> "RequestOptions":
        """
        The options of a request.

        :param request: the request to look at
        :return: the options set on it; an all-``None`` instance if there
                 are none
        """
        options = request.meta.get(_META_KEY)
        return options if isinstance(options, RequestOptions) else _NO_OPTIONS

    def __repr__(self) -> str:
        """
        :return: the options that are set, e.g.
                 ``RequestOptions(retry_safe=True)`` — undecided fields
                 are left out
        """
        decided = ", ".join(
            f"{field.name}={getattr(self, field.name)!r}"
            for field in dataclasses.fields(self)
            if getattr(self, field.name) is not None
        )
        return f"{self.__class__.__name__}({decided})"


_NO_OPTIONS = RequestOptions()
"""What :py:meth:`RequestOptions.of` answers for a request without options —
one shared instance, so the lookup allocates nothing."""

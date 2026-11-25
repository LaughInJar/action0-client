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
from dataclasses import dataclass

from action0.req import Request

_META_KEY = "action0-client"
"""The ``request.meta`` key the options of a request are stored under."""


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

    retry_safe: "bool | None" = None
    """Whether this request may be sent again by a retrying backend.
    ``True`` allows retries although the method is not in the policy's
    :py:attr:`~action0.client.retry.RetryPolicy.methods` — for a ``POST``
    that only reads, like a search or translation endpoint. ``False``
    forbids them for a request whose method would be retried. ``None``
    leaves the decision to the policy's method gate."""

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

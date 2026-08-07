"""The exception hierarchy shared by all backends and API clients."""

import builtins

from action0.req import Request
from action0.req import Response


class ClientError(Exception):
    """
    The base class of everything raised by action0-client itself.

    Catching this catches both transport failures (:py:class:`TransportError`)
    and API-level failures (:py:class:`APIError`), but not bugs like
    :py:class:`TypeError`.
    """


class TransportError(ClientError):
    """
    The request never produced an HTTP response: DNS failure, connection
    refused, TLS error, connection lost mid-response, and so on.

    Backends translate the exceptions of their HTTP library into this type
    (or a subclass), so callers only ever need to handle one exception
    family no matter which backend is plugged in. The original library
    exception is preserved as ``__cause__``.
    """

    def __init__(self, message: str, *, request: Request | None = None) -> None:
        """
        :param message: a human-readable description of the failure
        :param request: the request that failed, if known
        """
        super().__init__(message)
        self.request = request
        """The request that failed, ``None`` if unknown."""


class TimeoutError(TransportError, builtins.TimeoutError):
    """
    The request timed out — a :py:class:`TransportError` that is also a
    :py:class:`TimeoutError` (the built-in), so both ``except TransportError``
    and a plain ``except TimeoutError`` catch it.
    """


class APIError(ClientError):
    """
    An HTTP response arrived but the API interaction failed: an unexpected
    status code, an empty or malformed body, a payload that doesn't match
    the expected schema, ...

    Raised by the response handling of :py:class:`~action0.client.operation.Operation`
    (and meant to be subclassed for API-specific error types). The offending
    :py:class:`~action0.req.response.Response` stays available on the
    exception for inspection.
    """

    def __init__(
        self,
        message: str,
        *,
        request: Request | None = None,
        response: Response | None = None,
    ) -> None:
        """
        :param message: a human-readable description of the failure
        :param request: the request that was sent, if known
        :param response: the response that could not be handled, if any
        """
        super().__init__(message)
        self.request = request
        """The request that was sent, ``None`` if unknown."""
        self.response = response
        """The response that could not be handled, ``None`` if there is none."""

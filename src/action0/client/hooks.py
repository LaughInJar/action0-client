"""Instrumentation hooks observing (and adjusting) the requests a backend sends."""

import logging

from action0.req import Request
from action0.req import Response


class Hook:
    """
    The instrumentation interface of the backend base classes: every backend
    built on :py:class:`~action0.client.backend.BaseSyncBackend`,
    :py:class:`~action0.client.backend.BaseAsyncBackend` or
    :py:class:`~action0.client.backend.BaseDeferredBackend` calls its hooks
    around every send — for logging, metrics, tracing, request decoration, …

    All three methods are no-ops here; subclass and override what you need.
    The methods are plain synchronous calls in every execution model (they
    run around the I/O, never inside it), so one hook implementation works
    with sync, async and Twisted backends alike.

    Example — a metrics hook counting responses by status::

        class StatusMetricsHook(Hook):
            def __init__(self) -> None:
                self.counts: dict[int, int] = {}

            def on_response(self, request, response, elapsed):
                self.counts[response.status] = self.counts.get(response.status, 0) + 1
                return None
    """

    def on_request(self, request: Request) -> Request | None:
        """
        Called before the request is sent.

        :param request: the request about to be sent
        :return: a replacement request, or ``None`` to send the given one
                 (mutating the given request also works — it is the one
                 that will be sent)
        """
        return None

    def on_response(self, request: Request, response: Response, elapsed: float) -> Response | None:
        """
        Called after a response arrived, before it is handed to the caller.

        :param request: the request that was sent
        :param response: the response that arrived
        :param elapsed: the seconds between sending and the response's arrival
        :return: a replacement response, or ``None`` to keep the given one
        """
        return None

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        """
        Called when sending failed — after the backend translated the error
        (see :py:meth:`~action0.client.backend.BaseSyncBackend.translate_error`),
        right before it is raised. Purely observational: hooks cannot swallow
        or replace errors.

        :param request: the request that was sent
        :param error: the (translated) error about to be raised
        :param elapsed: the seconds between sending and the failure
        """
        return None


class LoggingHook(Hook):
    """
    A ready-made :py:class:`Hook` that logs every request, response and
    error. Requests and responses are logged via their ``repr()``, which
    redacts secret header values and passwords — safe for production logs.

    Example::

        >>> import logging, sys
        >>> logger = logging.getLogger("docs.logging-hook")
        >>> logger.propagate = False
        >>> logger.setLevel(logging.DEBUG)
        >>> logger.addHandler(logging.StreamHandler(sys.stdout))
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> backend = StubBackend(Response(200), hooks=[LoggingHook(logger)])
        >>> response = backend.send(Request("https://example.com/health"))
        -> Request(GET https://example.com/health)
        <- Response(200 OK) for Request(GET https://example.com/health) in 0ms
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        level: int = logging.DEBUG,
        error_level: int = logging.WARNING,
    ) -> None:
        """
        :param logger: the logger to log to; defaults to the logger named
                       like this module (``action0.client.hooks``)
        :param level: the level for request and response lines
        :param error_level: the level for error lines
        """
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.level = level
        self.error_level = error_level

    def on_request(self, request: Request) -> Request | None:
        """
        Log the request (redacted via ``repr()``).

        :param request: the request about to be sent
        :return: always ``None`` — the request is only observed
        """
        self.logger.log(self.level, "-> %r", request)
        return None

    def on_response(self, request: Request, response: Response, elapsed: float) -> Response | None:
        """
        Log the response with its round-trip time (redacted via ``repr()``).

        :param request: the request that was sent
        :param response: the response that arrived
        :param elapsed: the seconds between sending and the response's arrival
        :return: always ``None`` — the response is only observed
        """
        self.logger.log(self.level, "<- %r for %r in %.0fms", response, request, elapsed * 1000)
        return None

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        """
        Log the error with the request that caused it.

        :param request: the request that was sent
        :param error: the (translated) error about to be raised
        :param elapsed: the seconds between sending and the failure
        """
        self.logger.log(
            self.error_level, "!! %r for %r after %.0fms", error, request, elapsed * 1000
        )

"""
Backend-agnostic retries: wrap any backend in the retrying variant of its
execution model and failed sends are repeated with exponential backoff.

Retrying is a *wrapper*, not a :py:class:`~action0.client.hooks.Hook`:
hooks observe a send, retrying has to perform new ones. The wrappers
preserve the wrapped backend's execution model — and with it the static
types (``Client``/``APIClient`` treat a ``RetryingSyncBackend`` exactly
like any other sync backend) — and the wrapped backend's hooks run on
*every* attempt, so logs and metrics see the retries.

What counts as retryable is the :py:class:`RetryPolicy`'s call: by
default, transport errors and typical transient statuses (408, 429, 5xx
gateway family), for idempotent methods only. When the attempts are
exhausted, the last response is returned (or the last error raised)
as-is — the policy never invents failures.

The waits apply "full jitter" by default — each one is a uniformly
random fraction of the exponential delay, so a burst of failing clients
does not retry in lockstep — and honor a ``Retry-After`` response header
(both the seconds and the HTTP-date form), capped at the policy's
:py:attr:`~RetryPolicy.max_backoff`.
"""

import datetime
import email.utils
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import TypeVar
from typing import cast

from action0.req import Header
from action0.req import Request
from action0.req import Response

from .backend import Backend
from .errors import TransportError

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees these
    from twisted.internet.defer import Deferred
    from twisted.python.failure import Failure

T = TypeVar("T")
S = TypeVar("S")

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
"""The HTTP methods that are safe to repeat per :rfc:`9110` — the default
method gate of :py:class:`RetryPolicy`."""


def _retry_after_seconds(response: Response) -> "float | None":
    """
    The wait a ``Retry-After`` response header asks for, in seconds.

    Both :rfc:`9110` forms are understood: a non-negative number of
    seconds, and an HTTP-date (which is turned into a delay against the
    current time). A date in the past yields ``0.0``.

    :param response: the response that may carry the header
    :return: the requested wait, or ``None`` if the header is missing or
             unparseable
    """
    value = response.headers.get(Header.RETRY_AFTER)
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except ValueError:
            return None
        if when.tzinfo is None:
            # parsedate_to_datetime returns a naive datetime for "-0000"
            when = when.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        seconds = (when - now).total_seconds()
    return max(0.0, seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """
    When and how to retry — immutable, shared freely between backends.

    Example: five attempts, snappier backoff, POST included::

        RetryPolicy(attempts=5, backoff=0.1, methods=None)
    """

    attempts: int = 3
    """The total number of tries, including the first one."""

    backoff: float = 0.5
    """The seconds to wait before the second attempt; subsequent waits
    grow by :py:attr:`multiplier`."""

    multiplier: float = 2.0
    """The exponential backoff factor."""

    max_backoff: float = 30.0
    """The ceiling for a single wait, in seconds."""

    retry_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    """The response statuses considered transient."""

    retry_errors: tuple[type[BaseException], ...] = (TransportError,)
    """The exception types considered transient. Backends translate their
    library's network failures into
    :py:class:`~action0.client.errors.TransportError`, so the default
    covers connection failures and timeouts of every backend."""

    methods: "frozenset[str] | None" = IDEMPOTENT_METHODS
    """The methods that may be retried at all; ``None`` allows every
    method (only do that for APIs whose non-idempotent endpoints tolerate
    replays)."""

    jitter: bool = True
    """Whether to apply "full jitter": each wait becomes a uniformly
    random duration between zero and the exponential delay, so many
    clients failing together do not retry in lockstep. ``False`` waits
    the exact exponential delays."""

    respect_retry_after: bool = True
    """Whether a ``Retry-After`` response header overrides the computed
    backoff (jitter included) — the server knows best when it is worth
    coming back. Its value is still capped at :py:attr:`max_backoff`."""

    rng: Callable[[], float] = random.random
    """The random source for the jitter, returning floats in ``[0, 1)``
    (injectable for deterministic tests)."""

    def delay_for(self, attempt: int, response: "Response | None" = None) -> float:
        """
        The seconds to wait after the given (1-based) attempt failed.

        A parseable ``Retry-After`` header on the response wins over the
        computed backoff (if :py:attr:`respect_retry_after`); otherwise
        the delay is exponential, jittered per :py:attr:`jitter`. Both
        are capped at :py:attr:`max_backoff`.

        :param attempt: the attempt that just failed
        :param response: the response that triggered the retry, if the
                         attempt produced one
        :return: the wait in seconds
        """
        if response is not None and self.respect_retry_after:
            hinted = _retry_after_seconds(response)
            if hinted is not None:
                return min(self.max_backoff, hinted)
        delay = min(self.max_backoff, self.backoff * self.multiplier ** (attempt - 1))
        if self.jitter:
            delay *= self.rng()
        return delay

    def applies_to(self, request: Request) -> bool:
        """
        Whether the request's method may be retried at all.

        :param request: the request being sent
        :return: whether retrying is allowed for this request
        """
        return self.methods is None or request.method in self.methods

    def should_retry_response(self, request: Request, response: Response, attempt: int) -> bool:
        """
        Whether a received response should be thrown away and retried.

        :param request: the request that was sent
        :param response: the response that arrived
        :param attempt: the (1-based) attempt that produced it
        :return: whether to retry
        """
        return (
            attempt < self.attempts
            and self.applies_to(request)
            and response.status in self.retry_statuses
        )

    def should_retry_error(self, request: Request, error: BaseException, attempt: int) -> bool:
        """
        Whether a failed send should be retried.

        :param request: the request that was sent
        :param error: the (already translated) error it failed with
        :param attempt: the (1-based) attempt that failed
        :return: whether to retry
        """
        return (
            attempt < self.attempts
            and self.applies_to(request)
            and isinstance(error, self.retry_errors)
        )


class RetryingSyncBackend:
    """
    A retrying wrapper around a synchronous backend — itself a
    ``Backend[Response]``, so it plugs into the clients like the backend
    it wraps.

    Example::

        >>> from action0.client import Client, RetryPolicy
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> flaky = StubBackend(Response(503), Response(503), Response(200, body="finally"))
        >>> policy = RetryPolicy(attempts=3, backoff=0)  # no waiting, for the example
        >>> backend = RetryingSyncBackend(flaky, policy)
        >>> Client(backend).send(Request("https://api.example.com/")).body_str()
        'finally'
        >>> len(flaky.requests)
        3
    """

    def __init__(
        self,
        inner: Backend[Response],
        policy: RetryPolicy = RetryPolicy(),
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: when and how to retry
        :param sleep: the wait function for the backoff (injectable for
                      tests)
        """
        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    @property
    def inner(self) -> Backend[Response]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    def send(self, request: Request) -> Response:
        """
        Send with retries: transient failures (per the policy) are
        retried after an exponential backoff; the final outcome is
        returned or raised as-is.

        :param request: the request to send
        :return: the response of the last attempt
        :raises BaseException: the error of the last attempt
        """
        attempt = 1
        while True:
            rejected: "Response | None" = None
            try:
                response = self._inner.send(request)
            except Exception as error:
                if not self._policy.should_retry_error(request, error, attempt):
                    raise
            else:
                if not self._policy.should_retry_response(request, response, attempt):
                    return response
                rejected = response
            self._sleep(self._policy.delay_for(attempt, rejected))
            attempt += 1

    def map(self, result: T, fn: Callable[[T], S]) -> S:
        """
        Apply a function to a result of :py:meth:`send` — synchronously
        that is simply ``fn(result)``.

        :param result: a value as returned by :py:meth:`send`
        :param fn: the function to apply
        :return: the return value of ``fn``
        """
        return fn(result)

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class RetryingAsyncBackend:
    """
    A retrying wrapper around an async backend — itself a
    ``Backend[Awaitable[Response]]``, so it plugs into the clients like
    the backend it wraps. The backoff waits with :py:func:`asyncio.sleep`
    by default; under trio, pass ``sleep=trio.sleep``.
    """

    def __init__(
        self,
        inner: Backend[Awaitable[Response]],
        policy: RetryPolicy = RetryPolicy(),
        *,
        sleep: "Callable[[float], Awaitable[None]] | None" = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: when and how to retry
        :param sleep: the awaitable wait function for the backoff;
                      ``None`` uses :py:func:`asyncio.sleep` (pass
                      ``trio.sleep`` on trio)
        """
        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    @property
    def inner(self) -> Backend[Awaitable[Response]]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    async def send(self, request: Request) -> Response:
        """
        Send with retries: transient failures (per the policy) are
        retried after an exponential backoff; the final outcome is
        returned or raised as-is.

        :param request: the request to send
        :return: (an awaitable of) the response of the last attempt
        :raises BaseException: the error of the last attempt, at ``await``
                time
        """
        if self._sleep is None:
            import asyncio

            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        else:
            sleep = self._sleep

        attempt = 1
        while True:
            rejected: "Response | None" = None
            try:
                response = await self._inner.send(request)
            except Exception as error:
                if not self._policy.should_retry_error(request, error, attempt):
                    raise
            else:
                if not self._policy.should_retry_response(request, response, attempt):
                    return response
                rejected = response
            await sleep(self._policy.delay_for(attempt, rejected))
            attempt += 1

    def map(self, result: Awaitable[T], fn: Callable[[T], S]) -> Awaitable[S]:
        """
        Apply a function inside an awaitable result of :py:meth:`send`.

        :param result: an awaitable as returned by :py:meth:`send`
        :param fn: the function to apply to the awaited value
        :return: an awaitable of the return value of ``fn``
        """

        async def mapped() -> S:
            return fn(await result)

        return mapped()

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class RetryingDeferredBackend:
    """
    A retrying wrapper around a Twisted backend — itself a
    ``Backend[Deferred[Response]]``, so it plugs into the clients like
    the backend it wraps. The backoff waits via
    :py:func:`twisted.internet.task.deferLater` on the given reactor (or
    the global one).
    """

    def __init__(
        self,
        inner: "Backend[Deferred[Response]]",
        policy: RetryPolicy = RetryPolicy(),
        *,
        reactor: Any = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: when and how to retry
        :param reactor: the clock for the backoff timer; ``None`` uses the
                        global reactor (imported lazily on the first send,
                        not at construction)
        """
        self._inner = inner
        self._policy = policy
        self._reactor = reactor

    @property
    def inner(self) -> "Backend[Deferred[Response]]":
        """The wrapped backend doing the actual sends."""
        return self._inner

    def send(self, request: Request) -> "Deferred[Response]":
        """
        Send with retries: transient failures (per the policy) are
        retried after an exponential backoff; the final outcome fires (or
        fails) the returned Deferred as-is.

        :param request: the request to send
        :return: a Deferred firing with the response of the last attempt
        """
        from twisted.internet.task import deferLater

        if self._reactor is None:
            # deliberately imported here: importing the global reactor at
            # module import time would install it as a side effect
            from twisted.internet import reactor as global_reactor

            self._reactor = global_reactor
        clock = self._reactor

        def attempt_once(attempt: int) -> "Deferred[Response]":
            def on_response(response: Response) -> "Response | Deferred[Response]":
                if not self._policy.should_retry_response(request, response, attempt):
                    return response
                return wait_and_repeat(response)

            def on_failure(failure: "Failure") -> "Failure | Deferred[Response]":
                error = failure.value
                if error is None or not self._policy.should_retry_error(request, error, attempt):
                    return failure
                return wait_and_repeat(None)

            def wait_and_repeat(rejected: "Response | None") -> "Deferred[Response]":
                delay = self._policy.delay_for(attempt, rejected)
                waited: "Deferred[None]" = deferLater(clock, delay)
                return waited.addCallback(lambda _: attempt_once(attempt + 1))

            return cast(
                "Deferred[Response]",
                self._inner.send(request).addCallbacks(on_response, on_failure),
            )

        return attempt_once(1)

    def map(self, result: "Deferred[T]", fn: Callable[[T], S]) -> "Deferred[S]":
        """
        Apply a function inside a Deferred result of :py:meth:`send` —
        Twisted's native ``addCallback``.

        :param result: a Deferred as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Deferred firing with the return value of ``fn``
        """
        return result.addCallback(fn)

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"

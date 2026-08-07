"""
The backend abstraction: the protocols a backend implements and the base
classes that make implementing one easy.

A backend is the pluggable piece that performs the actual HTTP I/O. It
takes an :py:class:`action0.req.Request` and produces an
:py:class:`action0.req.Response` — but *how* the response is delivered
depends on the execution model of the underlying HTTP library:

- a **sync** backend returns the ``Response`` directly,
- an **async** backend returns an ``Awaitable[Response]``,
- a **Twisted** backend returns a ``Deferred[Response]``.

Python's type system cannot abstract over "the wrapper type" (there are no
higher-kinded types), so there is one :py:class:`typing.Protocol` per
execution model: :py:class:`SyncBackend`, :py:class:`AsyncBackend` and
:py:class:`DeferredBackend`. A backend implements exactly one of them —
purely structurally, no registration or inheritance required.

Each protocol has two methods:

- ``send(request)`` performs the I/O and returns the (wrapped) response.
- ``map(result, fn)`` applies a function *inside* the wrapper: a sync
  backend just calls ``fn(result)``, an async backend awaits first, a
  Twisted backend uses ``addCallback``. This is the composition hook that
  lets generic code — most importantly
  :py:meth:`action0.client.api.APIClient.send` — attach response parsing
  to a send without knowing the execution model. It also keeps the three
  protocols structurally distinct, which is what makes the return-type
  overloads of :py:class:`~action0.client.client.Client` and
  :py:class:`~action0.client.api.APIClient` resolve to the right wrapper.

The base classes (:py:class:`BaseSyncBackend`, :py:class:`BaseAsyncBackend`,
:py:class:`BaseDeferredBackend`) implement ``send`` as a template around an
abstract ``_send`` doing the raw I/O, and add the extension points every
real-world backend ends up needing:

- :py:class:`~action0.client.hooks.Hook` instrumentation (logging, metrics,
  tracing, request decoration) around every send, and
- ``translate_error`` for normalizing library-specific exceptions into the
  :py:class:`~action0.client.errors.TransportError` family.

The built-in backends in :py:mod:`action0.client.backends` build on them,
and custom backends are encouraged to do the same — but any object with a
conforming ``send``/``map`` pair is a backend.
"""

from __future__ import annotations

import time
from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING
from typing import Awaitable
from typing import Callable
from typing import Iterable
from typing import Protocol
from typing import TypeAlias
from typing import TypeVar

from action0.req import Request
from action0.req import Response

from .hooks import Hook

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees these —
    # at runtime nothing here imports twisted
    from twisted.internet.defer import Deferred
    from twisted.python.failure import Failure

T = TypeVar("T")
S = TypeVar("S")


class SyncBackend(Protocol):
    """
    The protocol of a synchronous backend: ``send`` blocks and returns the
    :py:class:`~action0.req.response.Response` directly.

    Built-in implementations:
    :py:class:`~action0.client.backends.requests.RequestsBackend`,
    :py:class:`~action0.client.backends.httpx.HttpxBackend` and the test
    double :py:class:`~action0.client.testing.StubBackend`.
    """

    def send(self, request: Request) -> Response:
        """
        Send the request and block until the response arrived.

        :param request: the request to send
        :return: the response
        :raises action0.client.errors.TransportError: if no response could
                be obtained
        """
        ...

    def map(self, result: T, fn: Callable[[T], S]) -> S:
        """
        Apply a function to a result of :py:meth:`send` — synchronously
        that is simply ``fn(result)``.

        :param result: a value as returned by :py:meth:`send`
        :param fn: the function to apply
        :return: the return value of ``fn``
        """
        ...


class AsyncBackend(Protocol):
    """
    The protocol of an asyncio backend: ``send`` returns an awaitable of
    the :py:class:`~action0.req.response.Response`.

    Built-in implementations:
    :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend` and the
    test double :py:class:`~action0.client.testing.AsyncStubBackend`.
    """

    def send(self, request: Request) -> Awaitable[Response]:
        """
        Start sending the request.

        :param request: the request to send
        :return: an awaitable resolving to the response
        :raises action0.client.errors.TransportError: raised at ``await``
                time if no response could be obtained
        """
        ...

    def map(self, result: Awaitable[T], fn: Callable[[T], S]) -> Awaitable[S]:
        """
        Apply a function inside an awaitable result of :py:meth:`send`:
        the returned awaitable resolves to ``fn`` of what ``result``
        resolves to.

        :param result: an awaitable as returned by :py:meth:`send`
        :param fn: the function to apply to the awaited value
        :return: an awaitable of the return value of ``fn``
        """
        ...


class DeferredBackend(Protocol):
    """
    The protocol of a Twisted backend: ``send`` returns a
    :py:class:`~twisted.internet.defer.Deferred` firing with the
    :py:class:`~action0.req.response.Response`.

    Built-in implementations:
    :py:class:`~action0.client.backends.twisted.TwistedBackend` and the
    test double :py:class:`~action0.client.testing.DeferredStubBackend`.
    """

    def send(self, request: Request) -> Deferred[Response]:
        """
        Start sending the request.

        :param request: the request to send
        :return: a Deferred firing with the response, or failing with a
                 :py:class:`~action0.client.errors.TransportError`
        """
        ...

    def map(self, result: Deferred[T], fn: Callable[[T], S]) -> Deferred[S]:
        """
        Apply a function inside a Deferred result of :py:meth:`send` —
        Twisted's native ``addCallback``.

        :param result: a Deferred as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Deferred firing with the return value of ``fn``
        """
        ...


Backend: TypeAlias = SyncBackend | AsyncBackend | DeferredBackend
"""Anything that can be plugged into a :py:class:`~action0.client.client.Client`
or :py:class:`~action0.client.api.APIClient`: a backend of any of the three
execution models."""


class _BaseBackend:
    """
    The machinery shared by the three backend base classes: the
    :py:class:`~action0.client.hooks.Hook` list, the hook runners and the
    error translation extension point. Not meant to be subclassed directly —
    use the execution-model-specific base classes.
    """

    def __init__(self, hooks: Iterable[Hook] = ()) -> None:
        """
        :param hooks: the instrumentation hooks to run around every send,
                      in order
        """
        self.hooks: list[Hook] = list(hooks)
        """The instrumentation hooks run around every send, in order.
        Mutable: appending to it later is fine."""

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        """
        Translate an exception raised while sending into the exception to
        actually raise — the hook for normalizing library-specific errors
        into the :py:class:`~action0.client.errors.TransportError` family.
        The default keeps the error as-is; the built-in backends override
        this. The original error is attached as ``__cause__`` automatically
        whenever something different is returned.

        :param error: the exception raised while sending
        :param request: the request that was being sent
        :return: the exception to raise instead (or ``error`` itself)
        """
        return error

    def _run_request_hooks(self, request: Request) -> Request:
        """
        Run the ``on_request`` hooks, honoring replacement requests.

        :param request: the request about to be sent
        :return: the request to actually send
        """
        for hook in self.hooks:
            replacement = hook.on_request(request)
            if replacement is not None:
                request = replacement
        return request

    def _run_response_hooks(
        self, request: Request, response: Response, elapsed: float
    ) -> Response:
        """
        Run the ``on_response`` hooks, honoring replacement responses.

        :param request: the request that was sent
        :param response: the response that arrived
        :param elapsed: the seconds between sending and arrival
        :return: the response to actually hand to the caller
        """
        for hook in self.hooks:
            replacement = hook.on_response(request, response, elapsed)
            if replacement is not None:
                response = replacement
        return response

    def _run_error_hooks(self, request: Request, error: BaseException, elapsed: float) -> None:
        """
        Run the ``on_error`` hooks (purely observational).

        :param request: the request that was sent
        :param error: the translated error about to be raised
        :param elapsed: the seconds between sending and the failure
        """
        for hook in self.hooks:
            hook.on_error(request, error, elapsed)


class BaseSyncBackend(_BaseBackend, ABC):
    """
    Base class for :py:class:`SyncBackend` implementations: subclasses only
    implement :py:meth:`_send` with the raw HTTP I/O and inherit the hook
    and error-translation plumbing.

    Example — a minimal custom backend::

        >>> from action0.req import Request, Response
        >>> class EchoBackend(BaseSyncBackend):
        ...     '''Answers every request with its own URL instead of doing I/O.'''
        ...
        ...     def _send(self, request: Request) -> Response:
        ...         return Response(200, body=request.url.as_str(), request=request)
        >>> backend = EchoBackend()
        >>> backend.send(Request("https://example.com/hello")).body_str()
        'https://example.com/hello'

    ``map`` applies a function to a sent result — synchronously that is a
    plain call, but generic code uses it to stay agnostic of the execution
    model:

        >>> backend.map(backend.send(Request("https://example.com/")), lambda r: r.status)
        200
    """

    def send(self, request: Request) -> Response:
        """
        Send the request: run the ``on_request`` hooks, perform the I/O via
        :py:meth:`_send`, and run the ``on_response`` (or, after
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`,
        the ``on_error``) hooks.

        :param request: the request to send
        :return: the response
        :raises BaseException: whatever ``translate_error`` returned for the
                exception raised while sending — a
                :py:class:`~action0.client.errors.TransportError` for the
                built-in backends
        """
        request = self._run_request_hooks(request)
        started = time.monotonic()
        try:
            response = self._send(request)
        except Exception as error:
            translated = self.translate_error(error, request)
            self._run_error_hooks(request, translated, time.monotonic() - started)
            if translated is error:
                raise
            raise translated from error
        return self._run_response_hooks(request, response, time.monotonic() - started)

    @abstractmethod
    def _send(self, request: Request) -> Response:
        """
        Perform the actual HTTP I/O — the only method a subclass must
        implement. Raised exceptions are passed through
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`.

        :param request: the request to send
        :return: the response
        """

    def map(self, result: T, fn: Callable[[T], S]) -> S:
        """
        Apply a function to a result of :py:meth:`send` — synchronously
        that is simply ``fn(result)``.

        :param result: a value as returned by :py:meth:`send`
        :param fn: the function to apply
        :return: the return value of ``fn``
        """
        return fn(result)


class BaseAsyncBackend(_BaseBackend, ABC):
    """
    Base class for :py:class:`AsyncBackend` implementations: subclasses only
    implement the coroutine :py:meth:`_send` with the raw HTTP I/O and
    inherit the hook and error-translation plumbing.

    Example — a minimal custom backend::

        >>> import asyncio
        >>> from action0.req import Request, Response
        >>> class AsyncEchoBackend(BaseAsyncBackend):
        ...     '''Answers every request with its own URL instead of doing I/O.'''
        ...
        ...     async def _send(self, request: Request) -> Response:
        ...         return Response(200, body=request.url.as_str(), request=request)
        >>> backend = AsyncEchoBackend()
        >>> response = asyncio.run(backend.send(Request("https://example.com/hello")))
        >>> response.body_str()
        'https://example.com/hello'

    ``map`` chains a function onto the awaitable without awaiting it first:

        >>> status = backend.map(backend.send(Request("https://example.com/")), lambda r: r.status)
        >>> asyncio.run(status)
        200
    """

    async def send(self, request: Request) -> Response:
        """
        Send the request: run the ``on_request`` hooks, perform the I/O via
        :py:meth:`_send`, and run the ``on_response`` (or, after
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`,
        the ``on_error``) hooks. All hooks run inside the coroutine, i.e.
        once it is awaited.

        :param request: the request to send
        :return: (an awaitable of) the response
        :raises BaseException: whatever ``translate_error`` returned for the
                exception raised while sending — a
                :py:class:`~action0.client.errors.TransportError` for the
                built-in backends
        """
        request = self._run_request_hooks(request)
        started = time.monotonic()
        try:
            response = await self._send(request)
        except Exception as error:
            translated = self.translate_error(error, request)
            self._run_error_hooks(request, translated, time.monotonic() - started)
            if translated is error:
                raise
            raise translated from error
        return self._run_response_hooks(request, response, time.monotonic() - started)

    @abstractmethod
    async def _send(self, request: Request) -> Response:
        """
        Perform the actual HTTP I/O — the only method a subclass must
        implement. Raised exceptions are passed through
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`.

        :param request: the request to send
        :return: (an awaitable of) the response
        """

    def map(self, result: Awaitable[T], fn: Callable[[T], S]) -> Awaitable[S]:
        """
        Apply a function inside an awaitable result of :py:meth:`send`:
        returns a new awaitable resolving to ``fn`` of the awaited value.

        :param result: an awaitable as returned by :py:meth:`send`
        :param fn: the function to apply to the awaited value
        :return: an awaitable of the return value of ``fn``
        """

        async def mapped() -> S:
            return fn(await result)

        return mapped()


class BaseDeferredBackend(_BaseBackend, ABC):
    """
    Base class for :py:class:`DeferredBackend` implementations: subclasses
    only implement :py:meth:`_send` returning a
    :py:class:`~twisted.internet.defer.Deferred` of the response and inherit
    the hook and error-translation plumbing.

    This class itself is importable without twisted installed (so e.g.
    :py:class:`~action0.client.testing.DeferredStubBackend` can always be
    defined); actually sending requires twisted.

    Example::

        from twisted.internet import reactor
        from action0.client.backends.twisted import TwistedBackend
        from action0.req import Request

        backend = TwistedBackend()  # subclasses BaseDeferredBackend
        deferred = backend.send(Request("https://example.com/"))
        deferred.addCallback(lambda response: print(response.status))
    """

    def send(self, request: Request) -> Deferred[Response]:
        """
        Send the request: run the ``on_request`` hooks, start the I/O via
        :py:meth:`_send`, and chain the ``on_response`` (or, after
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`,
        the ``on_error``) hooks onto the Deferred.

        :param request: the request to send
        :return: a Deferred firing with the response, or failing with the
                 translated error — a
                 :py:class:`~action0.client.errors.TransportError` for the
                 built-in backend
        """
        # local import so the module (and subclasses like the stub used in
        # tests) can be imported without twisted installed
        from twisted.internet.defer import fail
        from twisted.python.failure import Failure

        request = self._run_request_hooks(request)
        started = time.monotonic()

        def on_response(response: Response) -> Response:
            return self._run_response_hooks(request, response, time.monotonic() - started)

        def on_failure(failure: Failure) -> Failure:
            error = failure.value
            if error is None:  # pragma: no cover — a Failure always carries a value
                return failure
            if isinstance(error, Exception):
                translated = self.translate_error(error, request)
            else:  # e.g. KeyboardInterrupt: observe, never translate
                translated = error
            self._run_error_hooks(request, translated, time.monotonic() - started)
            if translated is error:
                return failure
            translated.__cause__ = error
            return Failure(translated)  # type: ignore[no-untyped-call]

        try:
            result = self._send(request)
        except Exception as error:
            # initiating the request failed synchronously — deliver the
            # (translated) error through the Deferred like any other failure
            translated = self.translate_error(error, request)
            self._run_error_hooks(request, translated, time.monotonic() - started)
            if translated is not error:
                translated.__cause__ = error
            return fail(translated)
        return result.addCallbacks(on_response, on_failure)

    @abstractmethod
    def _send(self, request: Request) -> Deferred[Response]:
        """
        Start the actual HTTP I/O — the only method a subclass must
        implement. Failures (and synchronously raised exceptions) are
        passed through
        :py:meth:`~action0.client.backend._BaseBackend.translate_error`.

        :param request: the request to send
        :return: a Deferred firing with the response
        """

    def map(self, result: Deferred[T], fn: Callable[[T], S]) -> Deferred[S]:
        """
        Apply a function inside a Deferred result of :py:meth:`send` —
        Twisted's native ``addCallback``.

        :param result: a Deferred as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Deferred firing with the return value of ``fn``
        """
        return result.addCallback(fn)

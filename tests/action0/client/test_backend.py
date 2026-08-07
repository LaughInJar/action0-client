import asyncio
import unittest

from twisted.internet.defer import Deferred
from twisted.internet.defer import succeed

from action0.client import BaseAsyncBackend
from action0.client import BaseDeferredBackend
from action0.client import BaseSyncBackend
from action0.client import Hook
from action0.client import TransportError
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response


class RecordingHook(Hook):
    """A hook writing every call into a shared journal list."""

    def __init__(self, journal: list[str], name: str) -> None:
        self.journal = journal
        self.name = name

    def on_request(self, request: Request) -> "Request | None":
        self.journal.append(f"{self.name}:request")
        return None

    def on_response(
        self, request: Request, response: Response, elapsed: float
    ) -> "Response | None":
        self.journal.append(f"{self.name}:response:{response.status}:{elapsed >= 0}")
        return None

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        self.journal.append(f"{self.name}:error:{type(error).__name__}")


class TaggingHook(Hook):
    """A hook replacing the request and the response with tagged copies."""

    def on_request(self, request: Request) -> "Request | None":
        replacement = request.copy()
        replacement.headers.add("X-Tag", "tagged")
        return replacement

    def on_response(
        self, request: Request, response: Response, elapsed: float
    ) -> "Response | None":
        return response.copy(status=299)


class EchoBackend(BaseSyncBackend):
    """A sync backend answering with the request URL, no I/O."""

    def _send(self, request: Request) -> Response:
        return Response(200, body=request.url.as_str(), request=request)


class FailingBackend(BaseSyncBackend):
    """A sync backend that always raises its configured error."""

    def __init__(self, error: Exception, translate: "Exception | None" = None) -> None:
        super().__init__()
        self.error = error
        self.translate = translate

    def _send(self, request: Request) -> Response:
        raise self.error

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        return self.translate if self.translate is not None else error


class SyncBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backend.BaseSyncBackend`
    """

    def test_send_returns_the_response(self) -> None:
        """
        Test the plain success path through the template.
        """
        response = EchoBackend().send(Request("https://example.com/x"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body_str(), "https://example.com/x")

    def test_hooks_run_in_order(self) -> None:
        """
        Test that request and response hooks run in registration order.
        """
        journal: list[str] = []
        backend = EchoBackend(hooks=[RecordingHook(journal, "a"), RecordingHook(journal, "b")])
        backend.send(Request("https://example.com/"))
        self.assertEqual(
            journal,
            ["a:request", "b:request", "a:response:200:True", "b:response:200:True"],
        )

    def test_hooks_can_replace_request_and_response(self) -> None:
        """
        Test that hook replacements are honored: the tagged request is the
        one sent, the replaced response is the one returned.
        """
        backend = EchoBackend(hooks=[TaggingHook()])
        response = backend.send(Request("https://example.com/"))
        self.assertEqual(response.status, 299)
        assert response.request is not None
        self.assertEqual(response.request.headers["X-Tag"], "tagged")

    def test_untranslated_error_is_raised_as_is(self) -> None:
        """
        Test that with the default identity translation, the original
        exception propagates unchanged.
        """
        error = ConnectionResetError("nope")
        with self.assertRaises(ConnectionResetError) as caught:
            FailingBackend(error).send(Request("https://example.com/"))
        self.assertIs(caught.exception, error)

    def test_translated_error_is_raised_with_cause(self) -> None:
        """
        Test that a translated error replaces the original and chains it
        as __cause__.
        """
        original = ConnectionResetError("nope")
        translated = TransportError("connection reset")
        with self.assertRaises(TransportError) as caught:
            FailingBackend(original, translated).send(Request("https://example.com/"))
        self.assertIs(caught.exception, translated)
        self.assertIs(caught.exception.__cause__, original)

    def test_error_hooks_see_the_translated_error(self) -> None:
        """
        Test that on_error receives the translated exception.
        """
        journal: list[str] = []
        backend = FailingBackend(ConnectionResetError("nope"), TransportError("reset"))
        backend.hooks.append(RecordingHook(journal, "a"))
        with self.assertRaises(TransportError):
            backend.send(Request("https://example.com/"))
        self.assertEqual(journal, ["a:request", "a:error:TransportError"])

    def test_map_applies_the_function(self) -> None:
        """
        Test that the sync map is a plain call.
        """
        backend = EchoBackend()
        self.assertEqual(backend.map(Response(201), lambda response: response.status), 201)


class AsyncEchoBackend(BaseAsyncBackend):
    """An async backend answering with the request URL, no I/O."""

    async def _send(self, request: Request) -> Response:
        return Response(200, body=request.url.as_str(), request=request)


class AsyncFailingBackend(BaseAsyncBackend):
    """An async backend that always raises its configured error."""

    def __init__(self, error: Exception, translate: "Exception | None" = None) -> None:
        super().__init__()
        self.error = error
        self.translate = translate

    async def _send(self, request: Request) -> Response:
        raise self.error

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        return self.translate if self.translate is not None else error


class AsyncBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.backend.BaseAsyncBackend`
    """

    async def test_send_returns_the_response(self) -> None:
        """
        Test the plain success path through the async template.
        """
        response = await AsyncEchoBackend().send(Request("https://example.com/x"))
        self.assertEqual(response.body_str(), "https://example.com/x")

    async def test_hooks_run_inside_the_coroutine(self) -> None:
        """
        Test that no hook runs before the coroutine is awaited.
        """
        journal: list[str] = []
        backend = AsyncEchoBackend(hooks=[RecordingHook(journal, "a")])
        coroutine = backend.send(Request("https://example.com/"))
        self.assertEqual(journal, [])
        await coroutine
        self.assertEqual(journal, ["a:request", "a:response:200:True"])

    async def test_translated_error_is_raised_with_cause(self) -> None:
        """
        Test error translation and chaining at await time.
        """
        original = ConnectionResetError("nope")
        translated = TransportError("connection reset")
        backend = AsyncFailingBackend(original, translated)
        with self.assertRaises(TransportError) as caught:
            await backend.send(Request("https://example.com/"))
        self.assertIs(caught.exception, translated)
        self.assertIs(caught.exception.__cause__, original)

    async def test_map_chains_onto_the_awaitable(self) -> None:
        """
        Test that map resolves to the function of the awaited value.
        """
        backend = AsyncEchoBackend()
        wrapped = backend.map(backend.send(Request("https://example.com/")), lambda r: r.status)
        self.assertEqual(await wrapped, 200)


class DeferredEchoBackend(BaseDeferredBackend):
    """A Deferred backend answering with the request URL, no I/O."""

    def _send(self, request: Request) -> "Deferred[Response]":
        return succeed(Response(200, body=request.url.as_str(), request=request))


class DeferredFailingBackend(BaseDeferredBackend):
    """A Deferred backend failing its Deferred (or raising synchronously)."""

    def __init__(
        self,
        error: Exception,
        translate: "Exception | None" = None,
        raise_synchronously: bool = False,
    ) -> None:
        super().__init__()
        self.error = error
        self.translate = translate
        self.raise_synchronously = raise_synchronously

    def _send(self, request: Request) -> "Deferred[Response]":
        if self.raise_synchronously:
            raise self.error
        deferred: "Deferred[Response]" = Deferred()
        deferred.errback(self.error)
        return deferred

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        return self.translate if self.translate is not None else error


class DeferredBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backend.BaseDeferredBackend`
    """

    def test_send_fires_with_the_response(self) -> None:
        """
        Test the plain success path through the Deferred template.
        """
        deferred = DeferredEchoBackend().send(Request("https://example.com/x"))
        response = deferred_result(deferred)
        self.assertEqual(response.body_str(), "https://example.com/x")

    def test_hooks_run_around_the_send(self) -> None:
        """
        Test that request hooks run synchronously and response hooks in
        the callback chain.
        """
        journal: list[str] = []
        backend = DeferredEchoBackend(hooks=[RecordingHook(journal, "a")])
        deferred_result(backend.send(Request("https://example.com/")))
        self.assertEqual(journal, ["a:request", "a:response:200:True"])

    def test_failure_is_translated_with_cause(self) -> None:
        """
        Test that a failing Deferred surfaces the translated error,
        chaining the original.
        """
        original = ConnectionResetError("nope")
        translated = TransportError("connection reset")
        backend = DeferredFailingBackend(original, translated)
        with self.assertRaises(TransportError) as caught:
            deferred_result(backend.send(Request("https://example.com/")))
        self.assertIs(caught.exception, translated)
        self.assertIs(caught.exception.__cause__, original)

    def test_untranslated_failure_passes_through(self) -> None:
        """
        Test that without translation the original failure is kept.
        """
        original = ConnectionResetError("nope")
        backend = DeferredFailingBackend(original)
        with self.assertRaises(ConnectionResetError) as caught:
            deferred_result(backend.send(Request("https://example.com/")))
        self.assertIs(caught.exception, original)

    def test_synchronous_raise_becomes_a_failed_deferred(self) -> None:
        """
        Test that an exception raised while initiating the request is
        delivered through the Deferred, translated like any other failure.
        """
        original = ConnectionResetError("nope")
        translated = TransportError("connection reset")
        backend = DeferredFailingBackend(original, translated, raise_synchronously=True)
        with self.assertRaises(TransportError) as caught:
            deferred_result(backend.send(Request("https://example.com/")))
        self.assertIs(caught.exception, translated)
        self.assertIs(caught.exception.__cause__, original)

    def test_error_hooks_see_the_translated_error(self) -> None:
        """
        Test that on_error receives the translated exception in the
        errback path.
        """
        journal: list[str] = []
        backend = DeferredFailingBackend(ConnectionResetError("nope"), TransportError("reset"))
        backend.hooks.append(RecordingHook(journal, "a"))
        with self.assertRaises(TransportError):
            deferred_result(backend.send(Request("https://example.com/")))
        self.assertEqual(journal, ["a:request", "a:error:TransportError"])

    def test_map_uses_add_callback(self) -> None:
        """
        Test that map transforms the eventual value.
        """
        backend = DeferredEchoBackend()
        deferred = backend.map(
            backend.send(Request("https://example.com/")), lambda response: response.status
        )
        self.assertEqual(deferred_result(deferred), 200)


class AsyncioSmokeTestCase(unittest.TestCase):
    """
    plain asyncio.run() smoke test, mirroring how sync code drives the
    async backend without an async test framework
    """

    def test_asyncio_run_drives_the_backend(self) -> None:
        """
        Test that a bare asyncio.run of send() works.
        """
        response = asyncio.run(AsyncEchoBackend().send(Request("https://example.com/")))
        self.assertEqual(response.status, 200)

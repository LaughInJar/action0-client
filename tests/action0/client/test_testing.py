import unittest

from action0.client import LoggingHook
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response


class StubBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.testing.StubBackend`
    """

    def test_defaults_to_a_plain_200(self) -> None:
        """
        Test the zero-argument stub.
        """
        response = StubBackend().send(Request("https://example.com/"))
        self.assertEqual(response.status, 200)

    def test_scripted_responses_in_order_last_repeats(self) -> None:
        """
        Test the response script semantics.
        """
        backend = StubBackend(Response(200), Response(429), Response(503))
        request = Request("https://example.com/")
        statuses = [backend.send(request).status for _ in range(5)]
        self.assertEqual(statuses, [200, 429, 503, 503, 503])

    def test_requests_are_recorded(self) -> None:
        """
        Test the request journal.
        """
        backend = StubBackend()
        first = Request("https://example.com/a")
        second = Request("https://example.com/b", "POST")
        backend.send(first)
        backend.send(second)
        self.assertEqual(backend.requests, [first, second])

    def test_responses_are_copied_and_linked(self) -> None:
        """
        Test that every answer is an independent copy carrying the request.
        """
        canned = Response(200, headers={"X-N": "1"})
        backend = StubBackend(canned)
        request = Request("https://example.com/")

        answer = backend.send(request)
        answer.headers["X-N"] = "mutated"

        self.assertIs(answer.request, request)
        self.assertEqual(backend.send(request).headers["X-N"], "1")
        self.assertEqual(canned.headers["X-N"], "1")

    def test_responder_callables(self) -> None:
        """
        Test dynamic responders, including raising ones.
        """

        def responder(request: Request) -> Response:
            if request.url.path == "/boom":
                raise ConnectionResetError("nope")
            return Response(200, body=request.url.path)

        backend = StubBackend(responder)
        self.assertEqual(backend.send(Request("https://example.com/a")).body_str(), "/a")
        with self.assertRaises(ConnectionResetError):
            backend.send(Request("https://example.com/boom"))

    def test_hooks_run_like_on_real_backends(self) -> None:
        """
        Test that the stub drives the full hook machinery.
        """
        import logging

        logger = logging.getLogger("test.stub-hooks")
        backend = StubBackend(hooks=[LoggingHook(logger, level=logging.INFO)])
        with self.assertLogs(logger, level=logging.INFO) as logs:
            backend.send(Request("https://example.com/"))
        self.assertEqual(len(logs.output), 2)

    def test_repr_counts_requests(self) -> None:
        """
        Test the debugging representation.
        """
        backend = StubBackend()
        backend.send(Request("https://example.com/"))
        self.assertEqual(repr(backend), "StubBackend(1 requests)")


class AsyncStubBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.testing.AsyncStubBackend`
    """

    async def test_send_awaits_to_the_scripted_response(self) -> None:
        """
        Test the async stub round trip.
        """
        backend = AsyncStubBackend(Response(418))
        request = Request("https://example.com/")
        response = await backend.send(request)
        self.assertEqual(response.status, 418)
        self.assertEqual(backend.requests, [request])


class DeferredStubBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.testing.DeferredStubBackend` and
    :py:func:`action0.client.testing.deferred_result`
    """

    def test_send_fires_with_the_scripted_response(self) -> None:
        """
        Test the Deferred stub round trip.
        """
        backend = DeferredStubBackend(Response(418))
        request = Request("https://example.com/")
        response = deferred_result(backend.send(request))
        self.assertEqual(response.status, 418)
        self.assertEqual(backend.requests, [request])

    def test_deferred_result_raises_failures(self) -> None:
        """
        Test that deferred_result re-raises what the Deferred failed with.
        """

        def explode(request: Request) -> Response:
            raise ConnectionResetError("nope")

        backend = DeferredStubBackend(explode)
        with self.assertRaises(ConnectionResetError):
            deferred_result(backend.send(Request("https://example.com/")))

    def test_deferred_result_rejects_pending_deferreds(self) -> None:
        """
        Test the has-not-fired assertion.
        """
        from twisted.internet.defer import Deferred

        with self.assertRaisesRegex(AssertionError, "not fired"):
            deferred_result(Deferred())

import unittest
from typing import Any

from twisted.internet.task import Clock

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import RetryPolicy
from action0.client import TransportError
from action0.client import query
from action0.client.retry import RetryingAsyncBackend
from action0.client.retry import RetryingDeferredBackend
from action0.client.retry import RetryingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response


class Ping(JsonOperation[Any]):
    """The example operation used in the integration tests."""

    path = "/ping"
    q: "str | None" = query(default=None)


def flaky_responder(failures: int, error: Exception) -> Any:
    """
    A responder failing the first ``failures`` sends, then answering 200.

    :param failures: how many sends fail before the success
    :param error: the exception to fail with
    :return: the responder callable for a stub backend
    """
    counter = {"sent": 0}

    def responder(request: Request) -> Response:
        counter["sent"] += 1
        if counter["sent"] <= failures:
            raise error
        return Response(200, body="recovered")

    return responder


class RetryPolicyTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.retry.RetryPolicy`
    """

    def test_delay_grows_exponentially_and_is_capped(self) -> None:
        """
        Test the backoff progression.
        """
        policy = RetryPolicy(backoff=1.0, multiplier=2.0, max_backoff=5.0)
        self.assertEqual(
            [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)], [1.0, 2.0, 4.0, 5.0]
        )

    def test_method_gate(self) -> None:
        """
        Test that non-idempotent methods are not retried by default, but
        can be allowed.
        """
        get = Request("https://example.com/")
        post = Request("https://example.com/", "POST")
        transient = Response(503)

        policy = RetryPolicy()
        self.assertTrue(policy.should_retry_response(get, transient, 1))
        self.assertFalse(policy.should_retry_response(post, transient, 1))
        self.assertFalse(policy.should_retry_error(post, TransportError("x"), 1))

        anything = RetryPolicy(methods=None)
        self.assertTrue(anything.should_retry_response(post, transient, 1))

    def test_attempt_budget(self) -> None:
        """
        Test that the last allowed attempt is never retried.
        """
        request = Request("https://example.com/")
        policy = RetryPolicy(attempts=3)
        self.assertTrue(policy.should_retry_response(request, Response(503), 2))
        self.assertFalse(policy.should_retry_response(request, Response(503), 3))

    def test_only_listed_statuses_and_errors_count(self) -> None:
        """
        Test the transient status and error filters.
        """
        request = Request("https://example.com/")
        policy = RetryPolicy()
        self.assertFalse(policy.should_retry_response(request, Response(404), 1))
        self.assertFalse(policy.should_retry_error(request, ValueError("bug"), 1))
        self.assertTrue(policy.should_retry_error(request, TransportError("net"), 1))


class RetryingSyncBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.retry.RetryingSyncBackend`
    """

    def backend(
        self, inner: StubBackend, policy: RetryPolicy
    ) -> "tuple[RetryingSyncBackend, list[float]]":
        """
        A retrying backend with a recording fake sleep.

        :param inner: the stub to wrap
        :param policy: the retry policy
        :return: the backend and the list of recorded sleep durations
        """
        slept: list[float] = []
        return RetryingSyncBackend(inner, policy, sleep=slept.append), slept

    def test_transient_statuses_are_retried_with_backoff(self) -> None:
        """
        Test the response-retry path, including the recorded waits.
        """
        inner = StubBackend(Response(503), Response(429), Response(200, body="ok"))
        backend, slept = self.backend(inner, RetryPolicy(attempts=3, backoff=1.0))

        response = backend.send(Request("https://example.com/"))

        self.assertEqual(response.body_str(), "ok")
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(slept, [1.0, 2.0])

    def test_exhausted_attempts_return_the_last_response(self) -> None:
        """
        Test that the policy never invents failures: the final 503 is
        returned, not raised.
        """
        inner = StubBackend(Response(503))
        backend, _ = self.backend(inner, RetryPolicy(attempts=2, backoff=0))

        response = backend.send(Request("https://example.com/"))

        self.assertEqual(response.status, 503)
        self.assertEqual(len(inner.requests), 2)

    def test_transport_errors_are_retried(self) -> None:
        """
        Test the error-retry path.
        """
        inner = StubBackend(flaky_responder(2, TransportError("flaky")))
        backend, _ = self.backend(inner, RetryPolicy(attempts=3, backoff=0))

        response = backend.send(Request("https://example.com/"))

        self.assertEqual(response.body_str(), "recovered")
        self.assertEqual(len(inner.requests), 3)

    def test_exhausted_attempts_raise_the_last_error(self) -> None:
        """
        Test that the final error propagates unchanged.
        """
        inner = StubBackend(flaky_responder(5, TransportError("flaky")))
        backend, _ = self.backend(inner, RetryPolicy(attempts=2, backoff=0))

        with self.assertRaises(TransportError):
            backend.send(Request("https://example.com/"))
        self.assertEqual(len(inner.requests), 2)

    def test_non_retryable_outcomes_pass_through_immediately(self) -> None:
        """
        Test that non-transient statuses and errors trigger no retries.
        """
        inner = StubBackend(Response(404))
        backend, slept = self.backend(inner, RetryPolicy(attempts=3))
        self.assertEqual(backend.send(Request("https://example.com/")).status, 404)
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(slept, [])

        bug = StubBackend(flaky_responder(1, ValueError("bug")))
        backend, _ = self.backend(bug, RetryPolicy(attempts=3, backoff=0))
        with self.assertRaises(ValueError):
            backend.send(Request("https://example.com/"))
        self.assertEqual(len(bug.requests), 1)

    def test_post_is_not_retried_by_default(self) -> None:
        """
        Test the idempotency gate end to end.
        """
        inner = StubBackend(Response(503))
        backend, _ = self.backend(inner, RetryPolicy(attempts=3, backoff=0))
        self.assertEqual(backend.send(Request("https://example.com/", "POST")).status, 503)
        self.assertEqual(len(inner.requests), 1)

    def test_api_client_integration(self) -> None:
        """
        Test that the wrapper composes with APIClient like any sync
        backend.
        """
        inner = StubBackend(Response(503), Response(200, body='{"pong": true}'))
        backend, _ = self.backend(inner, RetryPolicy(attempts=2, backoff=0))
        client = APIClient(backend, "https://api.example.com")
        self.assertEqual(client.send(Ping()), {"pong": True})

    def test_inner_property_and_repr(self) -> None:
        """
        Test that the wrapped backend stays reachable and shows in repr.
        """
        inner = StubBackend()
        backend = RetryingSyncBackend(inner)
        self.assertIs(backend.inner, inner)
        self.assertEqual(repr(backend), "RetryingSyncBackend(StubBackend(0 requests))")


class RetryingAsyncBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.retry.RetryingAsyncBackend`
    """

    async def test_transient_statuses_are_retried_with_backoff(self) -> None:
        """
        Test the async response-retry path with a recording fake sleep.
        """
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        inner = AsyncStubBackend(Response(503), Response(200, body="ok"))
        backend = RetryingAsyncBackend(
            inner, RetryPolicy(attempts=2, backoff=0.25), sleep=fake_sleep
        )

        response = await backend.send(Request("https://example.com/"))

        self.assertEqual(response.body_str(), "ok")
        self.assertEqual(len(inner.requests), 2)
        self.assertEqual(slept, [0.25])

    async def test_exhausted_attempts_raise_the_last_error(self) -> None:
        """
        Test the async error path with the default asyncio sleep.
        """
        inner = AsyncStubBackend(flaky_responder(5, TransportError("flaky")))
        backend = RetryingAsyncBackend(inner, RetryPolicy(attempts=2, backoff=0))

        with self.assertRaises(TransportError):
            await backend.send(Request("https://example.com/"))
        self.assertEqual(len(inner.requests), 2)

    async def test_api_client_integration(self) -> None:
        """
        Test that the wrapper composes with APIClient like any async
        backend.
        """
        inner = AsyncStubBackend(Response(503), Response(200, body='{"pong": true}'))
        backend = RetryingAsyncBackend(inner, RetryPolicy(attempts=2, backoff=0))
        client = APIClient(backend, "https://api.example.com")
        self.assertEqual(await client.send(Ping()), {"pong": True})


class RetryingDeferredBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.retry.RetryingDeferredBackend`
    (with a Clock, so the backoff is deterministic)
    """

    def test_transient_statuses_are_retried_after_the_backoff(self) -> None:
        """
        Test that the retry fires only once the clock advances past the
        backoff delay.
        """
        clock = Clock()
        inner = DeferredStubBackend(Response(503), Response(200, body="ok"))
        backend = RetryingDeferredBackend(
            inner, RetryPolicy(attempts=2, backoff=1.0), reactor=clock
        )

        deferred = backend.send(Request("https://example.com/"))
        seen: list[Response] = []

        def record(response: Response) -> Response:
            seen.append(response)
            return response

        deferred.addCallback(record)

        # first attempt done, waiting out the backoff
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(seen, [])

        clock.advance(1.0)
        self.assertEqual(len(inner.requests), 2)
        self.assertEqual(seen[0].body_str(), "ok")

    def test_exhausted_attempts_fail_with_the_last_error(self) -> None:
        """
        Test the error path through the errback chain.
        """
        clock = Clock()
        inner = DeferredStubBackend(flaky_responder(5, TransportError("flaky")))
        backend = RetryingDeferredBackend(inner, RetryPolicy(attempts=2, backoff=0), reactor=clock)

        deferred = backend.send(Request("https://example.com/"))
        clock.advance(0)

        with self.assertRaises(TransportError):
            deferred_result(deferred)
        self.assertEqual(len(inner.requests), 2)

    def test_api_client_integration(self) -> None:
        """
        Test that the wrapper composes with APIClient like any Deferred
        backend.
        """
        clock = Clock()
        inner = DeferredStubBackend(Response(503), Response(200, body='{"pong": true}'))
        backend = RetryingDeferredBackend(
            inner, RetryPolicy(attempts=2, backoff=0.5), reactor=clock
        )
        client = APIClient(backend, "https://api.example.com")

        deferred = client.send(Ping())
        clock.advance(0.5)

        self.assertEqual(deferred_result(deferred), {"pong": True})

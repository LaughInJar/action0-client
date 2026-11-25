import datetime
import email.utils
import unittest
from collections.abc import AsyncIterator
from typing import Any
from typing import ClassVar

from twisted.internet.task import Clock

from action0.client import APIClient
from action0.client import APIError
from action0.client import Client
from action0.client import JsonOperation
from action0.client import PayloadError
from action0.client import RequestOptions
from action0.client import RetryPolicy
from action0.client import TransportError
from action0.client import query
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.caching import CachingSyncBackend
from action0.client.retry import RetryingAsyncBackend
from action0.client.retry import RetryingDeferredBackend
from action0.client.retry import RetryingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import AsyncIterableBody
from action0.req import IterableBody
from action0.req import Method
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
        Test the backoff progression (with the jitter disabled, so it is
        exact).
        """
        policy = RetryPolicy(backoff=1.0, multiplier=2.0, max_backoff=5.0, jitter=False)
        self.assertEqual(
            [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)], [1.0, 2.0, 4.0, 5.0]
        )

    def test_full_jitter_scales_the_delay(self) -> None:
        """
        Test that the default full jitter multiplies the exponential
        delay by the rng draw.
        """
        policy = RetryPolicy(backoff=1.0, multiplier=2.0, max_backoff=5.0, rng=lambda: 0.5)
        self.assertEqual(
            [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)], [0.5, 1.0, 2.0, 2.5]
        )

    def test_jitter_stays_below_the_full_delay(self) -> None:
        """
        Test the default rng path: every jittered delay lands in
        [0, exponential delay).
        """
        policy = RetryPolicy(backoff=1.0, multiplier=2.0, max_backoff=5.0)
        for _ in range(20):
            self.assertGreaterEqual(policy.delay_for(2), 0.0)
            self.assertLess(policy.delay_for(2), 2.0)

    def test_retry_after_seconds_wins_over_the_backoff(self) -> None:
        """
        Test that a Retry-After header (seconds form) overrides the
        computed delay, jitter included, but stays capped.
        """
        policy = RetryPolicy(backoff=1.0, max_backoff=30.0)
        hinted = Response(429, headers={"Retry-After": "7"})
        self.assertEqual(policy.delay_for(1, hinted), 7.0)

        greedy = Response(429, headers={"Retry-After": "3600"})
        self.assertEqual(policy.delay_for(1, greedy), 30.0)

    def test_retry_after_http_date_form(self) -> None:
        """
        Test the HTTP-date form: a future date is capped at max_backoff,
        a past date means "retry right away".
        """
        policy = RetryPolicy(max_backoff=30.0)
        future = email.utils.format_datetime(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
            usegmt=True,
        )
        self.assertEqual(policy.delay_for(1, Response(503, headers={"Retry-After": future})), 30.0)

        past = "Mon, 01 Jan 2001 00:00:00 GMT"
        self.assertEqual(policy.delay_for(1, Response(503, headers={"Retry-After": past})), 0.0)

    def test_retry_after_fallbacks(self) -> None:
        """
        Test that an unparseable header, a missing response and
        respect_retry_after=False all fall back to the plain backoff.
        """
        policy = RetryPolicy(backoff=1.0, jitter=False)
        garbled = Response(503, headers={"Retry-After": "soon"})
        self.assertEqual(policy.delay_for(1, garbled), 1.0)
        self.assertEqual(policy.delay_for(1, None), 1.0)

        deaf = RetryPolicy(backoff=1.0, jitter=False, respect_retry_after=False)
        hinted = Response(429, headers={"Retry-After": "7"})
        self.assertEqual(deaf.delay_for(1, hinted), 1.0)

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
        backend, slept = self.backend(inner, RetryPolicy(attempts=3, backoff=1.0, jitter=False))

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

    def test_retry_after_is_honored_end_to_end(self) -> None:
        """
        Test that the wait between attempts follows the server's
        Retry-After header instead of the backoff.
        """
        inner = StubBackend(Response(429, headers={"Retry-After": "5"}), Response(200, body="ok"))
        backend, slept = self.backend(inner, RetryPolicy(attempts=2, backoff=1.0, jitter=False))

        response = backend.send(Request("https://example.com/"))

        self.assertEqual(response.body_str(), "ok")
        self.assertEqual(slept, [5.0])

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
            inner, RetryPolicy(attempts=2, backoff=0.25, jitter=False), sleep=fake_sleep
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
            inner, RetryPolicy(attempts=2, backoff=1.0, jitter=False), reactor=clock
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

    def test_retry_after_stretches_the_wait(self) -> None:
        """
        Test that the Twisted wrapper waits out a Retry-After header that
        is longer than the backoff.
        """
        clock = Clock()
        inner = DeferredStubBackend(
            Response(429, headers={"Retry-After": "2"}), Response(200, body="ok")
        )
        backend = RetryingDeferredBackend(
            inner, RetryPolicy(attempts=2, backoff=1.0, jitter=False), reactor=clock
        )

        backend.send(Request("https://example.com/"))

        # after the plain backoff (1s) nothing may happen yet — the server
        # asked for 2s
        clock.advance(1.0)
        self.assertEqual(len(inner.requests), 1)
        clock.advance(1.0)
        self.assertEqual(len(inner.requests), 2)

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
            inner, RetryPolicy(attempts=2, backoff=0.5, jitter=False), reactor=clock
        )
        client = APIClient(backend, "https://api.example.com")

        deferred = client.send(Ping())
        clock.advance(0.5)

        self.assertEqual(deferred_result(deferred), {"pong": True})


class Translate(JsonOperation[Any]):
    """A POST endpoint that only reads — safe to send again."""

    method = Method.POST
    path = "/translate"
    request_options = RequestOptions(retry_safe=True)


class CreateOrder(JsonOperation[Any]):
    """A POST endpoint with a side effect — the default applies."""

    method = Method.POST
    path = "/orders"


class RetrySafeTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for per-request retry safety
    (:py:attr:`action0.client.options.RequestOptions.retry_safe`) under
    the default policy, which retries idempotent methods only
    """

    BASE_URL = "https://api.example.com"

    def setUp(self) -> None:
        self.policy = RetryPolicy(attempts=3, backoff=0)
        self.responses = (Response(503), Response(503), Response(200, body='{"text": "hallo"}'))

    def test_policy_takes_the_request_at_its_word(self) -> None:
        """
        Test both directions of the override and the fallback to the
        method gate — also for a policy that allows every method.
        """
        post = Request("https://example.com/", "POST")
        safe_post = RequestOptions(retry_safe=True).apply(post.copy())
        get = Request("https://example.com/")
        unsafe_get = RequestOptions(retry_safe=False).apply(get.copy())
        undecided_get = RequestOptions().apply(get.copy())

        policy = RetryPolicy()
        self.assertFalse(policy.applies_to(post))
        self.assertTrue(policy.applies_to(safe_post))
        self.assertTrue(policy.applies_to(get))
        self.assertFalse(policy.applies_to(unsafe_get))
        self.assertTrue(policy.applies_to(undecided_get))

        self.assertTrue(policy.should_retry_response(safe_post, Response(503), 1))
        self.assertTrue(policy.should_retry_error(safe_post, TransportError("reset"), 1))
        self.assertFalse(policy.should_retry_response(unsafe_get, Response(503), 1))
        # safety lifts the method gate only: attempts and statuses still count
        self.assertFalse(policy.should_retry_response(safe_post, Response(503), 3))
        self.assertFalse(policy.should_retry_response(safe_post, Response(404), 1))

        self.assertFalse(RetryPolicy(methods=None).applies_to(unsafe_get))

    def test_sync(self) -> None:
        """
        Test that the declared-safe POST is retried and the plain POST is
        not, on a synchronous backend.
        """
        inner = StubBackend(*self.responses)
        client = APIClient(RetryingSyncBackend(inner, self.policy), self.BASE_URL)
        self.assertEqual(client.send(Translate()), {"text": "hallo"})
        self.assertEqual(len(inner.requests), 3)

        inner = StubBackend(*self.responses)
        client = APIClient(RetryingSyncBackend(inner, self.policy), self.BASE_URL)
        with self.assertRaises(APIError):
            client.send(CreateOrder())
        self.assertEqual(len(inner.requests), 1)

    async def test_async(self) -> None:
        """
        Test the same on an async backend.
        """
        inner = AsyncStubBackend(*self.responses)
        client = APIClient(RetryingAsyncBackend(inner, self.policy), self.BASE_URL)
        self.assertEqual(await client.send(Translate()), {"text": "hallo"})
        self.assertEqual(len(inner.requests), 3)

        inner = AsyncStubBackend(*self.responses)
        client = APIClient(RetryingAsyncBackend(inner, self.policy), self.BASE_URL)
        with self.assertRaises(APIError):
            await client.send(CreateOrder())
        self.assertEqual(len(inner.requests), 1)

    def test_deferred(self) -> None:
        """
        Test the same on a Twisted backend (a Clock runs the zero-length
        backoff timers).
        """
        clock = Clock()
        inner = DeferredStubBackend(*self.responses)
        backend = RetryingDeferredBackend(inner, self.policy, reactor=clock)
        deferred = APIClient(backend, self.BASE_URL).send(Translate())
        clock.pump([0, 0])
        self.assertEqual(deferred_result(deferred), {"text": "hallo"})
        self.assertEqual(len(inner.requests), 3)

        inner = DeferredStubBackend(*self.responses)
        backend = RetryingDeferredBackend(inner, self.policy, reactor=clock)
        with self.assertRaises(APIError):
            deferred_result(APIClient(backend, self.BASE_URL).send(CreateOrder()))
        self.assertEqual(len(inner.requests), 1)

    def test_futures(self) -> None:
        """
        Test the same through the thread-pool backend, where the retry
        loop runs in the worker thread.
        """
        inner = StubBackend(*self.responses)
        with ThreadPoolBackend(RetryingSyncBackend(inner, self.policy)) as backend:
            client = APIClient(backend, self.BASE_URL)
            self.assertEqual(client.send(Translate()).result(timeout=5), {"text": "hallo"})
        self.assertEqual(len(inner.requests), 3)

        inner = StubBackend(*self.responses)
        with ThreadPoolBackend(RetryingSyncBackend(inner, self.policy)) as backend:
            client = APIClient(backend, self.BASE_URL)
            with self.assertRaises(APIError):
                client.send(CreateOrder()).result(timeout=5)
        self.assertEqual(len(inner.requests), 1)


def html() -> Response:
    """A 200 that is not what a JSON operation expects."""
    return Response(200, headers={"Content-Type": "text/html"}, body="<html>busy</html>")


def pong() -> Response:
    """A good answer for :py:class:`Ping`."""
    return Response(200, body='{"pong": true}')


class CountingPing(JsonOperation[Any]):
    """A ping that counts how often a response body was decoded."""

    path = "/ping"
    loads: ClassVar[list[int]] = []

    def load(self, response: Response) -> Any:
        self.loads.append(response.status)
        return super().load(response)


class ParseRetryTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for retries that cover parsing: a response with an acceptable
    status that the operation cannot parse is retried when the policy
    names the error (``retry_parse_errors``)
    """

    BASE_URL = "https://api.example.com"

    def setUp(self) -> None:
        self.policy = RetryPolicy(attempts=3, backoff=0, retry_parse_errors=(PayloadError,))
        CountingPing.loads.clear()

    def sync_client(self, *responses: Any, policy: "RetryPolicy | None" = None) -> Any:
        """
        An APIClient over a retrying stub backend.

        :param responses: the stub's script
        :param policy: the retry policy (default: the parse-retrying one)
        :return: the client and the stub
        """
        inner = StubBackend(*responses)
        backend = RetryingSyncBackend(inner, policy or self.policy)
        return APIClient(backend, self.BASE_URL), inner

    def test_unparseable_response_is_retried(self) -> None:
        """
        Test the motivating case: an HTML page on a 200 is thrown away
        and the request sent again until the body parses.
        """
        client, inner = self.sync_client(html(), html(), pong())
        self.assertEqual(client.send(Ping()), {"pong": True})
        self.assertEqual(len(inner.requests), 3)

    def test_parse_failures_are_not_retried_by_default(self) -> None:
        """
        Test the opt-in: without ``retry_parse_errors`` the failure
        surfaces at once, as before.
        """
        client, inner = self.sync_client(html(), pong(), policy=RetryPolicy(backoff=0))
        with self.assertRaises(PayloadError):
            client.send(Ping())
        self.assertEqual(len(inner.requests), 1)

    def test_each_response_is_parsed_once(self) -> None:
        """
        Test that the retry wrapper and the client share one parse run
        per response: three responses, three decodes — not four.
        """
        client, _inner = self.sync_client(html(), html(), pong())
        self.assertEqual(client.send(CountingPing()), {"pong": True})
        self.assertEqual(CountingPing.loads, [200, 200, 200])

    def test_one_attempt_counter_for_every_kind_of_failure(self) -> None:
        """
        Test that transport errors, transient statuses and parse
        failures draw on the same budget: three attempts are three
        sends, whatever went wrong in between.
        """
        outcomes: "list[Response | Exception]" = [TransportError("reset"), Response(503), pong()]

        def flaky(request: Request) -> Response:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client, inner = self.sync_client(flaky)
        self.assertEqual(client.send(Ping()), {"pong": True})
        self.assertEqual(len(inner.requests), 3)

        client, inner = self.sync_client(Response(503), html(), html(), pong())
        with self.assertRaises(PayloadError):
            client.send(Ping())
        self.assertEqual(len(inner.requests), 3)

    def test_exhausted_attempts_raise_the_last_parse_error(self) -> None:
        """
        Test that when the attempts run out on parse failures, the error
        of the last response is raised as-is, with that response
        attached.
        """
        client, inner = self.sync_client(html(), html(), html(), pong())
        with self.assertRaises(PayloadError) as raised:
            client.send(Ping())
        self.assertEqual(len(inner.requests), 3)
        last = raised.exception.response
        assert last is not None
        self.assertIs(last.request, inner.requests[2])

    def test_only_the_named_errors_are_retried(self) -> None:
        """
        Test that naming PayloadError does not retry unexpected
        statuses (an APIError from ``check``), and that an operation's
        own exception types can be named.
        """
        client, inner = self.sync_client(Response(404), pong())
        with self.assertRaises(APIError):
            client.send(Ping())
        self.assertEqual(len(inner.requests), 1)

        class Strict(JsonOperation[Any]):
            path = "/ping"

            def load_json(self, data: Any) -> Any:
                return data["pong"]

        policy = RetryPolicy(attempts=3, backoff=0, retry_parse_errors=(KeyError,))
        client, inner = self.sync_client(Response(200, body="{}"), pong(), policy=policy)
        self.assertIs(client.send(Strict()), True)
        self.assertEqual(len(inner.requests), 2)

    def test_method_gate_applies_to_parse_retries(self) -> None:
        """
        Test that a non-idempotent request is not sent again because of
        a parse failure — unless it declares itself safe to retry.
        """
        client, inner = self.sync_client(html(), pong())
        with self.assertRaises(PayloadError):
            client.send(CreateOrder())
        self.assertEqual(len(inner.requests), 1)

        client, inner = self.sync_client(html(), Response(200, body='{"text": "hallo"}'))
        self.assertEqual(client.send(Translate()), {"text": "hallo"})
        self.assertEqual(len(inner.requests), 2)

    def test_raw_client_has_no_parse_step(self) -> None:
        """
        Test that a request sent through the raw Client is never judged
        by its body: there is no operation to parse it.
        """
        inner = StubBackend(html(), pong())
        client = Client(RetryingSyncBackend(inner, self.policy))
        response = client.send(Request(f"{self.BASE_URL}/ping"))
        self.assertEqual(response.body_str(), "<html>busy</html>")
        self.assertEqual(len(inner.requests), 1)

    async def test_async(self) -> None:
        """
        Test parse retries on an async backend.
        """
        inner = AsyncStubBackend(html(), html(), pong())
        client = APIClient(RetryingAsyncBackend(inner, self.policy), self.BASE_URL)
        self.assertEqual(await client.send(CountingPing()), {"pong": True})
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(CountingPing.loads, [200, 200, 200])

    def test_deferred(self) -> None:
        """
        Test parse retries on a Twisted backend.
        """
        clock = Clock()
        inner = DeferredStubBackend(html(), html(), pong())
        backend = RetryingDeferredBackend(inner, self.policy, reactor=clock)
        deferred = APIClient(backend, self.BASE_URL).send(CountingPing())
        clock.pump([0, 0])
        self.assertEqual(deferred_result(deferred), {"pong": True})
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(CountingPing.loads, [200, 200, 200])

    def test_futures(self) -> None:
        """
        Test parse retries through the thread-pool backend.
        """
        inner = StubBackend(html(), html(), pong())
        with ThreadPoolBackend(RetryingSyncBackend(inner, self.policy)) as backend:
            client = APIClient(backend, self.BASE_URL)
            self.assertEqual(client.send(CountingPing()).result(timeout=5), {"pong": True})
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(CountingPing.loads, [200, 200, 200])

    def test_unparseable_response_is_not_cached(self) -> None:
        """
        Test that a caching backend does not store a response the
        operation cannot parse — the HTML page on a 200 must not be
        served from the cache for a whole ttl.
        """
        inner = StubBackend(html(), pong())
        client = APIClient(CachingSyncBackend(inner), self.BASE_URL)
        with self.assertRaises(PayloadError):
            client.send(Ping())
        self.assertEqual(client.send(Ping()), {"pong": True})  # the network again
        self.assertEqual(client.send(Ping()), {"pong": True})  # now cached
        self.assertEqual(len(inner.requests), 2)

    def test_cache_around_a_parse_retrying_backend(self) -> None:
        """
        Test the full stack cache → retry → backend: the retry replaces
        the bad response, the cache stores the good one.
        """
        inner = StubBackend(html(), pong())
        backend = CachingSyncBackend(RetryingSyncBackend(inner, self.policy))
        client = APIClient(backend, self.BASE_URL)
        self.assertEqual(client.send(Ping()), {"pong": True})
        self.assertEqual(client.send(Ping()), {"pong": True})
        self.assertEqual(len(inner.requests), 2)


class ExhaustedStatusTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for running out of attempts on a retryable status: the last
    response still reaches ``Operation.check``, which raises an APIError
    with that response attached — in every execution model
    """

    BASE_URL = "https://api.example.com"

    def setUp(self) -> None:
        self.policy = RetryPolicy(attempts=3, backoff=0)
        self.responses = [Response(503, body=f"attempt {number}") for number in (1, 2, 3, 4)]

    def check(self, error: BaseException, sent: int) -> None:
        """
        Assert that the error is the APIError for the last response.

        :param error: the exception the send ended with
        :param sent: how many requests reached the stub
        """
        assert isinstance(error, APIError)
        self.assertEqual(sent, 3)
        last = error.response
        assert last is not None
        self.assertEqual(last.status, 503)
        self.assertEqual(last.body_str(), "attempt 3")

    def test_sync(self) -> None:
        """
        Test the synchronous model.
        """
        inner = StubBackend(*self.responses)
        client = APIClient(RetryingSyncBackend(inner, self.policy), self.BASE_URL)
        with self.assertRaises(APIError) as raised:
            client.send(Ping())
        self.check(raised.exception, len(inner.requests))

    async def test_async(self) -> None:
        """
        Test the asyncio model.
        """
        inner = AsyncStubBackend(*self.responses)
        client = APIClient(RetryingAsyncBackend(inner, self.policy), self.BASE_URL)
        with self.assertRaises(APIError) as raised:
            await client.send(Ping())
        self.check(raised.exception, len(inner.requests))

    def test_deferred(self) -> None:
        """
        Test the Twisted model.
        """
        clock = Clock()
        inner = DeferredStubBackend(*self.responses)
        backend = RetryingDeferredBackend(inner, self.policy, reactor=clock)
        deferred = APIClient(backend, self.BASE_URL).send(Ping())
        clock.pump([0, 0])
        with self.assertRaises(APIError) as raised:
            deferred_result(deferred)
        self.check(raised.exception, len(inner.requests))

    def test_futures(self) -> None:
        """
        Test the thread-pool model.
        """
        inner = StubBackend(*self.responses)
        with ThreadPoolBackend(RetryingSyncBackend(inner, self.policy)) as backend:
            future = APIClient(backend, self.BASE_URL).send(Ping())
            with self.assertRaises(APIError) as raised:
                future.result(timeout=5)
        self.check(raised.exception, len(inner.requests))


class NestedRetryTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for stacked retrying wrappers: the outermost one retries, the
    inner ones pass its attempts through — attempts never multiply
    """

    def setUp(self) -> None:
        self.policy = RetryPolicy(attempts=3, backoff=0)
        self.request = Request("https://api.example.com/ping")

    def test_sync_attempts_do_not_multiply(self) -> None:
        """
        Test that two stacked wrappers with three attempts each send
        three times, not nine — and that the request can be sent again
        afterwards with a fresh budget.
        """
        inner = StubBackend(Response(503))
        backend = RetryingSyncBackend(RetryingSyncBackend(inner, self.policy), self.policy)
        self.assertEqual(backend.send(self.request).status, 503)
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(backend.send(self.request).status, 503)
        self.assertEqual(len(inner.requests), 6)
        self.assertEqual(self.request.meta, {})

    def test_outer_policy_decides(self) -> None:
        """
        Test which policy applies: the outermost wrapper's — an inner
        wrapper that would retry does not when the outer one gives up.
        """
        inner = StubBackend(Response(503), Response(200))
        outer_policy = RetryPolicy(attempts=1)
        backend = RetryingSyncBackend(RetryingSyncBackend(inner, self.policy), outer_policy)
        self.assertEqual(backend.send(self.request).status, 503)
        self.assertEqual(len(inner.requests), 1)

    def test_request_is_released_after_an_error(self) -> None:
        """
        Test that a send ending in an exception leaves no mark on the
        request either.
        """

        def explode(request: Request) -> Response:
            raise TransportError("reset")

        inner = StubBackend(explode)
        backend = RetryingSyncBackend(RetryingSyncBackend(inner, self.policy), self.policy)
        with self.assertRaises(TransportError):
            backend.send(self.request)
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(self.request.meta, {})

    async def test_async_attempts_do_not_multiply(self) -> None:
        """
        Test the same on async wrappers.
        """
        inner = AsyncStubBackend(Response(503))
        backend = RetryingAsyncBackend(RetryingAsyncBackend(inner, self.policy), self.policy)
        self.assertEqual((await backend.send(self.request)).status, 503)
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(self.request.meta, {})

    def test_deferred_attempts_do_not_multiply(self) -> None:
        """
        Test the same on Twisted wrappers.
        """
        clock = Clock()
        inner = DeferredStubBackend(Response(503))
        backend = RetryingDeferredBackend(
            RetryingDeferredBackend(inner, self.policy, reactor=clock), self.policy, reactor=clock
        )
        deferred = backend.send(self.request)
        clock.pump([0, 0])
        self.assertEqual(deferred_result(deferred).status, 503)
        self.assertEqual(len(inner.requests), 3)
        self.assertEqual(self.request.meta, {})


class DiscardedResponseTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for what happens to a response a retry throws away: it is
    closed, so a streamed body releases its connection right away
    """

    def setUp(self) -> None:
        self.policy = RetryPolicy(attempts=3, backoff=0)
        self.closed: list[int] = []
        self.request = Request("https://api.example.com/feed")

    def streamed(self, status: int, number: int) -> Response:
        """
        A response with a streamed body that records being closed.

        :param status: the response status
        :param number: the number the close callback records
        :return: the response
        """
        body = IterableBody(iter([b"data"]), close=lambda: self.closed.append(number))
        return Response(status, body=body)

    def test_sync(self) -> None:
        """
        Test that the rejected responses are closed and the returned one
        is not.
        """
        inner = StubBackend(self.streamed(503, 1), self.streamed(503, 2), self.streamed(200, 3))
        response = RetryingSyncBackend(inner, self.policy).send(self.request)
        self.assertEqual(self.closed, [1, 2])
        self.assertEqual(response.body_bytes(), b"data")

    def test_last_response_is_left_open(self) -> None:
        """
        Test that the response of the last attempt is handed out
        untouched, although its status is one the policy retries.
        """
        inner = StubBackend(*(self.streamed(503, number) for number in (1, 2, 3)))
        response = RetryingSyncBackend(inner, self.policy).send(self.request)
        self.assertEqual(self.closed, [1, 2])
        self.assertEqual(response.status, 503)
        self.assertEqual(response.body_bytes(), b"data")

    async def test_async(self) -> None:
        """
        Test that an async streamed body of a rejected response is
        closed with its ``aclose``.
        """

        async def chunks() -> AsyncIterator[bytes]:
            yield b"data"

        async def release() -> None:
            self.closed.append(1)

        rejected = Response(503, body=AsyncIterableBody(chunks(), aclose=release))
        inner = AsyncStubBackend(rejected, Response(200))
        response = await RetryingAsyncBackend(inner, self.policy).send(self.request)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.closed, [1])

    def test_deferred(self) -> None:
        """
        Test the same on the Twisted wrapper.
        """
        clock = Clock()
        inner = DeferredStubBackend(self.streamed(503, 1), Response(200))
        deferred = RetryingDeferredBackend(inner, self.policy, reactor=clock).send(self.request)
        clock.pump([0])
        self.assertEqual(deferred_result(deferred).status, 200)
        self.assertEqual(self.closed, [1])

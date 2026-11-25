import asyncio
import socket
import unittest
from typing import Any

from action0.client import APIClient
from action0.client import BodyMode
from action0.client import Hook
from action0.client import JsonOperation
from action0.client import RequestOptions
from action0.client import RetryPolicy
from action0.client import TimeoutError
from action0.client import TransportError
from action0.client import backend as backend_module
from action0.client import path_param
from action0.client.backends.urllib import UrllibBackend
from action0.client.caching import CachingSyncBackend
from action0.client.retry import RetryingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import ChunkedBody
from action0.client.testing import MockRouter
from action0.client.testing import NetworkBlockedError
from action0.client.testing import StubBackend
from action0.client.testing import UnmockedRequestError
from action0.req import AsyncIterableBody
from action0.req import IterableBody
from action0.req import Request
from action0.req import Response
from action0.req import SpooledBody

API = "https://api.example.com"


class GetItem(JsonOperation[Any]):
    """The example operation."""

    path = "/items/{item_id}"
    item_id: int = path_param()


def get(url: str, **kwargs: Any) -> Request:
    """A GET request."""
    return Request(url, **kwargs)


class RouterTestCase(unittest.TestCase):
    """
    The shared fixture: a real (stdlib) backend that would hit the
    network, and a router active for the duration of each test.
    """

    assert_all_called = False

    def setUp(self) -> None:
        self.backend = UrllibBackend()
        self.mock = MockRouter(assert_all_called=self.assert_all_called)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def send(self, request: Request) -> Response:
        """Send through the real backend — answered by the router."""
        return self.backend.send(request)

    def status(self, url: str, method: str = "GET", **kwargs: Any) -> int:
        """
        The status a request gets; 0 if no route matches it.
        """
        try:
            return self.send(Request(url, method, **kwargs)).status
        except UnmockedRequestError:
            return 0


class MatchingTestCase(RouterTestCase):
    """
    tests for what a route matches on
    """

    def test_method_and_url(self) -> None:
        """
        Test matching on method plus a full URL: scheme, host (case
        does not matter), port and path all count.
        """
        self.mock.get(f"{API}/items").respond(201)
        self.assertEqual(self.status(f"{API}/items"), 201)
        self.assertEqual(self.status("https://API.Example.com/items"), 201)
        self.assertEqual(self.status(f"{API}/items", "POST"), 0)
        self.assertEqual(self.status("http://api.example.com/v1/items"), 0)
        self.assertEqual(self.status("https://other.example.com/items"), 0)
        self.assertEqual(self.status(f"{API}/items/7"), 0)

    def test_port_counts_only_when_given(self) -> None:
        """
        Test that a URL without a port matches any, one with a port only
        that.
        """
        self.mock.get("https://a.example.com/x").respond(201)
        self.mock.get("https://b.example.com:8443/x").respond(202)
        self.assertEqual(self.status("https://a.example.com:8443/x"), 201)
        self.assertEqual(self.status("https://b.example.com:8443/x"), 202)
        self.assertEqual(self.status("https://b.example.com/x"), 0)

    def test_path_only_matches_every_host(self) -> None:
        """
        Test that a route given just a path does not care about the
        host, and that a bare host URL means its root.
        """
        self.mock.get("/health").respond(204)
        self.mock.get("https://api.example.com").respond(200, text="root")
        self.assertEqual(self.status("https://a.example.com/health"), 204)
        self.assertEqual(self.status("http://b.example.com/health"), 204)
        self.assertEqual(self.send(get("https://api.example.com/")).body_str(), "root")
        self.assertEqual(self.send(get("https://api.example.com")).body_str(), "root")

    def test_path_prefix_regex_and_template(self) -> None:
        """
        Test the other path lookups: a prefix, a regular expression
        (which must match the whole path) and a ``{name}`` template whose
        placeholders match one segment each.
        """
        self.mock.route(path__startswith="/static/").respond(201)
        self.mock.route(path__regex=r"/v\d+/ping").respond(202)
        self.mock.get(f"{API}/items/{{item_id}}/tags/{{tag}}").respond(203)
        self.assertEqual(self.status("https://x.example.com/static/css/site.css"), 201)
        self.assertEqual(self.status("https://x.example.com/v2/ping"), 202)
        self.assertEqual(self.status("https://x.example.com/v2/ping/extra"), 0)
        self.assertEqual(self.status(f"{API}/items/7/tags/new"), 203)
        self.assertEqual(self.status(f"{API}/items/7/8/tags/new"), 0)

    def test_template_placeholders_reach_the_callable(self) -> None:
        """
        Test that a callable answer gets the captured placeholders it
        declares, decoded, as keyword arguments.
        """

        def item(request: Request, item_id: str) -> Response:
            return Response(200, body=f"item {item_id}")

        self.mock.get(f"{API}/items/{{item_id}}").mock(side_effect=item)
        self.assertEqual(self.send(get(f"{API}/items/a%20b")).body_str(), "item a b")

    def test_query_parameters_are_a_subset_match(self) -> None:
        """
        Test that the given parameters must be present — from the URL's
        query string or ``params`` — while others may be there too.
        """
        self.mock.get(f"{API}/items?page=2", params={"tag": ["a", "b"]}).respond(201)
        self.assertEqual(self.status(f"{API}/items?tag=b&page=2&tag=a&sort=name"), 201)
        self.assertEqual(self.status(f"{API}/items?page=2&tag=a"), 0)
        self.assertEqual(self.status(f"{API}/items?page=3&tag=a&tag=b"), 0)

    def test_headers_are_a_subset_match(self) -> None:
        """
        Test header lookups: names without regard to case, values
        exactly, other headers ignored.
        """
        self.mock.get(headers={"x-tenant": "one"}).respond(201)
        self.assertEqual(self.status(API, headers={"X-Tenant": "one", "Accept": "*/*"}), 201)
        self.assertEqual(self.status(API, headers={"X-Tenant": "two"}), 0)
        self.assertEqual(self.status(API), 0)

    def test_body_lookups(self) -> None:
        """
        Test matching on the body: exact content, parsed JSON (key order
        and whitespace do not matter) and form data.
        """
        self.mock.post("/raw", content="exact").respond(201)
        self.mock.post("/json", json={"q": "hi", "n": 1}).respond(202)
        self.mock.post("/form", data={"a": "1", "b": ["x", "y"]}).respond(203)
        self.assertEqual(self.status(f"{API}/raw", "POST", body=b"exact"), 201)
        self.assertEqual(self.status(f"{API}/raw", "POST", body=b"other"), 0)
        self.assertEqual(self.status(f"{API}/json", "POST", body='{"n": 1,  "q": "hi"}'), 202)
        self.assertEqual(self.status(f"{API}/json", "POST", body='{"q": "ho", "n": 1}'), 0)
        self.assertEqual(self.status(f"{API}/json", "POST", body="not json"), 0)
        self.assertEqual(self.status(f"{API}/form", "POST", body="b=x&a=1&b=y"), 203)
        self.assertEqual(self.status(f"{API}/form", "POST", body="a=1"), 0)

    def test_first_matching_route_answers(self) -> None:
        """
        Test that routes are tried in the order they were added.
        """
        self.mock.get(f"{API}/items/7").respond(201)
        self.mock.get(f"{API}/items/{{item_id}}").respond(202)
        self.mock.route().respond(203)
        self.assertEqual(self.status(f"{API}/items/7"), 201)
        self.assertEqual(self.status(f"{API}/items/8"), 202)
        self.assertEqual(self.status("https://elsewhere.example.com/"), 203)

    def test_base_url(self) -> None:
        """
        Test that with a base URL a path is relative to it — and so only
        matches that API.
        """
        mock = MockRouter(base_url=API, assert_all_called=False)
        mock.get("/items").respond(201)
        mock.get(path="/health").respond(202)
        with mock:
            self.assertEqual(self.status(f"{API}/items"), 201)
            self.assertEqual(self.status(f"{API}/health"), 202)
            self.assertEqual(self.status("https://other.example.com/items"), 0)
            mock.reset()

    def test_unknown_lookup_is_refused(self) -> None:
        """
        Test that a misspelled lookup fails when the route is defined.
        """
        with self.assertRaisesRegex(TypeError, "path__endswith"):
            self.mock.get(path__endswith=".json")

    def test_named_routes(self) -> None:
        """
        Test finding a route again by its name.
        """
        route = self.mock.route("GET", "/health", name="health")
        self.assertIs(self.mock["health"], route)
        self.assertEqual(self.mock.routes, [route])
        with self.assertRaises(KeyError):
            self.mock["missing"]


class AnswerTestCase(RouterTestCase):
    """
    tests for what a route answers with
    """

    def test_respond_builds_the_response(self) -> None:
        """
        Test the body forms of respond() and the content types they set.
        """
        self.mock.get("/json").respond(json={"id": 7}, headers={"X-Extra": "1"})
        self.mock.get("/text").respond(202, text="plain")
        self.mock.get("/html").respond(html="<p>hi</p>")
        self.mock.get("/bytes").respond(content=b"\x00\x01", reason="Fine")
        self.mock.get("/null").respond(json=None)
        self.mock.get("/default")

        answer = self.send(get(f"{API}/json"))
        self.assertEqual(answer.body_str(), '{"id": 7}')
        self.assertEqual(answer.headers["Content-Type"], "application/json")
        self.assertEqual(answer.headers["X-Extra"], "1")
        text = self.send(get(f"{API}/text"))
        self.assertEqual((text.status, text.body_str()), (202, "plain"))
        self.assertTrue(text.headers["Content-Type"].startswith("text/plain"))
        self.assertTrue(
            self.send(get(f"{API}/html")).headers["Content-Type"].startswith("text/html")
        )
        raw = self.send(get(f"{API}/bytes"))
        self.assertEqual((raw.body, raw.phrase), (b"\x00\x01", "Fine"))
        self.assertEqual(self.send(get(f"{API}/null")).body_str(), "null")
        default = self.send(get(f"{API}/default"))
        self.assertEqual((default.status, default.body), (200, None))

    def test_respond_takes_one_body(self) -> None:
        """
        Test that two body forms at once are refused.
        """
        with self.assertRaisesRegex(TypeError, "json"):
            self.mock.get("/x").respond(text="a", json={})

    def test_fixed_response_is_copied_per_request(self) -> None:
        """
        Test that every request gets its own response object with its
        own request attached — changing one does not affect the next.
        """
        canned = Response(200, headers={"X-A": "1"}, body="canned")
        self.mock.get("/x").mock(return_value=canned)
        first_request, second_request = get(f"{API}/x"), get(f"{API}/x")
        first = self.send(first_request)
        first.headers["X-A"] = "changed"
        second = self.send(second_request)
        self.assertEqual(second.headers["X-A"], "1")
        self.assertIs(first.request, first_request)
        self.assertIs(second.request, second_request)
        self.assertIsNone(canned.request)

    def test_side_effect_raises_like_the_network(self) -> None:
        """
        Test that the library's own errors — given as instance or class —
        are raised from send like real ones, hooks included.
        """
        seen: list[BaseException] = []

        class Watching(Hook):
            def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
                seen.append(error)

        self.backend.hooks.append(Watching())
        self.mock.get("/slow").mock(side_effect=TimeoutError("too slow"))
        self.mock.get("/down").mock(side_effect=TransportError)
        with self.assertRaisesRegex(TimeoutError, "too slow"):
            self.send(get(f"{API}/slow"))
        with self.assertRaises(TransportError):
            self.send(get(f"{API}/down"))
        self.assertEqual([type(error) for error in seen], [TimeoutError, TransportError])

    def test_sequence_goes_through_its_entries_once(self) -> None:
        """
        Test a sequence of answers — exceptions, responses and callables
        mixed — and that a request beyond its end is an error, not a
        silent repeat.
        """
        self.mock.get("/x").mock(
            side_effect=[
                TransportError("reset"),
                Response(503),
                lambda request: Response(200, body="finally"),
            ]
        )
        with self.assertRaises(TransportError):
            self.send(get(f"{API}/x"))
        self.assertEqual(self.send(get(f"{API}/x")).status, 503)
        self.assertEqual(self.send(get(f"{API}/x")).body_str(), "finally")
        with self.assertRaisesRegex(UnmockedRequestError, "used up"):
            self.send(get(f"{API}/x"))
        self.mock.reset()

    def test_callable_may_fall_back_to_the_fixed_response(self) -> None:
        """
        Test that a callable returning ``None`` hands over to
        ``return_value`` — the way to observe requests without answering
        them by hand.
        """
        seen: list[str] = []

        def observe(request: Request) -> None:
            seen.append(request.url.path)

        self.mock.get().mock(return_value=Response(204), side_effect=observe)
        self.assertEqual(self.send(get(f"{API}/a")).status, 204)
        self.assertEqual(seen, ["/a"])

    def test_async_callable_with_a_sync_backend(self) -> None:
        """
        Test that an async callable can answer a synchronous backend as
        long as it does not wait for anything — and that one that does is
        refused with an explanation.
        """

        async def quick(request: Request) -> Response:
            return Response(200, body="async answer")

        async def waiting(request: Request) -> Response:
            await asyncio.sleep(0)
            return Response(200)

        self.mock.get("/quick").mock(side_effect=quick)
        self.mock.get("/waiting").mock(side_effect=waiting)
        self.assertEqual(self.send(get(f"{API}/quick")).body_str(), "async answer")
        with self.assertRaisesRegex(TypeError, "must not wait"):
            self.send(get(f"{API}/waiting"))

    def test_pass_through_reaches_the_backend(self) -> None:
        """
        Test that a pass-through route lets the request go to the
        backend's own send — here a stub's script, with a real backend
        the network.
        """
        stub = StubBackend(Response(200, body="from the stub's script"))
        self.mock.get("/mocked").respond(text="from the router")
        self.mock.get("/real").pass_through()
        self.assertEqual(stub.send(get(f"{API}/mocked")).body_str(), "from the router")
        self.assertEqual(stub.send(get(f"{API}/real")).body_str(), "from the stub's script")
        self.assertEqual(self.mock.calls.call_count, 2)

    def test_unmatched_requests_can_be_let_through(self) -> None:
        """
        Test ``assert_all_mocked=False``: what no route matches goes to
        the backend instead of failing.
        """
        stub = StubBackend(Response(200, body="real"))
        with MockRouter(assert_all_mocked=False) as mock:
            mock.get("/mocked").respond(text="mocked")
            self.assertEqual(stub.send(get(f"{API}/mocked")).body_str(), "mocked")
            self.assertEqual(stub.send(get(f"{API}/other")).body_str(), "real")


class AssertionTestCase(RouterTestCase):
    """
    tests for the call records and the assertions
    """

    def test_calls_are_recorded_per_route_and_router(self) -> None:
        """
        Test call counts, the recorded requests and outcomes.
        """
        items = self.mock.get(f"{API}/items/{{item_id}}").respond(json={"id": 7})
        broken = self.mock.get("/broken").mock(side_effect=TransportError("down"))
        self.assertFalse(items.called)

        client = APIClient(self.backend, API)
        self.assertEqual(client.send(GetItem(item_id=7)), {"id": 7})
        client.send(GetItem(item_id=8))
        with self.assertRaises(TransportError):
            self.send(get(f"{API}/broken"))

        self.assertTrue(items.called)
        self.assertEqual(items.call_count, 2)
        self.assertEqual(items.calls.last.request.url.path, "/items/8")
        last = items.calls.last
        assert last.response is not None
        self.assertEqual(last.response.status, 200)
        self.assertIs(last.route, items)
        self.assertIsInstance(broken.calls.last.exception, TransportError)
        self.assertIsNone(broken.calls.last.response)
        broken.calls.assert_called_once()
        with self.assertRaises(AssertionError):
            items.calls.assert_called_once()
        self.assertEqual([call.route for call in self.mock.calls], [items, items, broken])
        self.assertEqual(repr(items), f"Route(GET {API}/items/{{item_id}}, 2 calls)")

    def test_last_call_of_an_uncalled_route(self) -> None:
        """
        Test that asking for the last call of a route that was never
        called is an assertion failure, not an IndexError.
        """
        route = self.mock.get("/never")
        route.calls.assert_not_called()
        with self.assertRaises(AssertionError):
            route.calls.last

    def test_unmatched_request_fails_loudly(self) -> None:
        """
        Test that a request no route matches raises an AssertionError
        subclass that lists the routes — and is neither translated nor
        retried as a network problem.
        """
        self.mock.get(f"{API}/items").respond()
        retrying = RetryingSyncBackend(self.backend, RetryPolicy(attempts=3, backoff=0))
        with self.assertRaises(UnmockedRequestError) as raised:
            retrying.send(get(f"{API}/typo"))
        self.assertIsInstance(raised.exception, AssertionError)
        self.assertNotIsInstance(raised.exception, TransportError)
        self.assertIn("/typo", str(raised.exception))
        self.assertIn(f"GET {API}/items", str(raised.exception))
        self.assertEqual([request.url.path for request in self.mock.unmatched], ["/typo"])
        self.assertIsNone(self.mock.calls.last.route)
        self.mock.reset()

    def test_swallowed_unmatched_request_fails_the_block(self) -> None:
        """
        Test that an unmocked request the application caught still fails
        the test: leaving the block reports it.
        """
        mock = MockRouter()
        with self.assertRaisesRegex(UnmockedRequestError, "1 request"):
            with mock:
                try:
                    self.backend.send(get(f"{API}/forgotten"))
                except Exception:
                    pass  # the application's catch-all
        self.assertEqual(len(mock.unmatched), 1)

    def test_all_routes_must_be_called(self) -> None:
        """
        Test the check on leaving the block, that it can be turned off,
        and that it stays out of the way of an exception already raised.
        """
        with self.assertRaisesRegex(AssertionError, "never called") as raised:
            with MockRouter() as mock:
                mock.get("/used").respond()
                mock.get("/idle").respond()
                self.backend.send(get(f"{API}/used"))
        self.assertIn("/idle", str(raised.exception))
        self.assertNotIn("/used,", str(raised.exception))

        with MockRouter(assert_all_called=False) as relaxed:
            relaxed.get("/idle").respond()

        with self.assertRaises(KeyError):
            with MockRouter() as failing:
                failing.get("/idle").respond()
                raise KeyError("the test's own failure")

    def test_reset_keeps_the_routes(self) -> None:
        """
        Test that reset() forgets the calls and keeps the answers.
        """
        route = self.mock.get("/x").respond(201)
        self.send(get(f"{API}/x"))
        self.mock.reset()
        self.assertEqual((route.call_count, self.mock.calls.call_count), (0, 0))
        self.assertEqual(self.send(get(f"{API}/x")).status, 201)


class ActivationTestCase(unittest.TestCase):
    """
    tests for switching the router on and off
    """

    def test_inactive_unless_asked_for(self) -> None:
        """
        Test that nothing is mocked by default, that the switch is on
        exactly inside the block, and that it is off again afterwards —
        also when the block raises.
        """
        self.assertIsNone(backend_module._mock)
        with MockRouter() as mock:
            self.assertIs(backend_module._mock, mock)
        self.assertIsNone(backend_module._mock)
        with self.assertRaises(KeyError):
            with MockRouter():
                raise KeyError("boom")
        self.assertIsNone(backend_module._mock)

    def test_routers_nest(self) -> None:
        """
        Test that the router started last answers, and the outer one
        again once the inner is left.
        """
        backend = UrllibBackend()
        with MockRouter() as outer:
            outer.get().respond(201)
            self.assertEqual(backend.send(get(API)).status, 201)
            with MockRouter() as inner:
                inner.get().respond(202)
                self.assertEqual(backend.send(get(API)).status, 202)
            self.assertEqual(backend.send(get(API)).status, 201)
        self.assertEqual((outer.calls.call_count, inner.calls.call_count), (2, 1))

    def test_start_and_stop(self) -> None:
        """
        Test the explicit form, and that stopping twice is harmless.
        """
        mock = MockRouter()
        mock.start()
        try:
            self.assertIs(backend_module._mock, mock)
        finally:
            mock.stop()
        mock.stop()
        self.assertIsNone(backend_module._mock)

    def test_stub_backends_are_answered_too(self) -> None:
        """
        Test that an active router takes over an injected stub as well —
        one way of mocking for the whole test — while the stub's own
        router and script are untouched.
        """
        stub = StubBackend(Response(200, body="script"))
        with MockRouter() as mock:
            mock.get().respond(text="router")
            self.assertEqual(stub.send(get(API)).body_str(), "router")
        self.assertEqual(stub.requests, [])
        self.assertEqual(stub.send(get(API)).body_str(), "script")

    def test_wrappers_run_around_the_router(self) -> None:
        """
        Test that retrying and caching backends work as usual on mocked
        sends: the retry sends again after a mocked failure, and a cache
        hit never reaches the router.
        """
        backend = CachingSyncBackend(
            RetryingSyncBackend(UrllibBackend(), RetryPolicy(attempts=3, backoff=0))
        )
        with MockRouter() as mock:
            route = mock.get(f"{API}/rates").mock(
                side_effect=[TransportError("reset"), Response(503), Response(200, body="rates")]
            )
            self.assertEqual(backend.send(get(f"{API}/rates")).body_str(), "rates")
            self.assertEqual(route.call_count, 3)
            self.assertEqual(backend.send(get(f"{API}/rates")).body_str(), "rates")
            self.assertEqual(route.call_count, 3)

    def test_hooks_see_mocked_traffic(self) -> None:
        """
        Test that the backend's hooks run for mocked requests: they can
        change the request the router sees and the response it gave.
        """

        class Stamping(Hook):
            def on_request(self, request: Request) -> Request:
                request.headers["X-Stamp"] = "hook"
                return request

            def on_response(
                self, request: Request, response: Response, elapsed: float
            ) -> Response:
                response.headers["X-Seen"] = "hook"
                return response

        backend = UrllibBackend(hooks=[Stamping()])
        with MockRouter() as mock:
            route = mock.get(headers={"X-Stamp": "hook"}).respond(204)
            response = backend.send(get(API))
        self.assertEqual(response.headers["X-Seen"], "hook")
        self.assertEqual(route.calls.last.request.headers["X-Stamp"], "hook")


class BodyModeTestCase(RouterTestCase):
    """
    tests for mocked responses in the body mode the request asks for
    """

    def request(self, mode: BodyMode, path: str = "/feed", **options: Any) -> Request:
        """A request asking for a body mode."""
        return RequestOptions(body=mode, **options).apply(get(f"{API}{path}"))

    def test_fixed_body_in_each_mode(self) -> None:
        """
        Test that the same mocked bytes arrive as bytes, as a
        single-use stream or as a spool, depending on the request.
        """
        self.mock.get("/feed").respond(content=b"mocked feed")
        self.assertEqual(self.send(get(f"{API}/feed")).body, b"mocked feed")
        self.assertEqual(self.send(self.request(BodyMode.PRELOAD)).body, b"mocked feed")

        streamed = self.send(self.request(BodyMode.STREAM))
        self.assertIsInstance(streamed.body, IterableBody)
        self.assertEqual(streamed.body_bytes(), b"mocked feed")

        with self.send(self.request(BodyMode.SPOOL, spool_max_memory=4)) as spooled:
            body = spooled.body
            assert isinstance(body, SpooledBody)
            self.assertTrue(body.rolled)
            file = spooled.body_file()
            assert file is not None
            self.assertEqual(file.read(), b"mocked feed")

    def test_backend_default_counts(self) -> None:
        """
        Test that a backend constructed with ``stream=True`` gets its
        mocked bodies streamed, like its real ones.
        """
        self.mock.get("/feed").respond(content=b"mocked feed")
        response = UrllibBackend(stream=True).send(get(f"{API}/feed"))
        self.assertIsInstance(response.body, IterableBody)

    def test_chunks_arrive_as_given(self) -> None:
        """
        Test a body mocked in chunks: a streamed read sees the chunk
        boundaries, the other modes the joined bytes.
        """
        self.mock.get("/feed").respond(chunks=[b"alpha\n", b"beta\n"])
        streamed = self.send(self.request(BodyMode.STREAM))
        producer = streamed.body_producer()
        assert producer is not None
        self.assertEqual(list(producer.chunks()), [b"alpha\n", b"beta\n"])
        self.assertEqual(self.send(get(f"{API}/feed")).body, b"alpha\nbeta\n")
        with self.send(self.request(BodyMode.SPOOL)) as spooled:
            self.assertEqual(spooled.body_bytes(), b"alpha\nbeta\n")

    def test_body_breaking_off(self) -> None:
        """
        Test a mocked body that fails midway: a streamed read gets the
        chunks before the break and then the error; a preloaded or
        spooled send fails as a whole.
        """
        self.mock.get("/feed").respond(chunks=[b"first", TransportError("connection lost")])

        streamed = self.send(self.request(BodyMode.STREAM))
        self.assertEqual(streamed.status, 200)
        producer = streamed.body_producer()
        assert producer is not None
        chunks = producer.chunks()
        self.assertEqual(next(chunks), b"first")
        with self.assertRaisesRegex(TransportError, "connection lost"):
            next(chunks)

        for mode in (BodyMode.PRELOAD, BodyMode.SPOOL):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(TransportError, "connection lost"):
                    self.send(self.request(mode))

    def test_resume_logic_against_a_mocked_download(self) -> None:
        """
        Test the use the broken body exists for: download code that
        resumes with a Range request, run entirely against the router.
        """
        payload = b"0123456789" * 10

        def download(request: Request) -> Response:
            requested = request.headers.get("Range")
            if requested is None:
                return Response(200, body=ChunkedBody(payload[:30], TransportError("reset")))
            start = int(requested.removeprefix("bytes=").rstrip("-"))
            headers = {"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"}
            return Response(206, headers=headers, body=ChunkedBody(payload[start:]))

        route = self.mock.get("/feed").mock(side_effect=download)

        received = bytearray()
        for _ in range(3):
            request = self.request(BodyMode.STREAM)
            if received:
                request.headers["Range"] = f"bytes={len(received)}-"
            try:
                with self.send(request) as response:
                    producer = response.body_producer()
                    assert producer is not None
                    for chunk in producer.chunks():
                        received += chunk
                break
            except TransportError:
                continue
        self.assertEqual(bytes(received), payload)
        self.assertEqual(route.call_count, 2)
        self.assertEqual(route.calls.last.request.headers["Range"], "bytes=30-")

    def test_producer_bodies_are_left_alone(self) -> None:
        """
        Test that a response mocked with a body producer of its own is
        handed out with exactly that producer.
        """
        body = IterableBody([b"own", b"producer"])
        self.mock.get("/feed").mock(return_value=Response(200, body=body))
        self.assertIs(self.send(get(f"{API}/feed")).body, body)


class AsyncRouterTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for the router answering async backends
    """

    async def test_async_callable_is_awaited(self) -> None:
        """
        Test that an async callable answering an async backend may
        really wait.
        """

        async def slow(request: Request) -> Response:
            await asyncio.sleep(0)
            return Response(200, body="awaited")

        backend = AsyncStubBackend()
        with MockRouter() as mock:
            mock.get().mock(side_effect=slow)
            self.assertEqual((await backend.send(get(API))).body_str(), "awaited")

    async def test_body_modes(self) -> None:
        """
        Test that a streamed mocked body is an async producer on an
        async backend, breaking off where the test says, and that a
        spooled one is a spool.
        """
        backend = AsyncStubBackend()
        with MockRouter() as mock:
            mock.get("/feed").respond(chunks=[b"first", TransportError("connection lost")])
            mock.get("/whole").respond(content=b"whole")

            request = RequestOptions(body=BodyMode.STREAM).apply(get(f"{API}/feed"))
            body = (await backend.send(request)).body
            assert isinstance(body, AsyncIterableBody)
            received = []
            with self.assertRaises(TransportError):
                async for chunk in body.achunks():
                    received.append(chunk)
            self.assertEqual(received, [b"first"])

            spooling = RequestOptions(body=BodyMode.SPOOL).apply(get(f"{API}/whole"))
            async with await backend.send(spooling) as spooled:
                self.assertIsInstance(spooled.body, SpooledBody)
                self.assertEqual(spooled.body_bytes(), b"whole")

    async def test_concurrent_requests_are_matched_not_ordered(self) -> None:
        """
        Test what an ordered script cannot do: concurrent requests each
        get the answer of their own route, whatever order they run in.
        """
        backend = AsyncStubBackend()
        with MockRouter() as mock:
            for number in range(20):
                mock.get(f"{API}/items/{number}").respond(json={"id": number})
            client = APIClient(backend, API)
            order = list(range(20))[::-1]
            results = await asyncio.gather(*(client.send(GetItem(item_id=n)) for n in order))
        self.assertEqual([result["id"] for result in results], order)


class BlockNetworkTestCase(unittest.TestCase):
    """
    tests for ``block_network``: no connection is opened while the
    router is active
    """

    def setUp(self) -> None:
        # a listening socket that would accept the connection
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.addCleanup(self.server.close)
        self.address = self.server.getsockname()

    def connect(self) -> None:
        """Open (and close) a connection to the local server."""
        socket.create_connection(self.address, timeout=5).close()

    def test_connections_are_refused_while_active(self) -> None:
        """
        Test that a connection attempt fails loudly while the router is
        active, fails the block even if the error was swallowed, and
        works again afterwards.
        """
        self.connect()
        with self.assertRaisesRegex(NetworkBlockedError, "went around the router"):
            with MockRouter(block_network=True):
                with self.assertRaises(NetworkBlockedError):
                    self.connect()
        self.connect()

    def test_pass_through_cannot_reach_the_network(self) -> None:
        """
        Test the combination with a real backend: even a request that
        is let through to the backend does not get out.
        """
        backend = UrllibBackend(timeout=5)
        url = f"http://127.0.0.1:{self.address[1]}/"
        with self.assertRaisesRegex(NetworkBlockedError, "went around the router"):
            with MockRouter(block_network=True, assert_all_mocked=False):
                # the library sees a failed connect and reports it its
                # way; the cause is still spelled out
                with self.assertRaisesRegex(TransportError, "block_network=True"):
                    backend.send(get(url))

    def test_unblocked_by_default(self) -> None:
        """
        Test that a plain router leaves sockets alone.
        """
        with MockRouter():
            self.connect()


class StubRouterTestCase(unittest.TestCase):
    """
    tests for the router every stub backend owns
    """

    def test_routes_take_precedence_over_the_script(self) -> None:
        """
        Test that routes on the stub's router answer what they match,
        the script everything else, and that both are recorded.
        """
        backend = StubBackend(Response(404))
        health = backend.router.get("/health").respond(200, text="fine")
        self.assertEqual(backend.send(get(f"{API}/health")).body_str(), "fine")
        self.assertEqual(backend.send(get(f"{API}/other")).status, 404)
        self.assertEqual(health.call_count, 1)
        self.assertEqual([request.url.path for request in backend.requests], ["/health", "/other"])
        self.assertEqual(repr(backend), "StubBackend(2 requests)")
        self.assertIsNone(backend_module._mock)  # a stub's router is private

    def test_scripted_body_follows_the_requested_mode(self) -> None:
        """
        Test that a stub's canned bytes are delivered in the body mode
        the request asks for, like a mocked route's.
        """
        backend = StubBackend(Response(200, body=b"scripted"))
        request = RequestOptions(body=BodyMode.SPOOL).apply(get(API))
        with backend.send(request) as response:
            self.assertIsInstance(response.body, SpooledBody)
            self.assertEqual(response.body_bytes(), b"scripted")
        self.assertEqual(backend.send(get(API)).body, b"scripted")

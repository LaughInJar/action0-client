import unittest
from typing import Any

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import query
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response
from action0.url import Url


class Ping(JsonOperation[Any]):
    """The example operation used throughout these tests."""

    path = "/ping"
    q: "str | None" = query(default=None)


class APIClientTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.api.APIClient`
    """

    def test_send_builds_sends_and_parses(self) -> None:
        """
        Test the full pipeline: request building, backend send, parsing.
        """
        backend = StubBackend(Response(200, body='{"pong": true}'))
        client = APIClient(backend, "https://api.example.com/v1")

        result = client.send(Ping(q="x"))

        self.assertEqual(result, {"pong": True})
        self.assertEqual(backend.requests[0].url.as_str(), "https://api.example.com/v1/ping?q=x")

    def test_default_headers_fill_gaps_only(self) -> None:
        """
        Test that client headers apply where the operation sets nothing,
        and lose against operation headers.
        """

        class WithAccept(JsonOperation[Any]):
            path = "/ping"

        backend = StubBackend(Response(200, body="{}"))
        client = APIClient(
            backend,
            "https://api.example.com",
            headers={"Authorization": "Bearer token", "Accept": "text/csv"},
        )
        client.send(WithAccept())

        sent = backend.requests[0].headers
        self.assertEqual(sent["Authorization"], "Bearer token")
        # the operation's own Accept (application/json) wins
        self.assertEqual(sent["Accept"], "application/json")

    def test_prepare_can_be_overridden(self) -> None:
        """
        Test the per-request extension point (e.g. signing).
        """

        class Signing(APIClient[StubBackend]):
            def prepare(self, request: Request) -> Request:
                request = super().prepare(request)
                request.headers["X-Signature"] = f"sig({request.url.path})"
                return request

        backend = StubBackend(Response(200, body="{}"))
        Signing(backend, "https://api.example.com").send(Ping())
        self.assertEqual(backend.requests[0].headers["X-Signature"], "sig(/ping)")

    def test_base_url_is_copied(self) -> None:
        """
        Test that neither a passed Url nor later sends leak mutations.
        """
        base = Url("https://api.example.com/v1")
        client = APIClient(StubBackend(Response(200, body="{}")), base)
        client.send(Ping(q="x"))
        client.send(Ping(q="y"))

        self.assertEqual(base.as_str(), "https://api.example.com/v1")
        self.assertEqual(client.base_url.as_str(), "https://api.example.com/v1")

    def test_backend_property(self) -> None:
        """
        Test that the backend stays reachable (e.g. for closing it).
        """
        backend = StubBackend()
        self.assertIs(APIClient(backend, "https://api.example.com").backend, backend)

    def test_repr(self) -> None:
        """
        Test the debugging representation.
        """
        client = APIClient(StubBackend(), "https://api.example.com/v1")
        self.assertEqual(
            repr(client), "APIClient(https://api.example.com/v1 via StubBackend(0 requests))"
        )


class AsyncAPIClientTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.api.APIClient` with an async backend
    """

    async def test_send_is_awaitable_and_parsed(self) -> None:
        """
        Test that the awaited result is the parsed payload.
        """
        client = APIClient(
            AsyncStubBackend(Response(200, body='{"pong": true}')), "https://api.example.com"
        )
        self.assertEqual(await client.send(Ping()), {"pong": True})


class DeferredAPIClientTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.api.APIClient` with a Deferred backend
    """

    def test_send_returns_a_deferred_of_the_parsed_result(self) -> None:
        """
        Test that the Deferred fires with the parsed payload.
        """
        client = APIClient(
            DeferredStubBackend(Response(200, body='{"pong": true}')), "https://api.example.com"
        )
        self.assertEqual(deferred_result(client.send(Ping())), {"pong": True})

    def test_parse_errors_arrive_in_the_errback(self) -> None:
        """
        Test that an unexpected status surfaces as a failed Deferred, not
        a synchronous raise.
        """
        from action0.client import APIError

        client = APIClient(DeferredStubBackend(Response(500)), "https://api.example.com")
        deferred = client.send(Ping())
        with self.assertRaises(APIError):
            deferred_result(deferred)

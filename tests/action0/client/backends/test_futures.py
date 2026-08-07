import unittest
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from action0.client import APIClient
from action0.client import APIError
from action0.client import Client
from action0.client import JsonOperation
from action0.client import query
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.testing import StubBackend
from action0.req import Request
from action0.req import Response


class Ping(JsonOperation[Any]):
    """The example operation used in these tests."""

    path = "/ping"
    q: "str | None" = query(default=None)


class ThreadPoolBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.futures.ThreadPoolBackend`
    """

    def test_send_returns_a_future_of_the_response(self) -> None:
        """
        Test the basic round trip through the pool.
        """
        inner = StubBackend(Response(201))
        with ThreadPoolBackend(inner) as backend:
            future = backend.send(Request("https://example.com/"))
            self.assertIsInstance(future, Future)
            self.assertEqual(future.result(timeout=5).status, 201)
        self.assertEqual(len(inner.requests), 1)

    def test_sends_run_in_parallel(self) -> None:
        """
        Test that many sends are in flight at once (the whole point).
        """
        import threading

        gate = threading.Barrier(4, timeout=5)

        def responder(request: Request) -> Response:
            gate.wait()  # only passes if all four sends run concurrently
            return Response(200)

        with ThreadPoolBackend(StubBackend(responder), max_workers=4) as backend:
            futures = [backend.send(Request("https://example.com/")) for _ in range(4)]
            statuses = [future.result(timeout=5).status for future in futures]
        self.assertEqual(statuses, [200, 200, 200, 200])

    def test_send_errors_surface_on_the_future(self) -> None:
        """
        Test that inner-backend failures arrive via Future.result().
        """

        def explode(request: Request) -> Response:
            raise ConnectionResetError("nope")

        with ThreadPoolBackend(StubBackend(explode)) as backend:
            future = backend.send(Request("https://example.com/"))
            with self.assertRaises(ConnectionResetError):
                future.result(timeout=5)

    def test_map_chains_the_function(self) -> None:
        """
        Test the Future map: value transformation and error propagation.
        """
        with ThreadPoolBackend(StubBackend(Response(200, body="pong"))) as backend:
            mapped = backend.map(
                backend.send(Request("https://example.com/")),
                lambda response: response.body_str(),
            )
            self.assertEqual(mapped.result(timeout=5), "pong")

            def boom(response: Response) -> str:
                raise ValueError("bad payload")

            failed = backend.map(backend.send(Request("https://example.com/")), boom)
            with self.assertRaises(ValueError):
                failed.result(timeout=5)

    def test_client_and_api_client_integration(self) -> None:
        """
        Test the full pipeline: Client and APIClient over the pool.
        """
        inner = StubBackend(Response(200, body='{"pong": true}'))
        with ThreadPoolBackend(inner) as backend:
            raw = Client(backend).send(Request("https://api.example.com/"))
            self.assertEqual(raw.result(timeout=5).status, 200)

            client = APIClient(backend, "https://api.example.com/v1")
            parsed = client.send(Ping(q="x"))
            self.assertEqual(parsed.result(timeout=5), {"pong": True})
        self.assertEqual(inner.requests[1].url.as_str(), "https://api.example.com/v1/ping?q=x")

    def test_parse_errors_surface_on_the_future(self) -> None:
        """
        Test that operation-level failures arrive via Future.result().
        """
        with ThreadPoolBackend(StubBackend(Response(500))) as backend:
            client = APIClient(backend, "https://api.example.com")
            future = client.send(Ping())
            with self.assertRaises(APIError):
                future.result(timeout=5)

    def test_close_semantics(self) -> None:
        """
        Test that an owned pool is shut down and a passed one is not.
        """
        own_pool = ThreadPoolExecutor(max_workers=1)
        backend = ThreadPoolBackend(StubBackend(), own_pool)
        backend.close()
        # still usable — the backend did not shut down what it doesn't own
        self.assertEqual(own_pool.submit(lambda: 41 + 1).result(timeout=5), 42)
        own_pool.shutdown()

        owned = ThreadPoolBackend(StubBackend())
        owned.close()
        with self.assertRaises(RuntimeError):
            owned.send(Request("https://example.com/"))

    def test_inner_property_and_repr(self) -> None:
        """
        Test that the wrapped backend stays reachable and shows in repr.
        """
        inner = StubBackend()
        with ThreadPoolBackend(inner) as backend:
            self.assertIs(backend.inner, inner)
            self.assertEqual(repr(backend), "ThreadPoolBackend(StubBackend(0 requests))")

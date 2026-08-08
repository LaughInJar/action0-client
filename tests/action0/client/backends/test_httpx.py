import unittest

import httpx

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.req import Request
from action0.req.body import BodyProducer
from action0.req.body import IterableBody


def echo_handler(request: httpx.Request) -> httpx.Response:
    """
    The mock transport endpoint: echoes the request back as headers and
    body, plus repeated Set-Cookie lines.

    :param request: the httpx request the transport received
    :return: the canned response
    """
    return httpx.Response(
        200,
        headers=[
            ("X-Method", request.method),
            ("X-Url", str(request.url)),
            ("X-Tag", request.headers.get("X-Tag", "")),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
        ],
        content=request.read(),
    )


class HttpxBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.backends.httpx.HttpxBackend`
    over an ``httpx.MockTransport``
    """

    def backend(self, handler: "httpx.MockTransport | None" = None) -> HttpxBackend:
        """
        A backend over the echo transport (or a custom one).

        :param handler: the transport to use instead of the echo one
        :return: the ready-made backend
        """
        transport = handler if handler is not None else httpx.MockTransport(echo_handler)
        return HttpxBackend(httpx.Client(transport=transport))

    def test_round_trip_conversion(self) -> None:
        """
        Test request and response conversion in one pass.
        """
        request = Request(
            "https://api.example.com/items",
            "POST",
            query={"page": 2},
            headers={"X-Tag": "yes"},
            body='{"a": 1}',
        )
        response = self.backend().send(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.http_version, "HTTP/1.1")
        self.assertEqual(response.headers["X-Method"], "POST")
        self.assertEqual(response.headers["X-Url"], "https://api.example.com/items?page=2")
        self.assertEqual(response.headers["X-Tag"], "yes")
        self.assertEqual(response.body_bytes(), b'{"a": 1}')
        self.assertIs(response.request, request)

    def test_multi_value_response_headers_are_preserved(self) -> None:
        """
        Test that repeated header lines survive the conversion.
        """
        response = self.backend().send(Request("https://api.example.com/"))
        self.assertEqual(response.headers.get_all("Set-Cookie"), ["a=1", "b=2"])

    def test_streaming_request_body(self) -> None:
        """
        Test that a BodyProducer body is streamed through httpx.
        """
        request = Request(
            "https://api.example.com/upload",
            "PUT",
            body=IterableBody([b"chu", b"nks"]),
        )
        response = self.backend().send(request)
        self.assertEqual(response.body_bytes(), b"chunks")

    def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as a producer and yields the
        same content a preloading send would.
        """
        transport = httpx.MockTransport(echo_handler)
        with HttpxBackend(httpx.Client(transport=transport), stream=True) as backend:
            response = backend.send(Request("https://api.example.com/items", body="ping"))
            self.assertIsInstance(response.body, BodyProducer)
            self.assertEqual(response.body_bytes(), b"ping")

    def test_timeout_is_translated(self) -> None:
        """
        Test that httpx timeouts surface as the library's TimeoutError.
        """

        def sleepy(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        backend = self.backend(httpx.MockTransport(sleepy))
        with self.assertRaises(TimeoutError) as caught:
            backend.send(Request("https://api.example.com/"))
        self.assertIsInstance(caught.exception.__cause__, httpx.ReadTimeout)

    def test_transport_error_is_translated(self) -> None:
        """
        Test that httpx transport failures surface as TransportError.
        """

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        backend = self.backend(httpx.MockTransport(refuse))
        with self.assertRaises(TransportError):
            backend.send(Request("https://api.example.com/"))

    def test_redirects_are_followed_by_created_clients(self) -> None:
        """
        Test the follow_redirects default on the client the backend
        creates for itself.
        """

        def redirect(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(302, headers={"Location": "/new"})
            return httpx.Response(200, content=b"moved in")

        transport = httpx.MockTransport(redirect)
        with HttpxBackend(timeout=1) as backend:
            # swap the transport of the created client, keeping its config
            backend._client._transport = transport
            response = backend.send(Request("https://api.example.com/old"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body_bytes(), b"moved in")

    def test_close_semantics(self) -> None:
        """
        Test that an owned client is closed and a passed one is not.
        """
        client = httpx.Client(transport=httpx.MockTransport(echo_handler))
        backend = HttpxBackend(client)
        backend.close()
        self.assertFalse(client.is_closed)
        client.close()

        owned = HttpxBackend()
        inner = owned._client
        owned.close()
        self.assertTrue(inner.is_closed)


class AsyncHttpxBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.backends.httpx.AsyncHttpxBackend`
    over an ``httpx.MockTransport``
    """

    def backend(self, handler: "httpx.MockTransport | None" = None) -> AsyncHttpxBackend:
        """
        A backend over the echo transport (or a custom one).

        :param handler: the transport to use instead of the echo one
        :return: the ready-made backend
        """
        transport = handler if handler is not None else httpx.MockTransport(echo_handler)
        return AsyncHttpxBackend(httpx.AsyncClient(transport=transport))

    async def test_round_trip_conversion(self) -> None:
        """
        Test the async request/response conversion.
        """
        request = Request("https://api.example.com/items", "POST", body=b"payload")
        response = await self.backend().send(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Method"], "POST")
        self.assertEqual(response.body_bytes(), b"payload")
        self.assertIs(response.request, request)

    async def test_streaming_request_body(self) -> None:
        """
        Test that a BodyProducer body is streamed via the async iterator.
        """
        request = Request(
            "https://api.example.com/upload",
            "PUT",
            body=IterableBody([b"chu", b"nks"]),
        )
        response = await self.backend().send(request)
        self.assertEqual(response.body_bytes(), b"chunks")

    async def test_streamed_response_body(self) -> None:
        """
        Test stream=True: the body arrives as an async producer; the
        chunks stream, the sync accessors refuse.
        """
        transport = httpx.MockTransport(echo_handler)
        client = httpx.AsyncClient(transport=transport)
        async with AsyncHttpxBackend(client, stream=True) as backend:
            response = await backend.send(Request("https://api.example.com/items", body="ping"))
            self.assertIsInstance(response.body, BodyProducer)

            producer = response.body_producer()
            assert producer is not None
            chunks = [chunk async for chunk in producer.achunks()]
            self.assertEqual(b"".join(chunks), b"ping")
            with self.assertRaises(RuntimeError):
                response.body_bytes()  # an async body has no sync view

    async def test_timeout_is_translated(self) -> None:
        """
        Test that httpx timeouts surface as the library's TimeoutError.
        """

        def sleepy(request: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("no connection available", request=request)

        backend = self.backend(httpx.MockTransport(sleepy))
        with self.assertRaises(TimeoutError):
            await backend.send(Request("https://api.example.com/"))

    async def test_close_semantics(self) -> None:
        """
        Test that an owned client is closed and a passed one is not.
        """
        client = httpx.AsyncClient(transport=httpx.MockTransport(echo_handler))
        async with AsyncHttpxBackend(client):
            pass
        self.assertFalse(client.is_closed)
        await client.aclose()

        owned = AsyncHttpxBackend()
        inner = owned._client
        await owned.aclose()
        self.assertTrue(inner.is_closed)

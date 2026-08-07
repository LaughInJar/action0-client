import unittest

from action0.client import Client
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response


class ClientTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.client.Client`
    """

    def test_send_delegates_to_the_backend(self) -> None:
        """
        Test that the request goes through the backend and the response
        comes back.
        """
        backend = StubBackend(Response(201))
        client = Client(backend)
        request = Request("https://example.com/")

        response = client.send(request)

        self.assertEqual(response.status, 201)
        self.assertEqual(backend.requests, [request])

    def test_backend_property(self) -> None:
        """
        Test that the backend stays reachable (e.g. for closing it).
        """
        backend = StubBackend()
        self.assertIs(Client(backend).backend, backend)

    def test_repr_shows_the_backend(self) -> None:
        """
        Test the debugging representation.
        """
        self.assertEqual(repr(Client(StubBackend())), "Client(StubBackend(0 requests))")


class AsyncClientTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.client.Client` with an async backend
    """

    async def test_send_is_awaitable(self) -> None:
        """
        Test that with an async backend the send result awaits to the
        response.
        """
        client = Client(AsyncStubBackend(Response(202)))
        response = await client.send(Request("https://example.com/"))
        self.assertEqual(response.status, 202)


class DeferredClientTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.client.Client` with a Deferred backend
    """

    def test_send_returns_a_deferred(self) -> None:
        """
        Test that with a Twisted backend the send result is a Deferred of
        the response.
        """
        client = Client(DeferredStubBackend(Response(203)))
        response = deferred_result(client.send(Request("https://example.com/")))
        self.assertEqual(response.status, 203)

import builtins
import unittest

from action0.client import APIError
from action0.client import ClientError
from action0.client import TimeoutError
from action0.client import TransportError
from action0.req import Request
from action0.req import Response


class HierarchyTestCase(unittest.TestCase):
    """
    tests for the exception hierarchy in :py:mod:`action0.client.errors`
    """

    def test_transport_error_is_client_error(self) -> None:
        """
        Test that catching ClientError catches transport failures.
        """
        self.assertIsInstance(TransportError("boom"), ClientError)

    def test_api_error_is_client_error(self) -> None:
        """
        Test that catching ClientError catches API failures.
        """
        self.assertIsInstance(APIError("boom"), ClientError)

    def test_timeout_error_is_transport_error(self) -> None:
        """
        Test that a timeout is a transport error.
        """
        self.assertIsInstance(TimeoutError("boom"), TransportError)

    def test_timeout_error_is_builtin_timeout(self) -> None:
        """
        Test that a plain `except TimeoutError` (the built-in) also catches
        the library's timeout.
        """
        self.assertIsInstance(TimeoutError("boom"), builtins.TimeoutError)

    def test_transport_and_api_errors_are_distinct(self) -> None:
        """
        Test that the two failure families don't catch each other.
        """
        self.assertNotIsInstance(TransportError("boom"), APIError)
        self.assertNotIsInstance(APIError("boom"), TransportError)


class AttributesTestCase(unittest.TestCase):
    """
    tests for the request/response attributes on the exceptions
    """

    def test_transport_error_carries_request(self) -> None:
        """
        Test that the failed request stays available on the error.
        """
        request = Request("https://example.com/")
        error = TransportError("boom", request=request)
        self.assertIs(error.request, request)
        self.assertEqual(str(error), "boom")

    def test_transport_error_defaults(self) -> None:
        """
        Test that the request defaults to None.
        """
        self.assertIsNone(TransportError("boom").request)

    def test_api_error_carries_request_and_response(self) -> None:
        """
        Test that the offending response stays available on the error.
        """
        request = Request("https://example.com/")
        response = Response(500)
        error = APIError("boom", request=request, response=response)
        self.assertIs(error.request, request)
        self.assertIs(error.response, response)

    def test_api_error_defaults(self) -> None:
        """
        Test that request and response default to None.
        """
        error = APIError("boom")
        self.assertIsNone(error.request)
        self.assertIsNone(error.response)

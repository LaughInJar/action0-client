import unittest

from action0.client._parse import PARSE_KEY
from action0.client._parse import ParseOnce
from action0.client._parse import parse_failure
from action0.req import Request
from action0.req import Response


class ParseOnceTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client._parse.ParseOnce`
    """

    def setUp(self) -> None:
        self.calls: list[Response] = []

    def _parse(self, response: Response) -> str:
        self.calls.append(response)
        if response.status != 200:
            raise ValueError(f"cannot parse a {response.status}")
        return f"parsed {response.body_str()}"

    def test_parses_once_per_response(self) -> None:
        """
        Test that asking again for the same response returns the first
        result without parsing again.
        """
        parse = ParseOnce(self._parse)
        response = Response(200, body="a")
        self.assertIsNone(parse.failure(response))
        self.assertEqual(parse(response), "parsed a")
        self.assertEqual(parse(response), "parsed a")
        self.assertEqual(len(self.calls), 1)

    def test_failure_is_remembered_and_raised_again(self) -> None:
        """
        Test that a failed parse is reported without raising, and raised
        — the very same exception — when the result is asked for.
        """
        parse = ParseOnce(self._parse)
        response = Response(500)
        error = parse.failure(response)
        self.assertIsInstance(error, ValueError)
        with self.assertRaises(ValueError) as raised:
            parse(response)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(self.calls), 1)

    def test_new_response_is_parsed_anew(self) -> None:
        """
        Test that only the latest response is remembered: another one —
        even an equal one — is parsed again.
        """
        parse = ParseOnce(self._parse)
        self.assertIsNotNone(parse.failure(Response(500)))
        self.assertEqual(parse(Response(200, body="b")), "parsed b")
        self.assertEqual(parse(Response(200, body="b")), "parsed b")
        self.assertEqual(len(self.calls), 3)

    def test_parse_failure_needs_the_hook_on_the_request(self) -> None:
        """
        Test the wrapper-side lookup: a request not sent by an APIClient
        has nothing to parse with, so nothing is reported.
        """
        request = Request("https://api.example.com/")
        self.assertIsNone(parse_failure(request, Response(500)))

        request.meta[PARSE_KEY] = ParseOnce(self._parse)
        self.assertIsInstance(parse_failure(request, Response(500)), ValueError)
        self.assertIsNone(parse_failure(request, Response(200)))

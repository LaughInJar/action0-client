import logging
import unittest

from action0.client import Hook
from action0.client import LoggingHook
from action0.client.testing import StubBackend
from action0.req import Request
from action0.req import Response


class HookTestCase(unittest.TestCase):
    """
    tests for the no-op :py:class:`action0.client.hooks.Hook` base class
    """

    def test_defaults_are_noops(self) -> None:
        """
        Test that the base hook observes without replacing anything.
        """
        hook = Hook()
        request = Request("https://example.com/")
        response = Response(200)
        self.assertIsNone(hook.on_request(request))
        self.assertIsNone(hook.on_response(request, response, 0.1))
        hook.on_error(request, RuntimeError("boom"), 0.1)  # must not raise


class LoggingHookTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.hooks.LoggingHook`
    """

    def test_request_and_response_are_logged(self) -> None:
        """
        Test that a send logs the redacted request and response lines.
        """
        logger = logging.getLogger("test.logging-hook")
        backend = StubBackend(Response(404), hooks=[LoggingHook(logger, level=logging.INFO)])

        with self.assertLogs(logger, level=logging.INFO) as logs:
            backend.send(Request("https://example.com/missing"))

        self.assertEqual(len(logs.output), 2)
        self.assertIn("-> Request(GET https://example.com/missing)", logs.output[0])
        self.assertIn("<- Response(404 Not Found)", logs.output[1])
        self.assertIn("ms", logs.output[1])

    def test_error_is_logged_at_error_level(self) -> None:
        """
        Test that a failing send logs the error at the error level.
        """

        def explode(request: Request) -> Response:
            raise ConnectionResetError("nope")

        logger = logging.getLogger("test.logging-hook-errors")
        hook = LoggingHook(logger, level=logging.DEBUG, error_level=logging.ERROR)
        backend = StubBackend(explode, hooks=[hook])

        with self.assertLogs(logger, level=logging.DEBUG) as logs:
            with self.assertRaises(ConnectionResetError):
                backend.send(Request("https://example.com/"))

        self.assertTrue(logs.output[-1].startswith("ERROR"))
        self.assertIn("ConnectionResetError", logs.output[-1])

    def test_default_logger_is_the_module_logger(self) -> None:
        """
        Test that the hook falls back to the hooks module logger.
        """
        self.assertEqual(LoggingHook().logger.name, "action0.client.hooks")

    def test_secret_headers_stay_redacted(self) -> None:
        """
        Test that logged requests never contain secret header values —
        repr() redaction is what makes the hook safe for production logs.
        """
        logger = logging.getLogger("test.logging-hook-secrets")
        backend = StubBackend(hooks=[LoggingHook(logger, level=logging.INFO)])
        request = Request(
            "https://user:secret-password@example.com/",
            headers={"Authorization": "Bearer secret-token"},
        )

        with self.assertLogs(logger, level=logging.INFO) as logs:
            backend.send(request)

        for line in logs.output:
            self.assertNotIn("secret-token", line)
            self.assertNotIn("secret-password", line)

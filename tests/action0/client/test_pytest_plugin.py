"""
The ``action0_mock`` pytest fixture, exercised the way a user's test
suite would use it: pytest runs on a generated test module in a process
of its own and finds the plugin through its entry point.
"""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

TESTS = """
import pytest

from action0.client import backend as backend_module
from action0.client.backends.urllib import UrllibBackend
from action0.client.testing import MockRouter
from action0.req import Request

BACKEND = UrllibBackend()  # the application's backend: never injected


def test_fixture_mocks_the_process(action0_mock):
    assert isinstance(action0_mock, MockRouter)
    assert backend_module._mock is action0_mock
    route = action0_mock.get("https://api.example.com/items/7").respond(json={"id": 7})
    assert BACKEND.send(Request("https://api.example.com/items/7")).body_str() == '{"id": 7}'
    assert route.calls.last.request.url.path == "/items/7"


def test_nothing_is_mocked_without_the_fixture():
    assert backend_module._mock is None


@pytest.mark.action0_mock(base_url="https://api.example.com/v1", assert_all_called=False)
def test_marker_configures_the_router(action0_mock):
    action0_mock.get("/items").respond(204)
    action0_mock.get("/never-called").respond()
    assert BACKEND.send(Request("https://api.example.com/v1/items")).status == 204


def test_unmocked_request_fails_the_test(action0_mock):
    try:
        BACKEND.send(Request("https://api.example.com/forgotten"))
    except Exception:
        pass  # swallowed by the application


def test_uncalled_route_fails_the_test(action0_mock):
    action0_mock.get("https://api.example.com/unused").respond()
"""


class PluginTestCase(unittest.TestCase):
    """
    tests for :py:mod:`action0.client.testing.pytest_plugin`
    """

    def test_fixture_in_a_real_pytest_run(self) -> None:
        """
        Test the fixture end to end: it is found without any conftest,
        activates the router for the test only, takes its options from
        the marker (accepted under ``--strict-markers``), and fails
        tests with unmocked requests or uncalled routes at teardown.
        """
        with TemporaryDirectory() as directory:
            Path(directory, "test_generated.py").write_text(textwrap.dedent(TESTS))
            finished = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "--strict-markers",
                    "-rA",
                    "--no-header",
                    "-q",
                    "test_generated.py",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        report = finished.stdout + finished.stderr
        for passed in (
            "test_fixture_mocks_the_process",
            "test_nothing_is_mocked_without_the_fixture",
            "test_marker_configures_the_router",
        ):
            self.assertIn(f"PASSED test_generated.py::{passed}", report)
        # the body of these two passes; the fixture's teardown fails them
        self.assertIn("ERROR test_generated.py::test_unmocked_request_fails_the_test", report)
        self.assertIn("1 request(s) were not mocked", report)
        self.assertIn("ERROR test_generated.py::test_uncalled_route_fails_the_test", report)
        self.assertIn("1 route(s) were never called", report)
        self.assertIn("5 passed, 2 errors", report)

    def test_plugin_module_is_the_only_one_importing_pytest(self) -> None:
        """
        Test that pytest stays optional: importing the testing package
        — the stubs and the router — does not import pytest.
        """
        code = (
            "import sys; import action0.client.testing; "
            "raise SystemExit(1 if 'pytest' in sys.modules else 0)"
        )
        finished = subprocess.run([sys.executable, "-c", code], timeout=60, check=False)
        self.assertEqual(finished.returncode, 0)

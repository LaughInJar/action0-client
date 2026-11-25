"""
The httpx and httpx2 backends in one process, with and without
``httpx2.alias_httpx()`` — which makes ``import httpx`` load httpx2 and
is process-wide, so each case runs in an interpreter of its own.
"""

import json
import subprocess
import sys
import unittest
from typing import Any

SCRIPT = r"""
import json
import sys

ALIASED = sys.argv[1] == "aliased"
if ALIASED:
    # must happen before anything imports httpx
    import httpx2

    httpx2.alias_httpx()

import httpx
import httpx2

from action0.client import TimeoutError
from action0.client import TransportError
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.httpx2 import AsyncHttpx2Backend
from action0.client.backends.httpx2 import Httpx2Backend
from action0.req import Request

REQUEST = Request("https://api.example.com/")


def outcome(backend_class, lib, raises=None):
    # What a send through the backend ends with: the status, or the
    # name of the exception class as the caller sees it.
    def handler(request):
        if raises is not None:
            raise raises("simulated")
        return lib.Response(204)

    client = lib.Client(transport=lib.MockTransport(handler))
    try:
        return backend_class(client).send(REQUEST).status
    except Exception as error:
        own = isinstance(error, (TimeoutError, TransportError))
        return ("action0." if own else "") + type(error).__name__
    finally:
        client.close()


report = {
    "httpx_is_httpx2": httpx is httpx2,
    "httpx_backend_library": HttpxBackend._lib.__name__,
    "httpx2_backend_library": Httpx2Backend._lib.__name__,
    "async_backends_share_the_libraries": [
        AsyncHttpxBackend._lib is HttpxBackend._lib,
        AsyncHttpx2Backend._lib is Httpx2Backend._lib,
    ],
    "httpx": {
        "ok": outcome(HttpxBackend, httpx),
        "timeout": outcome(HttpxBackend, httpx, httpx.ReadTimeout),
        "transport": outcome(HttpxBackend, httpx, httpx.ConnectError),
        "other_library_timeout": outcome(HttpxBackend, httpx, httpx2.ReadTimeout),
    },
    "httpx2": {
        "ok": outcome(Httpx2Backend, httpx2),
        "timeout": outcome(Httpx2Backend, httpx2, httpx2.ReadTimeout),
        "transport": outcome(Httpx2Backend, httpx2, httpx2.ConnectError),
        "other_library_timeout": outcome(Httpx2Backend, httpx2, httpx.ReadTimeout),
    },
}
print(json.dumps(report))
"""


def run(mode: str) -> "dict[str, Any]":
    """
    Run the script in a fresh interpreter.

    :param mode: ``"aliased"`` or ``"plain"``
    :return: the report it printed
    """
    finished = subprocess.run(
        [sys.executable, "-W", "error", "-c", SCRIPT, mode],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if finished.returncode != 0:
        raise AssertionError(f"the {mode} run failed:\n{finished.stderr}")
    report: "dict[str, Any]" = json.loads(finished.stdout)
    return report


class AliasTestCase(unittest.TestCase):
    """
    tests for importing and using both backend modules side by side
    """

    TRANSLATED = {
        "ok": 204,
        "timeout": "action0.TimeoutError",
        "transport": "action0.TransportError",
    }

    def test_without_the_alias_the_libraries_stay_apart(self) -> None:
        """
        Test the plain case: two libraries, each backend bound to its
        own, each translating its own exceptions — and passing the other
        library's through untouched instead of mixing them up.
        """
        report = run("plain")
        self.assertFalse(report["httpx_is_httpx2"])
        self.assertEqual(report["httpx_backend_library"], "httpx")
        self.assertEqual(report["httpx2_backend_library"], "httpx2")
        self.assertEqual(report["async_backends_share_the_libraries"], [True, True])
        for library in ("httpx", "httpx2"):
            with self.subTest(library=library):
                self.assertEqual(
                    report[library], self.TRANSLATED | {"other_library_timeout": "ReadTimeout"}
                )

    def test_with_the_alias_both_backends_drive_httpx2(self) -> None:
        """
        Test the aliased case: ``import httpx`` is httpx2, both backend
        modules still import, and both backends translate the (now
        single) library's exceptions.
        """
        report = run("aliased")
        self.assertTrue(report["httpx_is_httpx2"])
        self.assertEqual(report["httpx_backend_library"], "httpx2")
        self.assertEqual(report["httpx2_backend_library"], "httpx2")
        for library in ("httpx", "httpx2"):
            with self.subTest(library=library):
                self.assertEqual(
                    report[library],
                    self.TRANSLATED | {"other_library_timeout": "action0.TimeoutError"},
                )

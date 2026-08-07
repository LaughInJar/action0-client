import re
import unittest

import action0.client


class PackageTestCase(unittest.TestCase):
    """
    tests for the :py:mod:`action0.client` package root
    """

    def test_version(self) -> None:
        """
        Test that the version is a non-empty x.y.z string.
        """
        self.assertRegex(action0.client.__version__, re.compile(r"^\d+\.\d+\.\d+$"))

    def test_all_exports_exist(self) -> None:
        """
        Test that everything listed in __all__ is actually importable.
        """
        for name in action0.client.__all__:
            self.assertTrue(hasattr(action0.client, name), f"missing export: {name}")

    def test_dependencies_importable(self) -> None:
        """
        Test that the action0-url and action0-req dependencies resolve
        inside the same namespace.
        """
        from action0.req import Request
        from action0.url import Url

        self.assertEqual(Url("https://example.com/a").path, "/a")
        self.assertEqual(Request("https://example.com/a").method, "GET")

import dataclasses
import unittest
from typing import Any

from action0.client import BodyMode
from action0.client import RequestOptions
from action0.req import Request

URL = "https://api.example.com/translate"


class RequestOptionsTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.options.RequestOptions`
    """

    def test_request_without_options(self) -> None:
        """
        Test that a plain request has all-undecided options — one shared
        instance, and nothing is written to the request by looking.
        """
        request = Request(URL)
        options = RequestOptions.of(request)
        self.assertIsNone(options.retry_safe)
        self.assertEqual(options, RequestOptions())
        self.assertIs(RequestOptions.of(Request(URL)), options)
        self.assertEqual(request.meta, {})

    def test_apply_stores_under_one_namespaced_meta_key(self) -> None:
        """
        Test that apply() puts the options into the request's meta dict
        under the library's key and returns the request.
        """
        request = Request(URL, "POST")
        options = RequestOptions(retry_safe=True)
        self.assertIs(options.apply(request), request)
        self.assertEqual(request.meta, {"action0-client": options})
        self.assertIs(RequestOptions.of(request), options)

    def test_apply_merges_field_by_field(self) -> None:
        """
        Test that later options refine earlier ones: a decided field
        wins, an undecided one keeps what was there.
        """
        request = Request(URL, "POST")
        RequestOptions(retry_safe=True).apply(request)

        RequestOptions().apply(request)
        self.assertIs(RequestOptions.of(request).retry_safe, True)

        RequestOptions(retry_safe=False).apply(request)
        self.assertIs(RequestOptions.of(request).retry_safe, False)

    def test_options_never_reach_the_wire(self) -> None:
        """
        Test that options are metadata: absent from the rendered request
        and irrelevant for equality.
        """
        request = RequestOptions(retry_safe=True).apply(Request(URL, "POST", body="hello"))
        self.assertNotIn("retry", request.as_str(include_body=True).lower())
        self.assertEqual(request, Request(URL, "POST", body="hello"))

    def test_options_survive_a_request_copy(self) -> None:
        """
        Test that a copied request keeps the options, and that changing
        the copy's options leaves the original's alone.
        """
        request = RequestOptions(retry_safe=True).apply(Request(URL, "POST"))
        copy = request.copy(method="PUT")
        self.assertIs(RequestOptions.of(copy).retry_safe, True)

        RequestOptions(retry_safe=False).apply(copy)
        self.assertIs(RequestOptions.of(request).retry_safe, True)

    def test_immutable_and_keyword_only(self) -> None:
        """
        Test that options cannot be changed after creation (they are
        shared between requests) and take keyword arguments only (so
        fields can be added without breaking callers).
        """
        options = RequestOptions(retry_safe=True)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.retry_safe = False  # type: ignore[misc]  # ty: ignore[invalid-assignment]
        # Any: the call is wrong on purpose, and each checker words its
        # complaint differently
        untyped: Any = RequestOptions
        with self.assertRaises(TypeError):
            untyped(True)

    def test_repr_shows_what_is_decided(self) -> None:
        """
        Test that the repr leaves out undecided fields.
        """
        self.assertEqual(repr(RequestOptions()), "RequestOptions()")
        self.assertEqual(
            repr(RequestOptions(retry_safe=False)), "RequestOptions(retry_safe=False)"
        )

    def test_foreign_value_under_the_key_is_ignored(self) -> None:
        """
        Test that something else stored under the meta key reads as "no
        options" and is replaced by apply().
        """
        request = Request(URL, meta={"action0-client": "not options"})
        self.assertEqual(RequestOptions.of(request), RequestOptions())
        RequestOptions(retry_safe=True).apply(request)
        self.assertIs(RequestOptions.of(request).retry_safe, True)

    def test_body_options_merge_independently(self) -> None:
        """
        Test that the body mode and its spool settings can come from
        different places: each decided field survives the merge.
        """
        request = Request(URL)
        RequestOptions(body=BodyMode.SPOOL, spool_max_memory=0).apply(request)
        RequestOptions(spool_dir="/var/tmp", retry_safe=True).apply(request)
        RequestOptions(body=BodyMode.STREAM).apply(request)

        options = RequestOptions.of(request)
        self.assertIs(options.body, BodyMode.STREAM)
        self.assertEqual(options.spool_max_memory, 0)
        self.assertEqual(options.spool_dir, "/var/tmp")
        self.assertIs(options.retry_safe, True)
        self.assertEqual(
            repr(options),
            "RequestOptions(body=<BodyMode.STREAM: 'stream'>, spool_max_memory=0,"
            " spool_dir='/var/tmp', retry_safe=True)",
        )

    def test_negative_spool_memory_is_refused(self) -> None:
        """
        Test that a nonsensical threshold fails where it is written, not
        on the first response.
        """
        with self.assertRaises(ValueError):
            RequestOptions(spool_max_memory=-1)

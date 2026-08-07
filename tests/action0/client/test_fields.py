import dataclasses
import unittest
from typing import Any

from action0.client import JsonOperation
from action0.client.fields import _METADATA_KEY
from action0.client.fields import FieldSpec
from action0.client.fields import Location
from action0.client.fields import body
from action0.client.fields import header
from action0.client.fields import json_body
from action0.client.fields import json_field
from action0.client.fields import path_param
from action0.client.fields import query


def spec_of(field: "dataclasses.Field[Any]") -> FieldSpec:
    """
    The FieldSpec a specifier attached to a dataclass field.

    :param field: the dataclass field to inspect
    :return: the attached spec
    """
    spec = field.metadata[_METADATA_KEY]
    assert isinstance(spec, FieldSpec)
    return spec


class SpecifierTestCase(unittest.TestCase):
    """
    tests for the field specifier functions in
    :py:mod:`action0.client.fields`
    """

    def test_locations(self) -> None:
        """
        Test that every specifier stamps its location.
        """
        cases = [
            (query(), Location.QUERY),
            (header(), Location.HEADER),
            (path_param(), Location.PATH),
            (json_field(), Location.JSON_FIELD),
            (json_body(), Location.JSON_BODY),
            (body(), Location.BODY),
        ]
        for field, location in cases:
            with self.subTest(location=location):
                self.assertEqual(spec_of(field).location, location)

    def test_alias(self) -> None:
        """
        Test that the wire-name alias is recorded (and None by default).
        """
        self.assertEqual(spec_of(query("pageSize")).alias, "pageSize")
        self.assertEqual(spec_of(header("X-API-Key")).alias, "X-API-Key")
        self.assertEqual(spec_of(json_field("itemId")).alias, "itemId")
        self.assertIsNone(spec_of(query()).alias)

    def test_serialize_callable(self) -> None:
        """
        Test that a custom serializer is recorded.
        """
        upper = str.upper
        self.assertIs(spec_of(query(serialize=upper)).serialize, upper)
        self.assertIsNone(spec_of(query()).serialize)

    def test_default_handling(self) -> None:
        """
        Test that defaults and default factories reach the dataclass field.
        """
        with_default = query(default=25)
        self.assertEqual(with_default.default, 25)

        with_factory = json_field(default_factory=list)
        self.assertIs(with_factory.default_factory, list)

        required = query()
        self.assertIs(required.default, dataclasses.MISSING)
        self.assertIs(required.default_factory, dataclasses.MISSING)

    def test_default_and_factory_are_mutually_exclusive(self) -> None:
        """
        Test that the dataclasses-level exclusivity is enforced.
        """
        with self.assertRaises(ValueError):
            query(default=1, default_factory=list)

    def test_repr_false_hides_the_value(self) -> None:
        """
        Test that repr=False keeps secrets out of the operation repr.
        """

        class Authenticated(JsonOperation[Any]):
            path = "/x"
            token: str = header("X-API-Key", repr=False)
            page: int = query(default=1)

        rendered = repr(Authenticated(token="secret-token"))
        self.assertNotIn("secret-token", rendered)
        self.assertIn("page=1", rendered)

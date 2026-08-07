import datetime
import unittest
from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import ClassVar

from action0.client import APIError
from action0.client import JsonOperation
from action0.client import Operation
from action0.client.fields import Location
from action0.client.fields import body
from action0.client.fields import header
from action0.client.fields import json_body
from action0.client.fields import json_field
from action0.client.fields import path_param
from action0.client.fields import query
from action0.req import Method
from action0.req import Request
from action0.req import Response
from action0.req.body import BytesBody
from action0.url import Url


class Color(Enum):
    """An enum with string values, for serialization tests."""

    RED = "red"
    BLUE = "blue"


class SearchOperation(JsonOperation[Any]):
    """The workhorse example: a GET with query, header and path fields."""

    method = Method.GET
    path = "/shelves/{shelf}/items"

    shelf: str = path_param()
    q: str = query()
    page_size: int = query("pageSize", default=25)
    color: "Color | None" = query(default=None)
    locale: "str | None" = header("Accept-Language", default=None)


class DefinitionTestCase(unittest.TestCase):
    """
    tests for the class-creation behavior of
    :py:class:`action0.client.operation.Operation`
    """

    def test_subclasses_become_keyword_only_dataclasses(self) -> None:
        """
        Test that operations are dataclasses and reject positional
        arguments.
        """
        operation = SearchOperation(shelf="a", q="thing")
        self.assertEqual(operation.page_size, 25)
        with self.assertRaises(TypeError):
            SearchOperation("a", "thing")  # type: ignore[call-arg]  # ty: ignore[missing-argument, too-many-positional-arguments]

    def test_operations_compare_by_fields(self) -> None:
        """
        Test the generated dataclass equality.
        """
        self.assertEqual(
            SearchOperation(shelf="a", q="thing"), SearchOperation(shelf="a", q="thing")
        )
        self.assertNotEqual(
            SearchOperation(shelf="a", q="thing"), SearchOperation(shelf="b", q="thing")
        )

    def test_reserved_field_names_are_refused(self) -> None:
        """
        Test that a field named like a configuration attribute fails at
        class creation with a helpful message.
        """
        with self.assertRaisesRegex(TypeError, "path"):

            class Broken(JsonOperation[Any]):
                path: str = query()  # type: ignore[misc]  # ty: ignore[invalid-attribute-override]

    def test_missing_path_param_field_is_refused(self) -> None:
        """
        Test that a template placeholder without a matching field fails.
        """
        with self.assertRaisesRegex(TypeError, "item_id"):

            class Broken(JsonOperation[Any]):
                path = "/items/{item_id}"

    def test_dangling_path_param_field_is_refused(self) -> None:
        """
        Test that a path_param field without a placeholder fails.
        """
        with self.assertRaisesRegex(TypeError, "item_id"):

            class Broken(JsonOperation[Any]):
                path = "/items"
                item_id: int = path_param()

    def test_exotic_placeholders_are_refused(self) -> None:
        """
        Test that positional and format-spec placeholders fail.
        """
        for template in ("/items/{}", "/items/{0}", "/items/{id:>5}", "/items/{id!r}"):
            with self.subTest(template=template):
                with self.assertRaises(TypeError):

                    class Broken(JsonOperation[Any]):
                        path = template

    def test_conflicting_bodies_are_refused(self) -> None:
        """
        Test that mixing body kinds fails at class creation.
        """
        with self.assertRaisesRegex(TypeError, "more than one request body"):

            class JsonAndRaw(JsonOperation[Any]):
                path = "/x"
                payload: dict = json_body()  # type: ignore[type-arg]
                raw: bytes = body()

        with self.assertRaisesRegex(TypeError, "more than one request body"):

            class FieldAndWhole(JsonOperation[Any]):
                path = "/x"
                name: str = json_field()
                payload: dict = json_body()  # type: ignore[type-arg]

        with self.assertRaisesRegex(TypeError, "more than one request body"):

            class TwoWholes(JsonOperation[Any]):
                path = "/x"
                one: dict = json_body()  # type: ignore[type-arg]
                two: dict = json_body()  # type: ignore[type-arg]

    def test_fields_are_inherited(self) -> None:
        """
        Test that an operation family base can contribute common fields.
        """

        class FamilyBase(JsonOperation[Any]):
            token: str = header("X-API-Key", repr=False)

        class Ping(FamilyBase):
            path = "/ping"
            q: "str | None" = query(default=None)

        request = Ping(token="t", q="x").as_request("https://api.example.com")
        self.assertEqual(request.headers["X-API-Key"], "t")
        self.assertEqual(request.url.query["q"], "x")


class AsRequestTestCase(unittest.TestCase):
    """
    tests for :py:meth:`action0.client.operation.Operation.as_request`
    """

    def test_full_url_assembly(self) -> None:
        """
        Test path template, query fields and base URL joining.
        """
        operation = SearchOperation(shelf="fiction", q="dune", page_size=50)
        request = operation.as_request("https://api.example.com/v2")
        self.assertEqual(request.method, "GET")
        self.assertEqual(
            request.url.as_str(),
            "https://api.example.com/v2/shelves/fiction/items?q=dune&pageSize=50",
        )

    def test_none_fields_are_omitted(self) -> None:
        """
        Test that None means "not sent" for query and header fields.
        """
        request = SearchOperation(shelf="a", q="x").as_request("https://api.example.com")
        self.assertNotIn("color", request.url.query)
        self.assertNotIn("Accept-Language", request.headers)

    def test_enum_and_header_serialization(self) -> None:
        """
        Test enum values and header fields on the wire.
        """
        operation = SearchOperation(shelf="a", q="x", color=Color.BLUE, locale="de-AT")
        request = operation.as_request("https://api.example.com")
        self.assertEqual(request.url.query["color"], "blue")
        self.assertEqual(request.headers["Accept-Language"], "de-AT")

    def test_accept_header_from_class_attribute(self) -> None:
        """
        Test that JsonOperation advertises JSON, without clobbering an
        explicit Accept.
        """

        class WithAccept(JsonOperation[Any]):
            path = "/x"
            accept_override: str = header("Accept")

        self.assertEqual(
            SearchOperation(shelf="a", q="x").as_request().headers["Accept"],
            "application/json",
        )
        self.assertEqual(
            WithAccept(accept_override="text/csv").as_request().headers["Accept"],
            "text/csv",
        )

    def test_list_query_values_repeat_the_parameter(self) -> None:
        """
        Test multi-value query parameters from list fields.
        """

        class Multi(JsonOperation[Any]):
            path = "/items"
            ids: list[int] = query()

        request = Multi(ids=[1, 2, 3]).as_request("https://api.example.com")
        self.assertEqual(request.url.query.get_all("ids"), ["1", "2", "3"])

    def test_dates_and_booleans_serialize_web_style(self) -> None:
        """
        Test ISO dates and true/false booleans in query and path.
        """

        class Report(JsonOperation[Any]):
            path = "/reports/{day}"
            day: datetime.date = path_param()
            detailed: bool = query()

        request = Report(day=datetime.date(2026, 8, 7), detailed=True).as_request()
        self.assertEqual(request.url.path, "/reports/2026-08-07")
        self.assertEqual(request.url.query["detailed"], "true")

    def test_custom_serializer_wins(self) -> None:
        """
        Test the per-field serialize= hook.
        """

        class Custom(JsonOperation[Any]):
            path = "/items"
            ids: list[int] = query(serialize=lambda ids: ",".join(str(i) for i in ids))

        request = Custom(ids=[1, 2]).as_request()
        self.assertEqual(request.url.query["ids"], "1,2")

    def test_unserializable_query_value_raises(self) -> None:
        """
        Test the error for values that aren't wire scalars.
        """

        class Bad(JsonOperation[Any]):
            path = "/items"
            blob: object = query()

        with self.assertRaisesRegex(ValueError, "scalar"):
            Bad(blob=object()).as_request()

    def test_none_path_param_raises(self) -> None:
        """
        Test that a None path parameter is refused (it cannot be omitted).
        """

        class Get(JsonOperation[Any]):
            path = "/items/{item_id}"
            item_id: "int | None" = path_param(default=None)

        with self.assertRaisesRegex(ValueError, "item_id"):
            Get().as_request()

    def test_json_field_body(self) -> None:
        """
        Test the JSON object body assembled from json_field()s.
        """

        class Create(JsonOperation[Any]):
            method = Method.POST
            path = "/items"
            name: str = json_field()
            item_color: "Color | None" = json_field("color", default=None)
            tags: "list[str] | None" = json_field(default=None)

        request = Create(name="Thing", item_color=Color.RED).as_request()
        self.assertEqual(request.body, '{"name": "Thing", "color": "red"}')
        self.assertEqual(request.headers["Content-Type"], "application/json")

    def test_json_body_whole_payload(self) -> None:
        """
        Test the whole-body JSON field, including dataclass serialization.
        """

        @dataclass
        class Draft:
            name: str
            note: "str | None" = None

        class Create(JsonOperation[Any]):
            method = Method.POST
            path = "/items"
            draft: Draft = json_body()

        request = Create(draft=Draft(name="Thing")).as_request()
        self.assertEqual(request.body, '{"name": "Thing"}')

    def test_raw_body_passes_through(self) -> None:
        """
        Test the raw body field with str, bytes and a BodyProducer.
        """

        class Upload(Operation[Response]):
            method = Method.PUT
            path = "/blob"
            content: object = body()

            def load(self, response: Response) -> Response:
                return response

        for content in ("text", b"bytes", BytesBody(b"streamed")):
            with self.subTest(content=type(content).__name__):
                request = Upload(content=content).as_request()
                self.assertIs(request.body, content)
        self.assertNotIn("Content-Type", Upload(content=b"x").as_request().headers)

    def test_default_location_routes_plain_fields(self) -> None:
        """
        Test that plain fields follow default_location — the JSON-heavy
        API family case.
        """

        class JsonFamily(JsonOperation[Any]):
            default_location: ClassVar[Location] = Location.JSON_FIELD

        class Create(JsonFamily):
            method = Method.POST
            path = "/items"
            name: str
            count: int = 1

        request = Create(name="Thing").as_request()
        self.assertEqual(request.body, '{"name": "Thing", "count": 1}')

    def test_base_url_forms(self) -> None:
        """
        Test string, Url and absent base URLs (and that a passed Url is
        not mutated).
        """
        operation = SearchOperation(shelf="a", q="x")
        base = Url("https://api.example.com/v2?token=t")

        request = operation.as_request(base)
        self.assertEqual(
            request.url.as_str(),
            "https://api.example.com/v2/shelves/a/items?token=t&q=x&pageSize=25",
        )
        # the caller's Url is untouched
        self.assertEqual(base.as_str(), "https://api.example.com/v2?token=t")

        relative = operation.as_request()
        self.assertEqual(relative.url.as_str(), "/shelves/a/items?q=x&pageSize=25")

    def test_empty_path_uses_the_base_url(self) -> None:
        """
        Test operations against the base URL itself.
        """

        class Root(JsonOperation[Any]):
            q: str = query()

        request = Root(q="x").as_request("https://api.example.com/v2")
        self.assertEqual(request.url.as_str(), "https://api.example.com/v2?q=x")


class ParseTestCase(unittest.TestCase):
    """
    tests for check()/parse()/load() and the JSON loading
    """

    def test_parse_returns_the_loaded_payload(self) -> None:
        """
        Test the success path.
        """
        operation = SearchOperation(shelf="a", q="x")
        self.assertEqual(operation.parse(Response(200, body='{"hits": 1}')), {"hits": 1})

    def test_unexpected_status_raises_api_error(self) -> None:
        """
        Test that non-2xx responses raise, carrying the response.
        """
        operation = SearchOperation(shelf="a", q="x")
        response = Response(503, body="try later", request=Request("https://x.example"))
        with self.assertRaises(APIError) as caught:
            operation.parse(response)
        self.assertIn("503", str(caught.exception))
        self.assertIs(caught.exception.response, response)
        self.assertIs(caught.exception.request, response.request)

    def test_check_can_be_overridden(self) -> None:
        """
        Test per-operation status policies (404 tolerated here).
        """

        class Lenient(JsonOperation[Any]):
            path = "/items"

            def check(self, response: Response) -> None:
                if response.status != 404:
                    super().check(response)

            def load(self, response: Response) -> Any:
                if response.status == 404:
                    return None
                return super().load(response)

        self.assertIsNone(Lenient().parse(Response(404)))

    def test_empty_json_body_raises(self) -> None:
        """
        Test the empty-body APIError of JsonOperation.
        """
        operation = SearchOperation(shelf="a", q="x")
        with self.assertRaisesRegex(APIError, "JSON body"):
            operation.parse(Response(200))

    def test_malformed_json_body_raises(self) -> None:
        """
        Test the malformed-body APIError of JsonOperation, chaining the
        decoder error.
        """
        operation = SearchOperation(shelf="a", q="x")
        with self.assertRaisesRegex(APIError, "malformed") as caught:
            operation.parse(Response(200, body="{nope"))
        self.assertIsNotNone(caught.exception.__cause__)

    def test_load_json_produces_typed_results(self) -> None:
        """
        Test a typed operation end to end through parse().
        """

        @dataclass
        class Item:
            id: int
            name: str

        class GetItem(JsonOperation[Item]):
            path = "/items/{item_id}"
            item_id: int = path_param()

            def load_json(self, data: Any) -> Item:
                return Item(id=data["id"], name=data["name"])

        item = GetItem(item_id=1).parse(Response(200, body='{"id": 1, "name": "x"}'))
        self.assertEqual(item, Item(id=1, name="x"))


class SerializeJsonValueTestCase(unittest.TestCase):
    """
    tests for :py:meth:`action0.client.operation.Operation.serialize_json_value`
    """

    def setUp(self) -> None:
        self.operation = SearchOperation(shelf="a", q="x")

    def test_nested_structures(self) -> None:
        """
        Test dataclasses, mappings, sequences and scalars nested freely.
        """

        @dataclass
        class Inner:
            when: datetime.datetime
            color: Color
            note: "str | None" = None

        value = {
            "items": [Inner(when=datetime.datetime(2026, 8, 7, 12, 30), color=Color.RED)],
            "count": 1,
            "skip": None,
        }
        self.assertEqual(
            self.operation.serialize_json_value(value),
            {"items": [{"when": "2026-08-07T12:30:00", "color": "red"}], "count": 1},
        )

    def test_unserializable_value_raises(self) -> None:
        """
        Test the error for values without a JSON representation.
        """
        with self.assertRaisesRegex(ValueError, "JSON"):
            self.operation.serialize_json_value(object())

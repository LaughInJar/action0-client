"""
The typed description of an API endpoint (:py:class:`Operation`) and its
JSON convenience subclass (:py:class:`JsonOperation`).

An *operation* bundles everything about one endpoint of an API:

- the parts that never change — the HTTP ``method`` and the ``path``
  template — as class attributes,
- the variable parts — query parameters, headers, path parameters and the
  body — as typed dataclass fields (placed via the specifiers of
  :py:mod:`action0.client.fields`),
- and how to turn the HTTP response into a typed result — ``parse()``,
  with the result type as the generic parameter.

Subclasses become keyword-only dataclasses automatically (the base class
is a :py:func:`typing.dataclass_transform`), so an operation is declared
like a dataclass and instantiated like one::

    class GetItem(JsonOperation[Item]):
        method = Method.GET
        path = "/items/{item_id}"

        item_id: int = path_param()
        expand: bool | None = query(default=None)

        def load_json(self, data: Any) -> Item:
            return Item(id=data["id"], name=data["name"])


    operation = GetItem(item_id=42)

An :py:class:`~action0.client.api.APIClient` turns operations into
requests, sends them through its backend and parses the responses — with
the parsed type flowing through: ``client.send(GetItem(item_id=42))`` is
an ``Item`` (or an ``Awaitable[Item]`` / ``Deferred[Item]``, depending on
the backend).
"""

import dataclasses
import datetime
import json
import string
from abc import ABC
from abc import abstractmethod
from enum import Enum
from typing import Any
from typing import ClassVar
from typing import Generic
from typing import Mapping
from typing import TypeVar
from typing import cast
from typing import dataclass_transform

from action0.req import Header
from action0.req import Headers
from action0.req import Method
from action0.req import Request
from action0.req import Response
from action0.req.body import BodyTypes
from action0.url import Url
from action0.url.params import Params
from action0.url.params import ParamValue

from .errors import APIError
from .fields import _METADATA_KEY
from .fields import FieldSpec
from .fields import Location
from .fields import body
from .fields import form_field
from .fields import header
from .fields import json_body
from .fields import json_field
from .fields import path_param
from .fields import query

R_co = TypeVar("R_co", covariant=True)
"""The parsed result type of an operation — what :py:meth:`Operation.parse`
returns and what :py:meth:`action0.client.api.APIClient.send` resolves to."""

_RESERVED_FIELD_NAMES = frozenset({"method", "path", "accept", "default_location"})
"""Class-attribute names of :py:class:`Operation` that must not be used as
field names (a field named ``path`` is almost certainly a forgotten
``ClassVar`` — refuse it loudly)."""


def _placeholders(path: str) -> set[str]:
    """
    The ``{placeholder}`` names of a path template.

    :param path: the path template, e.g. ``"/items/{item_id}"``
    :return: the placeholder names, e.g. ``{"item_id"}``
    :raises TypeError: on positional (``{}`` / ``{0}``), attributed/indexed
                       (``{a.b}`` / ``{a[0]}``) or format-spec'd
                       (``{a:>5}`` / ``{a!r}``) placeholders — only plain
                       names are supported
    """
    names: set[str] = set()
    for _literal, name, spec, conversion in string.Formatter().parse(path):
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise TypeError(
                f"path template {path!r}: placeholders must be plain names"
                " like {item_id} (no positional, nested or format-spec forms)"
            )
        names.add(name)
    return names


def _dataclass_fields(operation: Any) -> "tuple[dataclasses.Field[Any], ...]":
    """
    The dataclass fields of an operation class or instance — a typed
    wrapper, because the static checker cannot know that every
    :py:class:`Operation` subclass is a dataclass (the transformation
    happens at runtime, in ``__init_subclass__``).

    :param operation: the operation class or instance
    :return: its dataclass fields
    """
    return dataclasses.fields(operation)


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(
        dataclasses.field,
        query,
        header,
        path_param,
        json_field,
        json_body,
        form_field,
        body,
    ),
)
class Operation(Generic[R_co], ABC):
    """
    The base class of all endpoint descriptions.

    Subclassing does three things automatically:

    - the subclass becomes a keyword-only :py:func:`dataclasses.dataclass`
      (fields are declared with the specifiers of
      :py:mod:`action0.client.fields`, or plainly — then
      :py:attr:`default_location` decides their placement),
    - the class is validated: every ``{placeholder}`` of the ``path``
      template must have exactly one matching
      :py:func:`~action0.client.fields.path_param` field, only one form of
      request body may be declared, and reserved names are refused,
    - instances gain dataclass ``__init__``, ``__eq__`` and ``__repr__``.

    Subclasses choose the parsed result type via the generic parameter and
    implement :py:meth:`load` (or use :py:class:`JsonOperation`, which
    implements it for JSON APIs). The class attributes fix the constant
    parts of the endpoint:

    - :py:attr:`method` — the HTTP method (default ``GET``),
    - :py:attr:`path` — the path template appended to the client's base
      URL, with ``{placeholder}`` names bound to ``path_param()`` fields,
    - :py:attr:`accept` — an ``Accept`` header value to request,
    - :py:attr:`default_location` — where fields without an explicit
      specifier go (query parameters by default; a JSON-body-heavy API
      family may want ``Location.JSON_FIELD``).

    Example — a raw (non-JSON) operation returning the body text::

        >>> from action0.req import Method, Response
        >>> class GetReport(Operation[str]):
        ...     method = Method.GET
        ...     path = "/reports/{report_id}"
        ...
        ...     report_id: int = path_param()
        ...     lines: int | None = query(default=None)
        ...
        ...     def load(self, response: Response) -> str:
        ...         return response.body_str() or ""
        >>> operation = GetReport(report_id=7, lines=100)
        >>> operation
        GetReport(report_id=7, lines=100)
        >>> operation.as_request("https://api.example.com/v1").url.as_str()
        'https://api.example.com/v1/reports/7?lines=100'
        >>> operation.parse(Response(200, body="all is well"))
        'all is well'

    Fields whose value is ``None`` are omitted from the request::

        >>> GetReport(report_id=7).as_request("https://api.example.com/v1").url.as_str()
        'https://api.example.com/v1/reports/7'

    Unexpected statuses raise an :py:class:`~action0.client.errors.APIError`
    (tune that by overriding :py:meth:`check`)::

        >>> operation.parse(Response(500, body="boom"))
        Traceback (most recent call last):
            ...
        action0.client.errors.APIError: GetReport: unexpected status 500 Internal Server Error
    """

    method: ClassVar[str] = Method.GET
    """The HTTP method of the endpoint — fixed per operation class."""

    path: ClassVar[str] = ""
    """The path template of the endpoint, appended to the client's base URL.
    ``{placeholder}`` names are filled from the operation's
    :py:func:`~action0.client.fields.path_param` fields. A leading ``/`` is
    optional — the path is always joined to the base URL with exactly one
    ``/``."""

    accept: ClassVar[str | None] = None
    """The ``Accept`` header to send, unless one is set explicitly; ``None``
    sends none. :py:class:`JsonOperation` sets ``application/json``."""

    default_location: ClassVar[Location] = Location.QUERY
    """Where fields without an explicit specifier are placed. Query
    parameters by default; an API family whose endpoints all take JSON
    bodies may set this to :py:attr:`~action0.client.fields.Location.JSON_FIELD`
    in its common base class."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """
        Turn the new subclass into a keyword-only dataclass and validate
        its field/template consistency.

        :param kwargs: passed through to :py:meth:`object.__init_subclass__`
        :raises TypeError: if a reserved name is used as a field, a path
                           placeholder has no matching ``path_param`` field
                           (or vice versa), or more than one body form is
                           declared
        """
        super().__init_subclass__(**kwargs)
        dataclasses.dataclass(kw_only=True)(cls)
        cls._validate()

    @classmethod
    def _validate(cls) -> None:
        """
        Check the field/template consistency of a freshly created subclass
        — failing at class-definition time beats failing on the first send.

        :raises TypeError: see :py:meth:`__init_subclass__`
        """
        specs = {field.name: cls._field_spec(field) for field in _dataclass_fields(cls)}

        reserved = _RESERVED_FIELD_NAMES.intersection(specs)
        if reserved:
            raise TypeError(
                f"{cls.__name__}: {', '.join(sorted(reserved))} cannot be used as field"
                " name(s) — these configure the operation class itself; if you meant to"
                " configure, assign without a type annotation (or annotate as ClassVar)"
            )

        path_fields = {name for name, spec in specs.items() if spec.location is Location.PATH}
        placeholders = _placeholders(cls.path)
        if path_fields != placeholders:
            raise TypeError(
                f"{cls.__name__}: path template {cls.path!r} placeholders"
                f" {sorted(placeholders)} do not match the path_param() fields"
                f" {sorted(path_fields)}"
            )

        json_fields = [n for n, s in specs.items() if s.location is Location.JSON_FIELD]
        json_bodies = [n for n, s in specs.items() if s.location is Location.JSON_BODY]
        form_fields = [n for n, s in specs.items() if s.location is Location.FORM_FIELD]
        raw_bodies = [n for n, s in specs.items() if s.location is Location.BODY]
        used = [kind for kind in (json_fields, json_bodies, form_fields, raw_bodies) if kind]
        if len(json_bodies) > 1 or len(raw_bodies) > 1 or len(used) > 1:
            conflicting = sorted(name for kind in used for name in kind)
            raise TypeError(
                f"{cls.__name__}: the fields {conflicting} declare more than one request"
                " body — use several json_field()s (or form_field()s), or a single"
                " json_body(), or a single body()"
            )

    @classmethod
    def _field_spec(cls, field: "dataclasses.Field[Any]") -> FieldSpec:
        """
        The placement spec of a field: the one attached by its specifier,
        or a default-location spec for plain fields.

        :param field: the dataclass field
        :return: the spec describing where the field goes
        """
        spec = field.metadata.get(_METADATA_KEY)
        if isinstance(spec, FieldSpec):
            return spec
        return FieldSpec(cls.default_location)

    def as_request(self, base_url: "str | Url | None" = None) -> Request:
        """
        Build the :py:class:`~action0.req.request.Request` this operation
        describes: the rendered path template appended to the base URL, the
        query/header/body fields serialized into their places (fields whose
        value is ``None`` are omitted), plus a ``Content-Type`` for a JSON
        or form body and the :py:attr:`accept` header — each only if not
        already set.

        Called by :py:meth:`action0.client.api.APIClient.send`, but also
        useful standalone, e.g. in tests. Override it (adjusting the result
        of ``super().as_request(base_url)``) for exotic request shapes.

        :param base_url: the URL the endpoint path is appended to, e.g.
                         ``"https://api.example.com/v2"``; ``None`` builds
                         a relative request (handy for inspecting)
        :return: the request, ready to be sent through a backend
        :raises ValueError: if a path parameter is ``None``, or a value
                            cannot be serialized for its location
        """
        if base_url is None:
            base = Url()
        elif isinstance(base_url, Url):
            base = base_url
        else:
            base = Url(base_url)

        path_values: dict[str, str] = {}
        query_items: list[tuple[str, "ParamValue | list[ParamValue]"]] = []
        headers = Headers()
        json_object: dict[str, Any] = {}
        has_json_fields = False
        json_payload: Any = None
        has_json_payload = False
        form_params = Params()
        has_form_fields = False
        raw_body: "BodyTypes | None" = None

        for field in _dataclass_fields(self):
            spec = self._field_spec(field)
            if spec.location is Location.JSON_FIELD:
                has_json_fields = True
            elif spec.location is Location.FORM_FIELD:
                has_form_fields = True

            value = getattr(self, field.name)
            if value is not None and spec.serialize is not None:
                value = spec.serialize(value)
            if value is None:
                if spec.location is Location.PATH:
                    raise ValueError(
                        f"{type(self).__name__}: path parameter {field.name!r} must not be None"
                    )
                continue

            name = spec.alias or field.name
            if spec.location is Location.PATH:
                path_values[field.name] = self._path_str(field.name, value)
            elif spec.location is Location.QUERY:
                query_items.append((name, self.serialize_value(value)))
            elif spec.location is Location.HEADER:
                headers.add(name, self.serialize_value(value))
            elif spec.location is Location.JSON_FIELD:
                json_object[name] = self.serialize_json_value(value)
            elif spec.location is Location.JSON_BODY:
                json_payload = self.serialize_json_value(value)
                has_json_payload = True
            elif spec.location is Location.FORM_FIELD:
                form_params.add(name, self.serialize_value(value))
            else:  # Location.BODY
                raw_body = cast("BodyTypes", value)

        # the operation path is always relative to the base URL; "/" joins
        # them exactly once (Url.__truediv__)
        url = base / self.path.format_map(path_values) if self.path else base.copy()
        for name, value_s in query_items:
            url.query.add(name, value_s)

        request_body: "BodyTypes | None" = raw_body
        if has_json_payload or (has_json_fields and raw_body is None):
            payload = json_payload if has_json_payload else json_object
            request_body = json.dumps(payload)
            if Header.CONTENT_TYPE not in headers:
                headers.add(Header.CONTENT_TYPE, "application/json")
        elif has_form_fields and raw_body is None:
            request_body = form_params.as_str()
            if Header.CONTENT_TYPE not in headers:
                headers.add(Header.CONTENT_TYPE, "application/x-www-form-urlencoded")

        if self.accept is not None and Header.ACCEPT not in headers:
            headers.add(Header.ACCEPT, self.accept)

        return Request(url, self.method, headers=headers, body=request_body)

    def serialize_value(self, value: Any) -> "ParamValue | list[ParamValue]":
        """
        Serialize a field value for a query parameter or header: enums
        become their ``value``, dates/times their ISO representation,
        scalars pass through (the ``Params``/``Headers`` classes coerce
        them, e.g. ``True`` to ``"true"``), and a list/tuple/set becomes a
        list — one query parameter / header line per element.

        Override to support more value types across an operation family.

        :param value: the field value (never ``None`` — those are omitted)
        :return: the scalar (or list of scalars) to put on the wire
        :raises ValueError: if the value (or an element) is no scalar
        """
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._scalar(element) for element in value]
        return self._scalar(value)

    def _scalar(self, value: Any) -> "ParamValue":
        """
        Serialize a single scalar for a query parameter, header or path
        segment.

        :param value: the value to serialize
        :return: the scalar to put on the wire
        :raises ValueError: if the value is no scalar
        """
        if isinstance(value, Enum):
            return self._scalar(value.value)
        if isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        raise ValueError(
            f"{type(self).__name__}: cannot serialize {type(value).__name__} value"
            f" {value!r} as a query/header/path scalar — pass a scalar or add a"
            " serialize= callable to the field"
        )

    def _path_str(self, name: str, value: Any) -> str:
        """
        Serialize a path parameter value to the string substituted into the
        template (booleans in web style, like everywhere else).

        :param name: the field name (for error messages)
        :param value: the field value
        :return: the path segment text (percent-encoding happens when the
                 URL is rendered)
        :raises ValueError: if the value is no scalar
        """
        scalar = self._scalar(value)
        if isinstance(scalar, bool):
            return "true" if scalar else "false"
        return str(scalar)

    def serialize_json_value(self, value: Any) -> Any:
        """
        Serialize a field value for a JSON body: enums become their
        ``value``, dates/times their ISO representation, dataclasses and
        mappings become objects (entries whose value is ``None`` are
        omitted, like everywhere else), lists/tuples/sets become arrays,
        scalars and ``None`` pass through.

        Override to support more value types across an operation family.

        :param value: the field value
        :return: something :py:func:`json.dumps` can encode
        :raises ValueError: if the value (or a part of it) has no JSON
                            representation
        """
        if isinstance(value, Enum):
            return self.serialize_json_value(value.value)
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: self.serialize_json_value(getattr(value, field.name))
                for field in dataclasses.fields(value)
                if getattr(value, field.name) is not None
            }
        if isinstance(value, Mapping):
            return {
                str(key): self.serialize_json_value(element)
                for key, element in value.items()
                if element is not None
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.serialize_json_value(element) for element in value]
        raise ValueError(
            f"{type(self).__name__}: cannot serialize {type(value).__name__} value"
            f" {value!r} as JSON — use JSON-representable types or add a serialize="
            " callable to the field"
        )

    def parse(self, response: Response) -> R_co:
        """
        Turn the HTTP response into the operation's typed result:
        :py:meth:`check` the status, then :py:meth:`load` the payload.
        This is what :py:meth:`action0.client.api.APIClient.send` attaches
        to the backend's result via ``map``.

        :param response: the response the backend produced
        :return: the parsed result
        :raises action0.client.errors.APIError: if the response is not
                usable (unexpected status, malformed payload, ...)
        """
        self.check(response)
        return self.load(response)

    def check(self, response: Response) -> None:
        """
        Verify the response is one this operation can load — by default
        any 2xx passes and everything else raises. Override for per-status
        handling (say, mapping 404 to a domain exception, or accepting
        3xx).

        :param response: the response the backend produced
        :raises action0.client.errors.APIError: if the status is not 2xx
        """
        if not response.is_success:
            message = f"{type(self).__name__}: unexpected status {response.status}"
            raise APIError(
                f"{message} {response.phrase}".rstrip(),
                request=response.request,
                response=response,
            )

    @abstractmethod
    def load(self, response: Response) -> R_co:
        """
        Turn a checked response into the typed result — the one method a
        concrete operation must provide (:py:class:`JsonOperation`
        implements it for JSON payloads).

        :param response: the response, already vetted by :py:meth:`check`
        :return: the parsed result
        :raises action0.client.errors.APIError: if the payload cannot be
                parsed
        """


class JsonOperation(Operation[R_co]):
    """
    An :py:class:`Operation` against a JSON endpoint: requests advertise
    ``Accept: application/json``, and :py:meth:`load` decodes the response
    body as JSON before handing it to :py:meth:`load_json`.

    Used directly with ``Any`` (or a JSON-ish alias) as result type, the
    decoded payload comes back as-is::

        >>> from typing import Any
        >>> from action0.req import Method, Response
        >>> class SearchItems(JsonOperation[Any]):
        ...     path = "/items"
        ...     q: str = query()
        >>> operation = SearchItems(q="thing")
        >>> operation.as_request("https://api.example.com").url.as_str()
        'https://api.example.com/items?q=thing'
        >>> operation.parse(Response(200, body='{"hits": 2}'))
        {'hits': 2}

    For a *typed* result, choose the result type and override
    :py:meth:`load_json`::

        >>> from dataclasses import dataclass
        >>> @dataclass
        ... class Item:
        ...     id: int
        ...     name: str
        >>> class GetItem(JsonOperation[Item]):
        ...     path = "/items/{item_id}"
        ...     item_id: int = path_param()
        ...
        ...     def load_json(self, data: Any) -> Item:
        ...         return Item(id=data["id"], name=data["name"])
        >>> GetItem(item_id=1).parse(Response(200, body='{"id": 1, "name": "Thing"}'))
        Item(id=1, name='Thing')

    Sending a JSON body is a matter of field specifiers, not of this class
    — see :py:func:`~action0.client.fields.json_field` /
    :py:func:`~action0.client.fields.json_body`::

        >>> class CreateItem(JsonOperation[Item]):
        ...     method = Method.POST
        ...     path = "/items"
        ...
        ...     name: str = json_field()
        ...     tags: list[str] | None = json_field(default=None)
        ...
        ...     def load_json(self, data: Any) -> Item:
        ...         return Item(id=data["id"], name=data["name"])
        >>> request = CreateItem(name="Thing").as_request("https://api.example.com")
        >>> request.body
        '{"name": "Thing"}'
        >>> request.headers["Content-Type"]
        'application/json'
    """

    accept = "application/json"

    def load(self, response: Response) -> R_co:
        """
        Decode the response body as JSON and delegate to
        :py:meth:`load_json`.

        :param response: the response, already vetted by :py:meth:`check`
        :return: the parsed result
        :raises action0.client.errors.APIError: if the body is empty or no
                valid JSON
        """
        text = response.body_str()
        if text is None or not text.strip():
            raise APIError(
                f"{type(self).__name__}: expected a JSON body, got none",
                request=response.request,
                response=response,
            )
        try:
            data = json.loads(text)
        except ValueError as error:
            raise APIError(
                f"{type(self).__name__}: malformed JSON body ({error})",
                request=response.request,
                response=response,
            ) from error
        return self.load_json(data)

    def load_json(self, data: Any) -> R_co:
        """
        Turn the decoded JSON payload into the typed result. The default
        returns the payload unchanged — which is only type-correct for
        ``JsonOperation[Any]`` (or a JSON-ish result type); override it
        whenever the result type is a real model.

        :param data: the decoded JSON payload
        :return: the parsed result
        :raises action0.client.errors.APIError: if the payload does not
                have the expected shape
        """
        return cast("R_co", data)

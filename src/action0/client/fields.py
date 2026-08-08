"""
The field specifiers of :py:class:`~action0.client.operation.Operation`:
they declare *where* in the HTTP request an operation field goes.

An operation is a dataclass; its fields describe the variable parts of the
endpoint. Each field is placed into the request according to its specifier::

    class SearchItems(JsonOperation[Any]):
        method = Method.GET
        path = "/items/{shelf}"

        shelf: str = path_param()  # into the path template
        q: str = query()  # ?q=...
        page_size: int = query("pageSize", default=25)  # renamed on the wire
        locale: str | None = header("Accept-Language", default=None)


    class CreateItem(JsonOperation[Any]):
        method = Method.POST
        path = "/items"

        name: str = json_field()  # key in the JSON body object
        tags: list[str] = json_field(default_factory=list)

A field without a specifier uses the operation's ``default_location``
(query parameters unless a subclass overrides it), so simple query-only
operations need no specifiers at all. Fields whose value is ``None`` are
omitted from the request everywhere.

The specifiers are `dataclass_transform field specifiers
<https://peps.python.org/pep-0681/>`_: type checkers understand ``default``
/ ``default_factory`` exactly like in :py:func:`dataclasses.field`. (The
wire-name parameter is called ``name``, not ``alias``, on purpose — PEP 681
reserves ``alias`` for renaming the ``__init__`` parameter, which is not
what a wire name means.)
"""

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import Callable

_METADATA_KEY = "action0-client"
"""The :py:attr:`dataclasses.Field.metadata` key under which the
:py:class:`FieldSpec` is stored."""


class Location(Enum):
    """Where in the HTTP request an operation field is placed."""

    QUERY = "query"
    """A query parameter (``?name=value``)."""

    HEADER = "header"
    """A header field."""

    PATH = "path"
    """A value for a ``{placeholder}`` in the operation's path template."""

    JSON_FIELD = "json-field"
    """A key of the JSON object sent as the request body."""

    JSON_BODY = "json-body"
    """The entire request body, serialized as JSON."""

    FORM_FIELD = "form-field"
    """A key of the ``application/x-www-form-urlencoded`` request body."""

    BODY = "body"
    """The entire request body, raw: ``bytes``, ``str`` or a
    :py:class:`~action0.req.body.BodyProducer`."""


@dataclass(frozen=True)
class FieldSpec:
    """
    The request-placement description attached to an operation field —
    what the specifier functions of this module produce (in the field
    metadata under ``"action0-client"``).
    """

    location: Location
    """Where in the request the field goes."""

    alias: str | None = None
    """The name on the wire (query parameter name, header name, JSON key);
    ``None`` uses the field name."""

    serialize: Callable[[Any], Any] | None = None
    """A custom serializer applied to the value before the standard value
    coercion; ``None`` uses the standard coercion only."""


def _field(spec: FieldSpec, default: Any, default_factory: Any, repr: bool) -> Any:
    """
    Create the :py:func:`dataclasses.field` carrying a spec.

    :param spec: the placement description
    :param default: the field default (:py:data:`dataclasses.MISSING` for a
                    required field)
    :param default_factory: the field default factory
    :param repr: whether the field shows up in ``repr()`` — pass ``False``
                 for secrets
    :return: the dataclass field
    """
    return dataclasses.field(
        default=default,
        default_factory=default_factory,
        repr=repr,
        metadata={_METADATA_KEY: spec},
    )


def query(
    name: str | None = None,
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as a query parameter.

    A list/tuple/set value produces one ``name=value`` pair per element;
    enums are sent as their ``value``; ``None`` omits the parameter.

    :param name: the parameter name on the wire; ``None`` uses the field name
    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default (for mutable
                            defaults like lists)
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()``
    :return: the dataclass field
    """
    spec = FieldSpec(Location.QUERY, alias=name, serialize=serialize)
    return _field(spec, default, default_factory, repr)


def header(
    name: str | None = None,
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as a header.

    Since field names cannot contain ``-``, most header fields want an
    alias: ``token: str = header("X-API-Key", repr=False)``. A list value
    produces one header line per element; ``None`` omits the header.

    :param name: the header name on the wire; ``None`` uses the field name
    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()`` —
                 pass ``False`` for credentials
    :return: the dataclass field
    """
    spec = FieldSpec(Location.HEADER, alias=name, serialize=serialize)
    return _field(spec, default, default_factory, repr)


def path_param(
    *,
    default: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field filling the ``{placeholder}`` of the same
    name in the operation's ``path`` template. The value must serialize to
    a single scalar and (unlike everywhere else) must not be ``None``.

    :param default: the field default; without one the field is required
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()``
    :return: the dataclass field
    """
    spec = FieldSpec(Location.PATH, serialize=serialize)
    return _field(spec, default, dataclasses.MISSING, repr)


def json_field(
    name: str | None = None,
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as one key of the JSON object request
    body. All ``json_field`` fields of an operation together form that
    object; ``None`` values are omitted. Cannot be combined with
    :py:func:`json_body` or :py:func:`body`.

    :param name: the JSON key on the wire; ``None`` uses the field name
    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default (for mutable
                            defaults like lists)
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()``
    :return: the dataclass field
    """
    spec = FieldSpec(Location.JSON_FIELD, alias=name, serialize=serialize)
    return _field(spec, default, default_factory, repr)


def form_field(
    name: str | None = None,
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as one key of an
    ``application/x-www-form-urlencoded`` request body — the classic HTML
    form POST (and the shape of OAuth token endpoints). All ``form_field``
    fields of an operation together form that body; values serialize like
    query parameters (a list produces one ``name=value`` pair per element,
    ``None`` omits the key). Cannot be combined with the JSON body
    specifiers or :py:func:`body`.

    :param name: the form key on the wire; ``None`` uses the field name
    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default (for mutable
                            defaults like lists)
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()`` —
                 pass ``False`` for credentials (e.g. OAuth client secrets)
    :return: the dataclass field
    """
    spec = FieldSpec(Location.FORM_FIELD, alias=name, serialize=serialize)
    return _field(spec, default, default_factory, repr)


def json_body(
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    serialize: Callable[[Any], Any] | None = None,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as the entire request body, serialized
    as JSON (dataclasses, mappings, sequences, enums, dates and scalars all
    work — see
    :py:meth:`~action0.client.operation.Operation.serialize_json_value`).
    At most one per operation; cannot be combined with :py:func:`json_field`
    or :py:func:`body`.

    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default
    :param serialize: a custom serializer applied to the value first
    :param repr: whether the field shows up in the operation's ``repr()``
    :return: the dataclass field
    """
    spec = FieldSpec(Location.JSON_BODY, serialize=serialize)
    return _field(spec, default, default_factory, repr)


def body(
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    repr: bool = True,
) -> Any:
    """
    Declare an operation field sent as the entire request body, raw. The
    value must be ``bytes``, ``str`` or a streaming
    :py:class:`~action0.req.body.BodyProducer` (i.e. an
    :py:data:`action0.req.body.BodyTypes`). At most one per operation;
    cannot be combined with the JSON body specifiers. Remember to also
    declare a ``Content-Type`` (e.g. via a :py:func:`header` field or the
    client's default headers).

    :param default: the field default; without one the field is required
    :param default_factory: a factory producing the default
    :param repr: whether the field shows up in the operation's ``repr()``
    :return: the dataclass field
    """
    spec = FieldSpec(Location.BODY)
    return _field(spec, default, default_factory, repr)

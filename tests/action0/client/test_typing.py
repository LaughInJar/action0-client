"""
Static typing assertions — the core promise of this library is that the
return type of ``send`` follows the backend, so that promise is pinned
here with :py:func:`typing.assert_type` and verified by every type checker
run (mypy, pyright and ty all check ``tests/``).

The ``check_*`` functions are deliberately *never executed* (they would
perform network I/O): ``assert_type`` is a static assertion, the functions
merely give the checkers call sites to analyze. pytest collects this
module but finds no ``test_*`` functions in it.
"""

from dataclasses import dataclass
from typing import Any
from typing import Awaitable
from typing import assert_type

from twisted.internet.defer import Deferred

from action0.client import APIClient
from action0.client import Client
from action0.client import JsonOperation
from action0.client import path_param
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.requests import RequestsBackend
from action0.client.backends.twisted import TwistedBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.req import Request
from action0.req import Response


@dataclass
class Item:
    """The parsed result the example operation produces."""

    id: int
    name: str


class GetItem(JsonOperation[Item]):
    """The example operation: fetch one item."""

    path = "/items/{item_id}"

    item_id: int = path_param()

    def load_json(self, data: Any) -> Item:
        """
        :param data: the decoded JSON payload
        :return: the payload as an Item
        """
        return Item(id=data["id"], name=data["name"])


def check_client_sync(request: Request) -> None:
    """A sync backend makes Client.send return the plain Response."""
    assert_type(Client(RequestsBackend()).send(request), Response)
    assert_type(Client(HttpxBackend()).send(request), Response)
    assert_type(Client(StubBackend()).send(request), Response)
    # the backend keeps its concrete type
    assert_type(Client(RequestsBackend()).backend, RequestsBackend)


def check_client_async(request: Request) -> None:
    """An async backend makes Client.send return an awaitable Response."""
    assert_type(Client(AsyncHttpxBackend()).send(request), Awaitable[Response])
    assert_type(Client(AsyncStubBackend()).send(request), Awaitable[Response])


async def check_client_awaited(request: Request) -> None:
    """Awaiting the async result yields the plain Response."""
    response = await Client(AsyncHttpxBackend()).send(request)
    assert_type(response, Response)


def check_client_deferred(request: Request) -> None:
    """A Twisted backend makes Client.send return a Deferred Response."""
    assert_type(Client(TwistedBackend()).send(request), Deferred[Response])
    assert_type(Client(DeferredStubBackend()).send(request), Deferred[Response])


def check_api_client_sync() -> None:
    """A sync backend makes APIClient.send return the parsed result."""
    client = APIClient(RequestsBackend(), "https://api.example.com")
    assert_type(client.send(GetItem(item_id=1)), Item)


def check_api_client_async() -> None:
    """An async backend wraps the parsed result in an awaitable."""
    client = APIClient(AsyncHttpxBackend(), "https://api.example.com")
    assert_type(client.send(GetItem(item_id=1)), Awaitable[Item])


async def check_api_client_awaited() -> None:
    """Awaiting the async result yields the parsed result."""
    client = APIClient(AsyncHttpxBackend(), "https://api.example.com")
    item = await client.send(GetItem(item_id=1))
    assert_type(item, Item)


def check_api_client_deferred() -> None:
    """A Twisted backend wraps the parsed result in a Deferred."""
    client = APIClient(TwistedBackend(), "https://api.example.com")
    assert_type(client.send(GetItem(item_id=1)), Deferred[Item])


def check_only_backends_accepted() -> None:
    """Things that aren't backends are rejected at construction."""
    Client(object())  # type: ignore
    APIClient(object(), "https://api.example.com")  # type: ignore

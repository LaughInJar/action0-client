"""
A complete example client for a fictional "PetStore" API, showing the
whole action0-client workflow:

- typed result models (plain dataclasses — bring pydantic etc. if you like),
- one :py:class:`~action0.client.operation.Operation` subclass per
  endpoint, with the HTTP method and path fixed on the class and the
  variable parts as typed fields,
- an :py:class:`~action0.client.api.APIClient` subclass fixing base URL
  and authentication,
- and the same client driven synchronously, with asyncio, and with
  Twisted — only the backend changes.

Run it (no network involved, the demo uses the stub backend)::

    uv run python examples/petstore.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from action0.client import APIClient
from action0.client import BackendT_co
from action0.client import JsonOperation
from action0.client import Operation
from action0.client import json_field
from action0.client import path_param
from action0.client import query
from action0.req import Method
from action0.req import Response

# ---------------------------------------------------------------------------
# The result models — what the API client hands to the application.


@dataclass
class Pet:
    """One pet of the store."""

    id: int
    name: str
    tag: str | None = None


def _pet(data: Any) -> Pet:
    """
    Build a Pet from one decoded JSON object.

    :param data: the decoded JSON object
    :return: the pet
    """
    return Pet(id=data["id"], name=data["name"], tag=data.get("tag"))


# ---------------------------------------------------------------------------
# The operations — one class per endpoint. Method and path are fixed on the
# class; query/path/body parameters are typed fields.


class GetPet(JsonOperation[Pet]):
    """``GET /pets/{pet_id}`` — fetch one pet."""

    method = Method.GET
    path = "/pets/{pet_id}"

    pet_id: int = path_param()

    def load_json(self, data: Any) -> Pet:
        """
        :param data: the decoded JSON payload
        :return: the pet
        """
        return _pet(data)


class SearchPets(JsonOperation[list[Pet]]):
    """``GET /pets`` — search pets; ``None`` fields are simply not sent."""

    method = Method.GET
    path = "/pets"

    q: str | None = query(default=None)
    tag: str | None = query(default=None)
    limit: int = query(default=20)

    def load_json(self, data: Any) -> list[Pet]:
        """
        :param data: the decoded JSON payload
        :return: the matching pets
        """
        return [_pet(item) for item in data["items"]]


class CreatePet(JsonOperation[Pet]):
    """``POST /pets`` — create a pet from a JSON object body."""

    method = Method.POST
    path = "/pets"

    name: str = json_field()
    tag: str | None = json_field(default=None)

    def load_json(self, data: Any) -> Pet:
        """
        :param data: the decoded JSON payload
        :return: the created pet (with its server-assigned id)
        """
        return _pet(data)


class DeletePet(Operation[None]):
    """
    ``DELETE /pets/{pet_id}`` — delete a pet.

    The endpoint answers 204 without a body, so this is a plain
    :py:class:`~action0.client.operation.Operation` (JsonOperation would
    insist on a JSON body): the 2xx check still runs, ``load`` has nothing
    to do.
    """

    method = Method.DELETE
    path = "/pets/{pet_id}"

    pet_id: int = path_param()

    def load(self, response: Response) -> None:
        """
        :param response: the (already vetted) response
        :return: nothing — a 2xx is all we wanted
        """
        return None


# ---------------------------------------------------------------------------
# The client — fixes base URL and auth; generic over the backend so the
# typed send() overloads keep working for every execution model.


class PetStoreClient(APIClient[BackendT_co]):
    """The PetStore API client: base URL and bearer auth baked in."""

    def __init__(
        self,
        backend: BackendT_co,
        token: str,
        base_url: str = "https://petstore.example.com/v1",
    ) -> None:
        """
        :param backend: any sync, async or Twisted backend
        :param token: the PetStore API token
        :param base_url: the API root (override e.g. for a sandbox)
        """
        super().__init__(backend, base_url, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# The same client in the three execution models.


def sync_usage() -> None:
    """The client with the (sync) requests backend."""
    from action0.client.backends.requests import RequestsBackend

    with RequestsBackend() as backend:
        client = PetStoreClient(backend, token="hunter2")
        pet: Pet = client.send(GetPet(pet_id=42))
        print(pet)


async def async_usage() -> None:
    """The same operations with the async httpx backend."""
    from action0.client.backends.httpx import AsyncHttpxBackend

    async with AsyncHttpxBackend() as backend:
        client = PetStoreClient(backend, token="hunter2")
        pets: list[Pet] = await client.send(SearchPets(q="pony"))
        print(pets)


async def aiohttp_usage() -> None:
    """The same operations with the aiohttp backend — drop-in for httpx."""
    from action0.client.backends.aiohttp import AiohttpBackend

    async with AiohttpBackend() as backend:
        client = PetStoreClient(backend, token="hunter2")
        pets: list[Pet] = await client.send(SearchPets(q="pony"))
        print(pets)


def parallel_usage() -> None:
    """Fan out requests from plain sync code via the thread-pool backend."""
    from concurrent.futures import Future

    from action0.client.backends.futures import ThreadPoolBackend
    from action0.client.backends.requests import RequestsBackend

    with RequestsBackend() as inner, ThreadPoolBackend(inner) as backend:
        client = PetStoreClient(backend, token="hunter2")
        futures: list[Future[Pet]] = [client.send(GetPet(pet_id=pet_id)) for pet_id in range(3)]
        print([future.result() for future in futures])


def twisted_usage() -> None:
    """The same operations on Twisted, as Deferreds."""
    from typing import cast

    from twisted.internet import reactor as _reactor
    from twisted.internet.defer import Deferred

    from action0.client.backends.twisted import TwistedBackend

    # the global reactor is a zope-interface object type checkers can't
    # follow — treat it as Any, like most typed twisted code does
    reactor = cast(Any, _reactor)

    client = PetStoreClient(TwistedBackend(), token="hunter2")
    deferred: Deferred[Pet] = client.send(CreatePet(name="Twilight", tag="pony"))
    deferred.addCallback(print)
    deferred.addBoth(lambda _: reactor.stop())
    reactor.run()


# ---------------------------------------------------------------------------
# A runnable, network-free demo: the stub backend answers instead of a
# server — which is also exactly how an application would test its code.


def demo() -> None:
    """Exercise the client against canned responses and print the results."""
    from action0.client.testing import StubBackend

    backend = StubBackend(
        Response(200, body='{"id": 42, "name": "Fluttershy", "tag": "pony"}'),
        Response(200, body='{"items": [{"id": 1, "name": "Rainbow Dash"}]}'),
        Response(201, body='{"id": 43, "name": "Twilight", "tag": "pony"}'),
        Response(204),
    )
    client = PetStoreClient(backend, token="hunter2")

    print(client.send(GetPet(pet_id=42)))
    print(client.send(SearchPets(q="dash")))
    print(client.send(CreatePet(name="Twilight", tag="pony")))
    print(client.send(DeletePet(pet_id=42)))
    print("requests sent:")
    for request in backend.requests:
        print("  ", request.method, request.url.as_str())


if __name__ == "__main__":
    demo()

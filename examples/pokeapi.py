"""
A small client for the real, public `PokéAPI <https://pokeapi.co>`_,
complementing :file:`examples/petstore.py` (which owns authentication,
request bodies and the three execution models with a fictional API):

- operations against a live server — ``GET /pokemon/{name}`` and the
  paginated ``GET /pokemon`` listing,
- a pagination generator looping the listing by bumping ``offset``,
- backend composition: caching around retries around the HTTP backend.
  PokéAPI's fair-use policy asks clients to cache locally, which is
  exactly what :py:class:`~action0.client.caching.CachingSyncBackend`
  does.

Run the network-free demo (the stub backend answers, as in CI)::

    uv run python examples/pokeapi.py

Run it against the real API (manual only — never in CI)::

    uv run python examples/pokeapi.py --live
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from typing import Iterator

from action0.client import APIClient
from action0.client import BackendT_co
from action0.client import CachingSyncBackend
from action0.client import JsonOperation
from action0.client import RetryingSyncBackend
from action0.client import SyncBackend
from action0.client import path_param
from action0.client import query
from action0.req import Method

# ---------------------------------------------------------------------------
# The result models — small typed views onto PokéAPI's (much larger) JSON.


@dataclass
class Pokemon:
    """One Pokémon, reduced to the fields this client cares about."""

    id: int
    name: str
    height: int  # decimetres, as the API reports it
    weight: int  # hectograms, as the API reports it
    types: list[str]


@dataclass
class PokemonPage:
    """One page of the Pokémon listing."""

    count: int
    """How many Pokémon exist in total (across all pages)."""

    names: list[str]
    """The names on this page."""


# ---------------------------------------------------------------------------
# The operations.


class GetPokemon(JsonOperation[Pokemon]):
    """``GET /pokemon/{name}`` — fetch one Pokémon by name (or id)."""

    method = Method.GET
    path = "/pokemon/{name}"

    name: str = path_param()

    def load_json(self, data: Any) -> Pokemon:
        """
        :param data: the decoded JSON payload
        :return: the Pokémon, with the nested ``types`` list flattened
                 to plain names
        """
        return Pokemon(
            id=data["id"],
            name=data["name"],
            height=data["height"],
            weight=data["weight"],
            types=[entry["type"]["name"] for entry in data["types"]],
        )


class ListPokemon(JsonOperation[PokemonPage]):
    """``GET /pokemon`` — one page of the listing, offset-paginated."""

    method = Method.GET
    path = "/pokemon"

    limit: int = query(default=20)
    offset: int = query(default=0)

    def load_json(self, data: Any) -> PokemonPage:
        """
        :param data: the decoded JSON payload
        :return: the page
        """
        return PokemonPage(
            count=data["count"],
            names=[entry["name"] for entry in data["results"]],
        )


# ---------------------------------------------------------------------------
# The client — no auth here (PokéAPI is open); see the petstore example
# for baking in a token.


class PokeAPIClient(APIClient[BackendT_co]):
    """The PokéAPI client: base URL baked in."""

    def __init__(self, backend: BackendT_co, base_url: str = "https://pokeapi.co/api/v2") -> None:
        """
        :param backend: any sync, async or Twisted backend
        :param base_url: the API root
        """
        super().__init__(backend, base_url)


def iter_pokemon(client: PokeAPIClient[SyncBackend], page_size: int = 100) -> Iterator[str]:
    """
    All Pokémon names, page by page: the offset-pagination loop.

    :param client: a client on a synchronous backend
    :param page_size: how many names to fetch per request
    :return: a generator yielding every name; it stops requesting as
             soon as the caller stops consuming
    """
    offset = 0
    while True:
        page = client.send(ListPokemon(limit=page_size, offset=offset))
        yield from page.names
        offset += page_size
        if offset >= page.count:
            return


# ---------------------------------------------------------------------------
# Composed backend: cache hits skip everything below them, so caching
# goes outermost and retries sit between the cache and the network.


def composed_backend(inner: SyncBackend) -> CachingSyncBackend:
    """
    :param inner: the backend doing the actual HTTP
    :return: the same backend wrapped in retries, wrapped in a cache
    """
    return CachingSyncBackend(RetryingSyncBackend(inner))


def live() -> None:
    """Run against the real https://pokeapi.co — manual use only."""
    from itertools import islice

    from action0.client.backends.requests import RequestsBackend

    with RequestsBackend() as network:
        client = PokeAPIClient(composed_backend(network))

        pikachu = client.send(GetPokemon(name="pikachu"))
        print(pikachu)
        client.send(GetPokemon(name="pikachu"))  # answered from the cache

        print(list(islice(iter_pokemon(client, page_size=5), 7)))


# ---------------------------------------------------------------------------
# The network-free demo (run in CI): the stub backend answers, and also
# proves the cache short-circuits the second identical send.


def demo() -> None:
    """Exercise the client against canned responses and print the results."""
    from action0.client.testing import StubBackend
    from action0.req import Response

    stub = StubBackend(
        Response(
            200,
            body=(
                '{"id": 25, "name": "pikachu", "height": 4, "weight": 60,'
                ' "types": [{"slot": 1, "type": {"name": "electric"}}]}'
            ),
        ),
        Response(
            200,
            body=(
                '{"count": 3, "results":'
                ' [{"name": "bulbasaur"}, {"name": "charmander"}, {"name": "squirtle"}]}'
            ),
        ),
    )
    client = PokeAPIClient(composed_backend(stub))

    print(client.send(GetPokemon(name="pikachu")))
    print(client.send(GetPokemon(name="pikachu")))  # cache hit, stub not asked again
    print(list(iter_pokemon(client, page_size=3)))
    print("requests that reached the (stub) network:")
    for request in stub.requests:
        print("  ", request.method, request.url.as_str())


if __name__ == "__main__":
    live() if "--live" in sys.argv[1:] else demo()

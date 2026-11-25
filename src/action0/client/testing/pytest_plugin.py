"""
The pytest plugin of action0-client: the ``action0_mock`` fixture.

pytest loads it by itself (it is registered as a ``pytest11`` entry
point of the package), so a test only has to name the fixture. This
module is the one place in the library that imports pytest — which is
therefore no dependency of action0-client: without pytest installed,
nothing ever imports this module.

Example::

    def test_item_is_fetched(action0_mock):
        action0_mock.get("https://api.example.com/v1/items/7").respond(json={"id": 7})

        assert fetch_item(7).id == 7  # application code, whatever its backend

The router's options are set with a marker::

    @pytest.mark.action0_mock(base_url="https://api.example.com/v1", assert_all_called=False)
    def test_listing(action0_mock):
        action0_mock.get("/items").respond(json=[])
        ...
"""

from collections.abc import Iterator
from typing import Any

import pytest

from ._router import MockRouter


def pytest_configure(config: Any) -> None:
    """
    Register the marker, so that ``--strict-markers`` accepts it.

    :param config: the pytest configuration
    """
    config.addinivalue_line(
        "markers",
        "action0_mock(assert_all_called=True, assert_all_mocked=True, base_url=None,"
        " block_network=False): options for the MockRouter of the action0_mock fixture",
    )


@pytest.fixture
def action0_mock(request: Any) -> Iterator[MockRouter]:
    """
    An active :py:class:`~action0.client.testing.MockRouter` for the
    duration of the test: every action0 backend answers from its routes.

    When the test ends, the router's checks run as on leaving a ``with``
    block — every route called, no request unmocked — unless the test
    has already failed.

    :param request: pytest's fixture request (for the marker)
    :return: the router to add routes to
    """
    marker = request.node.get_closest_marker("action0_mock")
    options = dict(marker.kwargs) if marker is not None else {}
    with MockRouter(**options) as router:
        yield router

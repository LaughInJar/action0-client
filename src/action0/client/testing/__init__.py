"""
Test doubles for writing tests against API clients — yours or ones built
with this library — without any network I/O.

There are two ways to use them, sharing one API:

- **Mock the whole process** with a :py:class:`MockRouter` (as a context
  manager, or through the ``action0_mock`` pytest fixture): while it is
  active, every backend answers from the router's routes instead of
  sending. The tests do not depend on which backend the application
  uses — or on being able to reach it.
- **Inject a stub backend** (:py:class:`StubBackend`,
  :py:class:`AsyncStubBackend`, :py:class:`DeferredStubBackend`) into the
  code under test. A stub answers from a script of responses and from the
  routes of its own ``router``.

Either way the requests are recorded, and the regular
:py:class:`~action0.client.hooks.Hook` machinery, error translation and
the retrying and caching wrappers run for real.

Example::

    >>> from action0.req import Request, Response
    >>>
    >>> backend = StubBackend(Response(200, body="pong"))
    >>> backend.send(Request("https://api.example.com/ping")).body_str()
    'pong'
    >>> backend.requests[0]
    Request(GET https://api.example.com/ping)
"""

from ._router import Call
from ._router import CallList
from ._router import ChunkedBody
from ._router import MockRouter
from ._router import NetworkBlockedError
from ._router import Route
from ._router import UnmockedRequestError
from ._stubs import AsyncStubBackend
from ._stubs import DeferredStubBackend
from ._stubs import Responder
from ._stubs import StubBackend
from ._stubs import deferred_result

__all__ = [
    "AsyncStubBackend",
    "Call",
    "CallList",
    "ChunkedBody",
    "DeferredStubBackend",
    "MockRouter",
    "NetworkBlockedError",
    "Responder",
    "Route",
    "StubBackend",
    "UnmockedRequestError",
    "deferred_result",
]

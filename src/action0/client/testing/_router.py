"""
The mock router: decides, for a request a backend is about to send, what
comes back — without the backend, and therefore without any network I/O.

It is used in two ways that share one API:

- **activated for the whole process** (``with MockRouter() as mock:`` or
  the ``action0_mock`` pytest fixture): every backend built on the base
  classes hands its requests to the router instead of doing I/O. Tests
  written this way do not care which backend the application uses.
- **inside the stub backends**, each of which owns a private router that
  is never activated globally.

Hooks, error translation and the retrying and caching wrappers are not
affected: the router replaces only the innermost step, the I/O.
"""

import inspect
import json as jsonlib
import re
import socket
import threading
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import parse_qsl
from urllib.parse import unquote
from urllib.parse import urlsplit

from action0.req import Request
from action0.req import Response
from action0.req.body import BodyTypes
from action0.req.headers import HeaderTypes

from .. import backend as backend_module
from ..backends._body import adeliver
from ..backends._body import adeliver_bytes
from ..backends._body import body_mode
from ..backends._body import deliver
from ..backends._body import deliver_bytes
from ..errors import TransportError
from ..options import BodyMode

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

_UNSET: Any = object()
"""Marks "no JSON given" where ``None`` is a JSON value of its own."""

_LOOKUPS = frozenset(
    {
        "scheme",
        "host",
        "port",
        "path",
        "path__startswith",
        "path__regex",
        "params",
        "headers",
        "content",
        "json",
        "data",
    }
)
"""The keyword lookups a route can match on, besides method and URL."""

SideEffect = Any
"""What a route can be told to do instead of returning a fixed response:
an exception (class or instance) to raise, a callable taking the request
(sync or async) and returning a response, or an iterable of responses,
exceptions and callables to go through one per request."""


class UnmockedRequestError(AssertionError):
    """
    A request reached the router and no route matches it (or the route's
    sequence of answers is used up). Raised from the backend's ``send``,
    and — because application code may well catch it there — reported
    again when the router is left.

    An ``AssertionError``, so test runners show it as a failed test and
    no ``except TransportError`` or retry policy mistakes it for a
    network problem.
    """


class NetworkBlockedError(AssertionError, OSError):
    """
    Code tried to open a network connection while a router with
    ``block_network=True`` was active — some request went around the
    router, e.g. through a backend that is not built on the base
    classes or through an HTTP library used directly.

    Also an ``OSError``, which is what a failed ``connect()`` raises:
    HTTP libraries then clean up after it the way they do after any
    refused connection — and usually wrap it in an error of their own.
    The attempt is therefore recorded as well, and leaving the router
    reports it whatever became of the exception.
    """


class ChunkedBody:
    """
    A response body for mocked responses that arrives in the given
    pieces — and, if one of the pieces is an exception, breaks off at
    that point. For testing code that consumes streams, and code that
    recovers from downloads that fail midway.

    How it reaches the application depends on the body mode the request
    asks for, exactly as with a real backend: preloaded (joined into
    bytes), streamed (the chunks as given) or spooled. A break raises
    from ``send`` for a preloaded or spooled body, and while the body is
    read for a streamed one.

    Example::

        >>> from action0.client import TransportError
        >>> body = ChunkedBody(b"first, ", b"second", TransportError("connection lost"))
        >>> received = []
        >>> try:
        ...     for chunk in body.chunks():
        ...         received.append(chunk)
        ... except TransportError as error:
        ...     print("broke off:", error)
        broke off: connection lost
        >>> received
        [b'first, ', b'second']

    It is a :py:class:`~action0.req.body.BodyProducer`, re-iterable (it
    breaks at the same point every time).
    """

    def __init__(self, *chunks: "bytes | BaseException") -> None:
        """
        :param chunks: the pieces of the body in order; an exception
                       instance ends the body by being raised
        """
        self._chunks = chunks

    def content_length(self) -> "int | None":
        """
        :return: always ``None`` — a body that may break off has no
                 length to promise
        """
        return None

    def chunks(self) -> Iterator[bytes]:
        """
        :return: the chunks as given
        :raises BaseException: the exception among the chunks, when the
                               iteration reaches it
        """
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def achunks(self) -> AsyncIterator[bytes]:
        """
        :return: the chunks as given, asynchronously
        :raises BaseException: the exception among the chunks, when the
                               iteration reaches it
        """
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def as_bytes(self) -> bytes:
        """
        :return: all chunks joined
        :raises BaseException: the exception among the chunks, if any
        """
        return b"".join(self.chunks())


@dataclass(frozen=True)
class Call:
    """One request that reached a router, and what became of it."""

    request: Request
    """The request as the backend was about to send it (after its
    ``on_request`` hooks)."""

    response: "Response | None"
    """The response the router answered with; ``None`` if it raised."""

    exception: "BaseException | None"
    """What the router raised instead of answering; ``None`` if it
    answered."""

    route: "Route | None"
    """The route that matched; ``None`` for an unmatched request."""


class CallList(list[Call]):
    """
    The calls recorded by a route or a router, in order — a list with a
    few conveniences for assertions.
    """

    @property
    def called(self) -> bool:
        """Whether there was at least one call."""
        return bool(self)

    @property
    def call_count(self) -> int:
        """The number of calls."""
        return len(self)

    @property
    def last(self) -> Call:
        """
        The most recent call.

        :raises AssertionError: if there was none
        """
        if not self:
            raise AssertionError("expected at least one call, there was none")
        return self[-1]

    def assert_called_once(self) -> None:
        """
        :raises AssertionError: unless there was exactly one call
        """
        if len(self) != 1:
            raise AssertionError(f"expected exactly one call, there were {len(self)}")

    def assert_not_called(self) -> None:
        """
        :raises AssertionError: if there was a call
        """
        if self:
            raise AssertionError(f"expected no call, there were {len(self)}")


def _pairs(value: Any) -> "list[tuple[str, str]]":
    """
    Normalize query or form parameters to a list of string pairs.

    :param value: a mapping (values may be lists for repeated names), a
                  sequence of pairs, or a query string
    :return: the name/value pairs
    """
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        return parse_qsl(text, keep_blank_values=True)
    items = value.items() if isinstance(value, Mapping) else value
    pairs: "list[tuple[str, str]]" = []
    for name, entry in items:
        entries = entry if isinstance(entry, (list, tuple)) else [entry]
        pairs.extend((str(name), str(single)) for single in entries)
    return pairs


def _template_regex(path: str) -> "re.Pattern[str] | None":
    """
    Compile a path with ``{name}`` placeholders into a regular
    expression capturing each placeholder as one path segment.

    :param path: the path, e.g. ``"/items/{item_id}"``
    :return: the pattern; ``None`` if the path has no placeholder
    """
    if "{" not in path:
        return None
    parts = re.split(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
    pattern = ""
    for index, part in enumerate(parts):
        # the split alternates literal text and placeholder names
        pattern += f"(?P<{part}>[^/]+)" if index % 2 else re.escape(part)
    return re.compile(pattern)


class _Pattern:
    """
    What a route matches on: the method, the parts of the URL, and
    optionally headers and body.
    """

    def __init__(
        self,
        method: "str | None",
        url: "str | None",
        lookups: "dict[str, Any]",
        base_url: "str | None",
    ) -> None:
        """
        :param method: the method to match; ``None`` matches every one
        :param url: a full URL, or a path (then every host matches,
                    unless the router has a base URL); its path may
                    contain ``{name}`` placeholders, its query string
                    adds to ``params``
        :param lookups: the keyword lookups, see :py:meth:`MockRouter.route`
        :param base_url: the router's base URL, prepended to a relative
                         ``url``
        :raises TypeError: on an unknown lookup
        """
        unknown = sorted(set(lookups) - _LOOKUPS)
        if unknown:
            raise TypeError(
                f"unknown lookup(s) {', '.join(unknown)} — a route matches on"
                f" {', '.join(sorted(_LOOKUPS))}"
            )
        self.method = method.upper() if method is not None else None
        self.scheme: "str | None" = lookups.get("scheme")
        host = lookups.get("host")
        self.host: "str | None" = host.lower() if host is not None else None
        self.port: "int | None" = lookups.get("port")
        self.path: "str | None" = None
        self.path_prefix: "str | None" = lookups.get("path__startswith")
        self.path_regex: "re.Pattern[str] | None" = None
        self.params = _pairs(lookups.get("params", ()))
        self.headers = [
            (name.lower(), value) for name, value in _pairs(lookups.get("headers", ()))
        ]
        self.content: "bytes | None" = None
        if "content" in lookups:
            content = lookups["content"]
            self.content = content.encode() if isinstance(content, str) else bytes(content)
        self.json: Any = lookups.get("json", _UNSET)
        self.data = sorted(_pairs(lookups["data"])) if "data" in lookups else None
        self.described = self._describe(method, url, lookups)

        if url is not None:
            self._apply_url(url, base_url)
        elif base_url is not None and "path" in lookups:
            self._apply_url(lookups["path"], base_url)
            return
        if "path" in lookups:
            self._apply_path(lookups["path"])
        if "path__regex" in lookups:
            regex = lookups["path__regex"]
            self.path_regex = re.compile(regex) if isinstance(regex, str) else regex

    @staticmethod
    def _describe(method: "str | None", url: "str | None", lookups: "dict[str, Any]") -> str:
        """
        :return: the pattern as it was written, for messages
        """
        parts = [method.upper() if method else "ANY", url or "*"]
        parts.extend(f"{name}={value!r}" for name, value in lookups.items())
        return " ".join(parts)

    def _apply_url(self, url: str, base_url: "str | None") -> None:
        """
        Take scheme, host, port, path and query from a URL (or just path
        and query from a relative one).

        :param url: the URL or path given to the route
        :param base_url: the router's base URL for a relative ``url``
        """
        if base_url is not None and "://" not in url:
            url = base_url.rstrip("/") + "/" + url.lstrip("/")
        parts = urlsplit(url)
        if parts.scheme:
            self.scheme = parts.scheme
        if parts.hostname:
            self.host = parts.hostname.lower()
        if parts.port is not None:
            self.port = parts.port
        if parts.path:
            self._apply_path(parts.path)
        elif parts.hostname:
            # "https://api.example.com" means its root
            self._apply_path("/")
        self.params = parse_qsl(parts.query, keep_blank_values=True) + self.params

    def _apply_path(self, path: str) -> None:
        """
        Match the path exactly, or as a template if it has placeholders.

        :param path: the (possibly percent-encoded) path
        """
        decoded = unquote(path)
        template = _template_regex(decoded)
        if template is not None:
            self.path_regex = template
        else:
            self.path = decoded

    def match(self, request: Request) -> "dict[str, str] | None":
        """
        Check a request against the pattern.

        :param request: the request a backend is about to send
        :return: the captured path placeholders (an empty dict if there
                 are none) for a match, ``None`` otherwise
        """
        url = request.url
        if self.method is not None and request.method != self.method:
            return None
        if self.scheme is not None and url.scheme != self.scheme:
            return None
        if self.host is not None and (url.hostname or "").lower() != self.host:
            return None
        if self.port is not None and url.port != self.port:
            return None
        path = url.path or "/"
        if self.path is not None and path != self.path:
            return None
        if self.path_prefix is not None and not path.startswith(self.path_prefix):
            return None
        captured: "dict[str, str]" = {}
        if self.path_regex is not None:
            found = self.path_regex.fullmatch(path)
            if found is None:
                return None
            captured = found.groupdict()
        for name, value in self.params:
            if value not in url.query.get_all(name):
                return None
        for name, value in self.headers:
            if value not in request.headers.get_all(name):
                return None
        if not self._body_matches(request):
            return None
        return captured

    def _body_matches(self, request: Request) -> bool:
        """
        Compare the request body, if the pattern says anything about it.
        Reading the body of a request with a streaming producer consumes
        it — only patterns with a body lookup do that.

        :param request: the request to check
        :return: whether the body matches
        """
        if self.content is None and self.json is _UNSET and self.data is None:
            return True
        body = request.body_bytes() or b""
        if self.content is not None and body != self.content:
            return False
        if self.json is not _UNSET:
            try:
                if jsonlib.loads(body) != self.json:
                    return False
            except ValueError:
                return False
        if self.data is not None:
            try:
                sent = sorted(parse_qsl(body.decode(), keep_blank_values=True))
            except UnicodeDecodeError:
                return False
            if sent != self.data:
                return False
        return True


def _build_response(
    status: int,
    *,
    headers: "HeaderTypes | None",
    content: "bytes | str | None",
    text: "str | None",
    json: Any,
    html: "str | None",
    chunks: "Iterable[bytes | BaseException] | None",
    reason: "str | None",
) -> Response:
    """
    Build the response a route answers with from the arguments of
    :py:meth:`Route.respond`.

    :raises TypeError: if more than one kind of body is given
    """
    given = [
        name
        for name, value in (
            ("content", content),
            ("text", text),
            ("html", html),
            ("chunks", chunks),
        )
        if value is not None
    ]
    if json is not _UNSET:
        given.append("json")
    if len(given) > 1:
        raise TypeError(f"a response has one body, got {' and '.join(given)}")

    response = Response(status, headers=headers, reason=reason)
    body: "BodyTypes | None" = None
    content_type: "str | None" = None
    if json is not _UNSET:
        body, content_type = jsonlib.dumps(json), "application/json"
    elif text is not None:
        body, content_type = text, "text/plain; charset=utf-8"
    elif html is not None:
        body, content_type = html, "text/html; charset=utf-8"
    elif chunks is not None:
        body = ChunkedBody(*chunks)
    elif content is not None:
        body = content
    response.body = body
    if content_type is not None and "Content-Type" not in response.headers:
        response.headers["Content-Type"] = content_type
    return response


class _PassThrough:
    """The answer of a route that lets the request go to the backend."""


_PASS_THROUGH = _PassThrough()


class Route:
    """
    One rule of a :py:class:`MockRouter`: which requests it matches, and
    what they are answered with. Created by :py:meth:`MockRouter.route`
    and its shortcuts (``get``, ``post``, ...); the answer is set with
    :py:meth:`respond`, :py:meth:`mock` or :py:meth:`pass_through`, all
    of which return the route for chaining.

    A route without an answer responds with an empty ``200``.
    """

    def __init__(self, pattern: _Pattern, name: "str | None" = None) -> None:
        """
        :param pattern: what the route matches on
        :param name: an optional name, to find the route again via
                     :py:attr:`MockRouter.routes`
        """
        self._pattern = pattern
        self.name = name
        self.calls = CallList()
        """The calls this route answered, in order."""
        self._return_value: "Response | None" = None
        self._side_effect: SideEffect = None
        self._sequence: "Iterator[Any] | None" = None
        self._sequence_length = 0
        self._pass_through = False
        self._lock = threading.Lock()

    @property
    def called(self) -> bool:
        """Whether the route answered at least one request."""
        return self.calls.called

    @property
    def call_count(self) -> int:
        """How many requests the route answered."""
        return self.calls.call_count

    @property
    def return_value(self) -> "Response | None":
        """The fixed response of the route (copied for every request)."""
        return self._return_value

    @return_value.setter
    def return_value(self, response: "Response | None") -> None:
        self._return_value = response
        self._pass_through = False

    @property
    def side_effect(self) -> SideEffect:
        """What the route does instead of returning its fixed response —
        see :py:meth:`mock`."""
        return self._side_effect

    @side_effect.setter
    def side_effect(self, effect: SideEffect) -> None:
        self._side_effect = effect
        self._sequence = None
        self._pass_through = False
        if effect is None or callable(effect) or isinstance(effect, BaseException):
            return
        entries = list(effect)
        self._sequence = iter(entries)
        self._sequence_length = len(entries)

    def respond(
        self,
        status: int = 200,
        *,
        headers: "HeaderTypes | None" = None,
        content: "bytes | str | None" = None,
        text: "str | None" = None,
        json: Any = _UNSET,
        html: "str | None" = None,
        chunks: "Iterable[bytes | BaseException] | None" = None,
        reason: "str | None" = None,
    ) -> "Route":
        """
        Answer with a fixed response, described by its parts.

        :param status: the status code
        :param headers: the response headers
        :param content: the body as bytes (or str)
        :param text: the body as text; sets a ``text/plain`` content type
        :param json: a value to send as JSON body; sets
                     ``application/json``
        :param html: the body as HTML; sets ``text/html``
        :param chunks: the body as a :py:class:`ChunkedBody` — pieces
                       that arrive one by one, an exception among them
                       breaking the body off
        :param reason: the reason phrase, if not the standard one
        :return: the route
        :raises TypeError: if more than one kind of body is given
        """
        self.side_effect = None
        self.return_value = _build_response(
            status,
            headers=headers,
            content=content,
            text=text,
            json=json,
            html=html,
            chunks=chunks,
            reason=reason,
        )
        return self

    def mock(
        self, return_value: "Response | None" = None, side_effect: SideEffect = None
    ) -> "Route":
        """
        Answer with a ready-made response and/or a side effect.

        :param return_value: the fixed response (copied for every
                             request)
        :param side_effect: takes precedence over ``return_value``:

                            - an exception class or instance — raised.
                              Use the library's own
                              :py:class:`~action0.client.errors.TimeoutError`
                              / :py:class:`~action0.client.errors.TransportError`
                              to simulate network failures; they pass
                              through the backend like real ones.
                            - a callable taking the request (and, as
                              keyword arguments, the placeholders of a
                              path template if it declares them) —
                              called for every request. It may be
                              ``async``. Returning ``None`` falls back
                              to ``return_value``.
                            - an iterable of responses, exceptions and
                              callables — one per request, in order. A
                              request beyond the last entry is an
                              :py:class:`UnmockedRequestError`.
        :return: the route
        """
        self.return_value = return_value
        self.side_effect = side_effect
        return self

    def pass_through(self, value: bool = True) -> "Route":
        """
        Let matching requests go to the backend itself — the real
        network for a real backend, the script of a stub backend.

        :param value: ``False`` turns it off again
        :return: the route
        """
        self._pass_through = value
        return self

    def _next_answer(self, request: Request, captured: "dict[str, str]") -> Any:
        """
        Decide what this request gets.

        :param request: the request
        :param captured: the placeholders captured from the path
        :return: a response (still to be copied/delivered), an awaitable
                 of one (an async callable), ``_PASS_THROUGH``, or
                 ``None`` for "the default response"
        :raises BaseException: the exception the route is told to raise
        """
        if self._pass_through:
            return _PASS_THROUGH
        effect = self._side_effect
        if self._sequence is not None:
            with self._lock:
                effect = next(self._sequence, _UNSET)
            if effect is _UNSET:
                raise UnmockedRequestError(
                    f"{request!r} matched the route {self._pattern.described}, whose"
                    f" sequence of {self._sequence_length} answer(s) is used up"
                )
        if effect is None:
            return self._return_value
        if isinstance(effect, type) and issubclass(effect, BaseException):
            effect = _instantiate(effect, self._pattern.described)
        if isinstance(effect, BaseException):
            if isinstance(effect, TransportError):
                # like a real backend's error, it knows its request
                effect.request = request
            raise effect
        if isinstance(effect, Response):
            return effect
        if callable(effect):
            answer = _call(effect, request, captured)
            return self._return_value if answer is None else answer
        raise TypeError(f"a route cannot answer with {effect!r}")

    def _or_fixed(self, answer: Any) -> Any:
        """
        :param answer: what an (awaited) async callable returned
        :return: the answer, or the fixed response if it was ``None``
        """
        return self._return_value if answer is None else answer

    def __repr__(self) -> str:
        """
        :return: the route with what it matches and its call count
        """
        return f"Route({self._pattern.described}, {self.call_count} calls)"


def _instantiate(error: "type[BaseException]", route: str) -> BaseException:
    """
    Create the exception for a route told to raise an exception class.

    :param error: the exception class
    :param route: the route, for the message
    :return: an instance — with a message where the class takes one
    """
    try:
        return error(f"raised by the mock route {route}")
    except TypeError:
        return error()


def _call(effect: Callable[..., Any], request: Request, captured: "dict[str, str]") -> Any:
    """
    Call a side-effect callable, passing the path placeholders it asks
    for by name.

    :param effect: the callable
    :param request: the request
    :param captured: the placeholders captured from the path
    :return: whatever it returned (possibly an awaitable)
    """
    if not captured:
        return effect(request)
    try:
        parameters = inspect.signature(effect).parameters
    except (TypeError, ValueError):
        return effect(request)
    takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    wanted = {name: value for name, value in captured.items() if takes_any or name in parameters}
    return effect(request, **wanted)


def _run_to_completion(awaitable: Any) -> Any:
    """
    Get the result of an async side effect for a backend that is not
    async: step the coroutine once. That is enough for a coroutine that
    never really waits, which is all a canned answer should need.

    :param awaitable: the awaitable the side effect returned
    :return: its result
    :raises TypeError: if it suspends
    """
    steps = awaitable.__await__()
    try:
        next(steps)
    except StopIteration as finished:
        return finished.value
    close = getattr(steps, "close", None)
    if close is not None:
        close()
    raise TypeError(
        "an async side effect answering a synchronous or Twisted backend must not wait"
        " for anything: it is run without an event loop"
    )


#: the routers activated for the whole process, innermost last
_active: "list[MockRouter]" = []
_activation_lock = threading.Lock()
#: how many active routers block the network, and the patched functions
_blockers = 0
_real_connect: Any = None
_real_connect_ex: Any = None


class MockRouter:
    """
    A set of routes answering requests in place of the network.

    Activated as a context manager it applies to the whole process: for
    its duration every backend built on
    :py:class:`~action0.client.backend.BaseSyncBackend`,
    :py:class:`~action0.client.backend.BaseAsyncBackend` or
    :py:class:`~action0.client.backend.BaseDeferredBackend` — all the
    shipped ones — passes its requests to the router instead of sending
    them. The application's backend does not have to be injected,
    replaced or even known.

    Example::

        >>> from action0.client import APIClient, JsonOperation, path_param
        >>> from action0.client.backends.urllib import UrllibBackend
        >>>
        >>> class GetItem(JsonOperation[dict]):
        ...     path = "/items/{item_id}"
        ...     item_id: int = path_param()
        >>>
        >>> client = APIClient(UrllibBackend(), "https://api.example.com/v1")
        >>> with MockRouter() as mock:
        ...     route = mock.get("https://api.example.com/v1/items/{item_id}")
        ...     _ = route.respond(200, json={"id": 7, "name": "Thing"})
        ...     client.send(GetItem(item_id=7))
        {'id': 7, 'name': 'Thing'}
        >>> route.calls.last.request
        Request(GET https://api.example.com/v1/items/7)

    A request that no route matches raises an
    :py:class:`UnmockedRequestError` — a test can never reach the
    network by accident. Leaving the ``with`` block checks (unless an
    exception is already on its way) that every route was called and
    that no request went unmatched; the latter matters because
    application code may have swallowed the error.

    The switch is global rather than tied to the current context on
    purpose: application code crosses threads and event loops (worker
    pools, ``async_to_sync`` bridges, a reactor thread), and a
    context-local mock would silently not apply there — the test would
    talk to the real service.
    """

    def __init__(
        self,
        *,
        assert_all_called: bool = True,
        assert_all_mocked: bool = True,
        base_url: "str | None" = None,
        block_network: bool = False,
    ) -> None:
        """
        :param assert_all_called: whether leaving the ``with`` block
                                  fails for a route that was never
                                  called
        :param assert_all_mocked: whether a request that no route matches
                                  is an error (:py:class:`UnmockedRequestError`
                                  from ``send``, and again when the
                                  block is left). ``False`` lets such
                                  requests through to the backend.
        :param base_url: prepended to route URLs given as paths, so
                         ``mock.get("/items")`` matches only that API
        :param block_network: additionally make every attempt to open a
                              network connection fail with a
                              :py:class:`NetworkBlockedError` while the
                              router is active — a net for requests that
                              do not go through a backend at all
        """
        self._assert_all_called = assert_all_called
        self._assert_all_mocked = assert_all_mocked
        self._base_url = base_url
        self._block_network = block_network
        self._routes: "list[Route]" = []
        self._fallback: "Route | None" = None
        self.calls = CallList()
        """Every request that reached the router, in order — matched or
        not."""
        self._blocked: "list[str]" = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- routes

    @property
    def routes(self) -> "list[Route]":
        """The routes in the order they were added — which is the order
        they are tried in; the first match answers."""
        return list(self._routes)

    def __getitem__(self, name: str) -> Route:
        """
        :param name: the name a route was given
        :return: that route
        :raises KeyError: if there is none
        """
        for route in self._routes:
            if route.name == name:
                return route
        raise KeyError(name)

    def route(
        self,
        method: "str | None" = None,
        url: "str | None" = None,
        *,
        name: "str | None" = None,
        **lookups: Any,
    ) -> Route:
        """
        Add a route. Everything given must match; what is left out
        matches anything — ``mock.route()`` matches every request.

        :param method: the HTTP method
        :param url: a full URL, or just a path. Its path may contain
                    ``{name}`` placeholders, each matching one path
                    segment; a query string adds to ``params``.
        :param name: a name to find the route by (``mock["name"]``)
        :param lookups: further conditions:

                        - ``scheme``, ``host``, ``port``
                        - ``path`` (exact, or a ``{name}`` template),
                          ``path__startswith``, ``path__regex``
                        - ``params``: query parameters that must be
                          present with these values (others may be too)
                        - ``headers``: headers that must be present with
                          these values (names are case-insensitive)
                        - ``content``: the exact body; ``json``: the body
                          parsed as JSON equals this value; ``data``: the
                          body is this form data
        :return: the new route — set its answer with
                 :py:meth:`Route.respond` or :py:meth:`Route.mock`
        :raises TypeError: on an unknown lookup
        """
        route = Route(_Pattern(method, url, lookups, self._base_url), name)
        self._routes.append(route)
        return route

    def get(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``GET`` requests."""
        return self.route("GET", url, **lookups)

    def post(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``POST`` requests."""
        return self.route("POST", url, **lookups)

    def put(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``PUT`` requests."""
        return self.route("PUT", url, **lookups)

    def patch(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``PATCH`` requests."""
        return self.route("PATCH", url, **lookups)

    def delete(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``DELETE`` requests."""
        return self.route("DELETE", url, **lookups)

    def head(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``HEAD`` requests."""
        return self.route("HEAD", url, **lookups)

    def options(self, url: "str | None" = None, **lookups: Any) -> Route:
        """A :py:meth:`route` for ``OPTIONS`` requests."""
        return self.route("OPTIONS", url, **lookups)

    def reset(self) -> None:
        """
        Forget the recorded calls of the router and its routes; the
        routes and their answers stay.
        """
        with self._lock:
            self.calls.clear()
            self._blocked.clear()
            for route in self._all_routes():
                route.calls.clear()

    def _all_routes(self) -> "list[Route]":
        """
        :return: the routes including the fallback, if there is one
        """
        return self._routes + ([self._fallback] if self._fallback is not None else [])

    # --------------------------------------------------------- assertions

    @property
    def unmatched(self) -> "list[Request]":
        """The requests no route matched."""
        return [call.request for call in self.calls if call.route is None]

    def assert_all_called(self) -> None:
        """
        :raises AssertionError: if a route was never called
        """
        idle = [route for route in self._routes if not route.called]
        if idle:
            listed = "\n  ".join(repr(route) for route in idle)
            raise AssertionError(f"{len(idle)} route(s) were never called:\n  {listed}")

    def assert_all_mocked(self) -> None:
        """
        :raises UnmockedRequestError: if a request reached the router
                that no route matched — or, with ``block_network``, a
                connection attempt went around it
        """
        unmatched = self.unmatched
        if unmatched:
            listed = "\n  ".join(repr(request) for request in unmatched)
            raise UnmockedRequestError(f"{len(unmatched)} request(s) were not mocked:\n  {listed}")
        self._assert_nothing_blocked()

    def _assert_nothing_blocked(self) -> None:
        """
        :raises NetworkBlockedError: if, with ``block_network``, a
                connection attempt was refused — whatever the code that
                made it did with the error
        """
        if self._blocked:
            listed = "\n  ".join(self._blocked)
            raise NetworkBlockedError(
                f"{len(self._blocked)} connection attempt(s) went around the router:\n  {listed}"
            )

    # --------------------------------------------------------- activation

    def start(self) -> None:
        """
        Activate the router for the whole process. Prefer the ``with``
        form, which cannot forget to :py:meth:`stop`. Routers nest: the
        one started last answers until it is stopped.
        """
        global _blockers, _real_connect, _real_connect_ex
        with _activation_lock:
            _active.append(self)
            backend_module._mock = self
            if self._block_network:
                if _blockers == 0:
                    _real_connect = socket.socket.connect
                    _real_connect_ex = socket.socket.connect_ex
                    # setattr: patching a method of a class is nothing
                    # the type checkers have a spelling for
                    setattr(socket.socket, "connect", _blocked_connect)
                    setattr(socket.socket, "connect_ex", _blocked_connect)
                _blockers += 1

    def stop(self) -> None:
        """
        Deactivate the router (without any of the checks the ``with``
        form makes). Does nothing if it is not active.
        """
        global _blockers
        with _activation_lock:
            if self not in _active:
                return
            _active.remove(self)
            backend_module._mock = _active[-1] if _active else None
            if self._block_network:
                _blockers -= 1
                if _blockers == 0:
                    setattr(socket.socket, "connect", _real_connect)
                    setattr(socket.socket, "connect_ex", _real_connect_ex)

    def __enter__(self) -> "MockRouter":
        """
        :return: the router, now active
        """
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """
        Deactivate the router and — unless an exception is already
        leaving the block — run the checks it was configured with.
        """
        self.stop()
        if exc_type is not None:
            return
        self._assert_nothing_blocked()
        if self._assert_all_mocked:
            self.assert_all_mocked()
        if self._assert_all_called:
            self.assert_all_called()

    # ----------------------------------------------------------- dispatch

    def _find(self, request: Request) -> "tuple[Route | None, dict[str, str]]":
        """
        :param request: the request to answer
        :return: the first matching route (or the fallback) and the path
                 placeholders it captured
        """
        for route in self._routes:
            captured = route._pattern.match(request)
            if captured is not None:
                return route, captured
        return self._fallback, {}

    def _record(
        self,
        route: "Route | None",
        request: Request,
        response: "Response | None" = None,
        exception: "BaseException | None" = None,
    ) -> None:
        """
        Remember what became of a request.
        """
        call = Call(request, response, exception, route)
        with self._lock:
            self.calls.append(call)
            if route is not None:
                route.calls.append(call)

    def _unmatched(self, request: Request) -> UnmockedRequestError:
        """
        :param request: a request no route matches
        :return: the error to raise for it
        """
        routes = "\n  ".join(repr(route) for route in self._routes) or "(none)"
        return UnmockedRequestError(
            f"{request!r} is not mocked. The routes, in the order they are tried:\n  {routes}"
        )

    def _answer(self, backend: Any, request: Request) -> "tuple[Route | None, Any]":
        """
        The model-independent first half of a dispatch: find the route
        and get its answer.

        :param backend: the backend the request came through
        :param request: the request
        :return: the route and its raw answer — a response, an
                 awaitable, ``_PASS_THROUGH`` or ``None`` (the default
                 response)
        :raises BaseException: what the route raises; an
                               :py:class:`UnmockedRequestError` if no
                               route matches
        """
        route, captured = self._find(request)
        if route is None:
            if self._assert_all_mocked:
                error = self._unmatched(request)
                self._record(None, request, exception=error)
                raise error
            return None, _PASS_THROUGH
        try:
            return route, route._next_answer(request, captured)
        except BaseException as error:
            self._record(route, request, exception=error)
            raise

    @staticmethod
    def _prepared(answer: Any, request: Request) -> Response:
        """
        Turn a route's answer into the response object for this request:
        a fixed response is copied (so that requests cannot affect each
        other), and every response knows its request.

        :param answer: a response, or ``None`` for the default one
        :param request: the request being answered
        :return: the response, its body not yet in the requested mode
        """
        if answer is None:
            return Response(200, request=request)
        if not isinstance(answer, Response):
            raise TypeError(f"a route must answer with a Response, got {answer!r}")
        return answer.copy(request=request)

    def _mode(self, backend: Any, request: Request) -> BodyMode:
        """
        :param backend: the backend the request came through
        :param request: the request
        :return: the body mode a real send would use
        """
        return body_mode(request, bool(getattr(backend, "_stream", False)))

    def _dispatch_sync(self, backend: Any, request: Request) -> Response:
        """
        Answer a request for a synchronous backend.

        :param backend: the backend whose ``send`` was called
        :param request: the request, after the ``on_request`` hooks
        :return: the response
        """
        route, answer = self._answer(backend, request)
        if answer is _PASS_THROUGH:
            passed: Response = backend._send(request)
            self._record(route, request, response=passed)
            return passed
        try:
            if inspect.isawaitable(answer):
                assert route is not None
                answer = route._or_fixed(_run_to_completion(answer))
            response = self._prepared(answer, request)
            response.body = _in_mode(self._mode(backend, request), request, response)
        except BaseException as error:
            self._record(route, request, exception=error)
            raise
        self._record(route, request, response=response)
        return response

    async def _dispatch_async(self, backend: Any, request: Request) -> Response:
        """
        Answer a request for an async backend.

        :param backend: the backend whose ``send`` was called
        :param request: the request, after the ``on_request`` hooks
        :return: the response
        """
        route, answer = self._answer(backend, request)
        if answer is _PASS_THROUGH:
            passed: Response = await backend._send(request)
            self._record(route, request, response=passed)
            return passed
        try:
            if inspect.isawaitable(answer):
                assert route is not None
                answer = route._or_fixed(await answer)
            response = self._prepared(answer, request)
            response.body = await _ain_mode(self._mode(backend, request), request, response)
        except BaseException as error:
            self._record(route, request, exception=error)
            raise
        self._record(route, request, response=response)
        return response

    def _dispatch_deferred(self, backend: Any, request: Request) -> "Deferred[Response]":
        """
        Answer a request for a Twisted backend: with a Deferred that has
        already fired. Errors are raised; the backend's ``send`` turns
        them into a failed Deferred like any error of ``_send``.

        :param backend: the backend whose ``send`` was called
        :param request: the request, after the ``on_request`` hooks
        :return: a fired Deferred with the response
        """
        from twisted.internet.defer import succeed

        route, answer = self._answer(backend, request)
        if answer is _PASS_THROUGH:
            passing: "Deferred[Response]" = backend._send(request)

            def record(passed: Response) -> Response:
                self._record(route, request, response=passed)
                return passed

            return passing.addCallback(record)
        try:
            mode = self._mode(backend, request)
            if mode is BodyMode.STREAM and not getattr(backend, "_can_stream", True):
                # the same refusal the backend itself would give
                backend._refuse_stream()
            if inspect.isawaitable(answer):
                assert route is not None
                answer = route._or_fixed(_run_to_completion(answer))
            response = self._prepared(answer, request)
            response.body = _in_mode(mode, request, response)
        except BaseException as error:
            self._record(route, request, exception=error)
            raise
        self._record(route, request, response=response)
        return succeed(response)


def _identity(error: Exception, request: Request) -> BaseException:
    """
    The "translation" of errors raised by a mocked body: none — they are
    what the test put there.
    """
    return error


def _nothing() -> None:
    """A mocked body holds no connection to release."""


async def _anothing() -> None:
    """A mocked body holds no connection to release."""


def _in_mode(mode: BodyMode, request: Request, response: Response) -> "BodyTypes | None":
    """
    The body of a mocked response the way a real backend would hand it
    over in the given mode.

    :param mode: the body mode of the request
    :param request: the request being answered
    :param response: the prepared response
    :return: the body to put on the response
    :raises BaseException: the exception inside a :py:class:`ChunkedBody`,
                           for the modes that read the body right away
    """
    body = response.body
    if isinstance(body, ChunkedBody):
        if mode is BodyMode.PRELOAD:
            return body.as_bytes()
        return deliver(request, mode, body.chunks(), _nothing, _identity)
    return deliver_bytes(request, response, mode)


async def _ain_mode(mode: BodyMode, request: Request, response: Response) -> "BodyTypes | None":
    """
    :py:func:`_in_mode` for async backends: streamed bodies are async
    producers, spools are filled without blocking the loop.
    """
    body = response.body
    if isinstance(body, ChunkedBody):
        if mode is BodyMode.PRELOAD:
            return body.as_bytes()
        return await adeliver(request, mode, body.achunks(), _anothing, _identity)
    return await adeliver_bytes(request, response, mode)


def _blocked_connect(sock: socket.socket, address: Any) -> Any:
    """
    The stand-in for ``socket.connect`` / ``connect_ex`` while a router
    blocks the network: local (unix domain) sockets still connect,
    everything else is refused and reported to the active routers.

    :param sock: the socket
    :param address: where it wants to connect
    :raises NetworkBlockedError: for a network address
    """
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return _real_connect(sock, address)
    attempt = f"connection to {address!r}"
    for router in list(_active):
        if router._block_network:
            router._blocked.append(attempt)
    raise NetworkBlockedError(
        f"{attempt} blocked: a MockRouter with block_network=True is active, and this"
        " request did not go through it"
    )

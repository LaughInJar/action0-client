"""
Event-loop affinity for the async backends.

The clients of the async HTTP libraries — ``httpx.AsyncClient``,
``aiohttp.ClientSession`` — keep connections that are bound to the event
loop they were opened on. A backend object that outlives one loop (a
module-level client used with ``asyncio.run`` per call, or from worker
threads that each run their own loop) therefore cannot hold a single
client. Two helpers deal with that:

- :py:class:`PerLoop` keeps one object per running loop, created on
  first use and closed when its loop shuts down.
- :py:class:`LoopBound` guards a single object the caller passed in: it
  remembers the loop of the first use and refuses any other with a clear
  error.

Under trio there is no asyncio loop to key on; both helpers then behave
as if there were one loop for the whole process.
"""

import asyncio
import threading
import weakref
from dataclasses import dataclass
from typing import Any
from typing import AsyncGenerator
from typing import Awaitable
from typing import Callable
from typing import Generic
from typing import TypeVar

T = TypeVar("T")


def running_loop() -> "asyncio.AbstractEventLoop | None":
    """
    :return: the running asyncio event loop; ``None`` when called from
             outside one — in particular from a trio task
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


@dataclass
class _Entry(Generic[T]):
    """What :py:class:`PerLoop` keeps for one event loop."""

    loop: "weakref.ref[asyncio.AbstractEventLoop] | None"
    """The loop the object belongs to — weakly, so a dead loop can be
    told apart from a new one that happens to get the same ``id()``."""

    value: T
    """The object created for the loop."""

    guard: "AsyncGenerator[None, None] | None"
    """The async generator whose ``finally`` closes the object; the loop
    finalizes it on shutdown. ``None`` outside of asyncio."""

    def alive_on(self, loop: "asyncio.AbstractEventLoop | None") -> bool:
        """
        :param loop: the loop asking for its object
        :return: whether this entry is that loop's (and not a leftover
                 of a dead loop with the same id)
        """
        if self.loop is None:
            return loop is None
        return loop is not None and self.loop() is loop

    def stale(self) -> bool:
        """
        :return: whether the entry's loop is gone or closed
        """
        if self.loop is None:
            return False
        loop = self.loop()
        return loop is None or loop.is_closed()


class PerLoop(Generic[T]):
    """
    One object per running event loop: created by a factory on first use
    in a loop, handed out again for every later use in the same loop,
    and closed when the loop shuts down.

    The closing rides on a mechanism every asyncio loop has: before a
    loop is closed, ``loop.shutdown_asyncgens()`` finalizes the async
    generators that were started on it (``asyncio.run`` and
    ``asyncio.Runner`` do that). Each object is held by such a generator,
    whose ``finally`` block closes it — inside its own loop, while that
    loop can still do I/O. A loop that is closed without that step leaves
    its object to the garbage collector; its entry is dropped the next
    time this instance is used.

    Safe to use from several threads, each running its own loop.
    """

    def __init__(self, factory: Callable[[], T], aclose: Callable[[T], Awaitable[None]]) -> None:
        """
        :param factory: creates the object for a loop; called inside
                        that loop
        :param aclose: closes an object; awaited inside the object's
                       loop
        """
        self._factory = factory
        self._aclose = aclose
        self._entries: "dict[int, _Entry[T]]" = {}
        self._lock = threading.Lock()

    async def get(self) -> T:
        """
        The object of the running loop, created if this is the first use
        in that loop.

        :return: the object
        """
        loop = running_loop()
        key = id(loop)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.alive_on(loop):
                return entry.value
            self._sweep()
            value = self._factory()
            guard = self._guard(key, value) if loop is not None else None
            reference = weakref.ref(loop) if loop is not None else None
            self._entries[key] = _Entry(reference, value, guard)
        if guard is not None:
            # run the generator up to its ``yield``: that registers it
            # with the loop, which will finalize it on shutdown
            await guard.__anext__()
        return value

    async def aclose(self) -> None:
        """
        Close the object of the running loop, if there is one, and drop
        what dead loops left behind. Objects of other loops that are
        still running are not touched — they can only be closed from
        inside their own loop, and are when that loop shuts down.
        """
        loop = running_loop()
        key = id(loop)
        with self._lock:
            self._sweep()
            entry = self._entries.get(key)
            if entry is None or not entry.alive_on(loop):
                return
            if entry.guard is None:
                del self._entries[key]
        if entry.guard is not None:
            # finalizing the generator runs its ``finally``: the one
            # place where an entry is removed and its object closed
            await entry.guard.aclose()
        else:
            await self._aclose(entry.value)

    def __len__(self) -> int:
        """
        :return: the number of loops that currently have an object
        """
        with self._lock:
            return len(self._entries)

    async def _guard(self, key: int, value: T) -> AsyncGenerator[None, None]:
        """
        Hold an object for as long as its loop lives.

        :param key: the entry's key
        :param value: the object to close in the end
        """
        try:
            yield
        finally:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.value is value:
                    del self._entries[key]
            await self._aclose(value)

    def _sweep(self) -> None:
        """
        Drop the entries of loops that are gone. Their objects cannot be
        closed properly any more — the loop they need for that is closed
        — so they are left to the garbage collector. Called with the
        lock held.
        """
        for key in [key for key, entry in self._entries.items() if entry.stale()]:
            del self._entries[key]


class LoopBound:
    """
    The guard for a single loop-bound object that was handed to a
    backend: the first use decides which loop it belongs to, and a use
    from any other loop raises a :py:class:`RuntimeError` that says what
    is wrong and what to do — instead of the HTTP library failing
    somewhere inside with "Event loop is closed" or "attached to a
    different loop".
    """

    def __init__(self, what: str, alternative: str) -> None:
        """
        :param what: the object being guarded, for the message — e.g.
                     ``"the httpx.AsyncClient passed to AsyncHttpxBackend"``
        :param alternative: what to do instead, for the message — e.g.
                            ``"pass client_factory=... instead of client=..."``
        """
        self._what = what
        self._alternative = alternative
        self._loop: "weakref.ref[asyncio.AbstractEventLoop] | None" = None

    def check(self) -> None:
        """
        Call before every use of the guarded object.

        :raises RuntimeError: if the running loop is not the one the
                              object was first used on
        """
        loop = running_loop()
        if loop is None:
            return
        if self._loop is None:
            self._loop = weakref.ref(loop)
            return
        first: Any = self._loop()
        if first is loop:
            return
        state = "has been closed" if first is None or first.is_closed() else "is a different one"
        raise RuntimeError(
            f"{self._what} is bound to the event loop it was first used on, and that loop"
            f" {state}. To share one backend between event loops (asyncio.run() per"
            f" call, or one loop per thread), {self._alternative}: the backend then keeps"
            " one client per loop."
        )

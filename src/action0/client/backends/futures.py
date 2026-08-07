"""
The thread-pool backend — a :py:data:`~action0.client.backend.FuturesBackend`
(``Backend[Future[Response]]``), stdlib-only: it wraps any synchronous
backend and runs its sends on a :py:class:`~concurrent.futures.ThreadPoolExecutor`,
so plain sync code gets parallel requests as
:py:class:`concurrent.futures.Future` results — no async machinery.
"""

from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from typing import TypeVar

from action0.req import Request
from action0.req import Response

from ..backend import Backend

T = TypeVar("T")
S = TypeVar("S")


class ThreadPoolBackend:
    """
    A backend whose execution model is :py:class:`concurrent.futures.Future`:
    every send runs the wrapped synchronous backend on the thread pool.

    ``Client(ThreadPoolBackend(...)).send(request)`` is a
    ``Future[Response]``, and :py:meth:`APIClient.send
    <action0.client.api.APIClient.send>` returns ``Future[R]`` — response
    parsing (and any instrumentation hooks, which belong on the *wrapped*
    backend) runs on the pool threads.

    Example::

        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> with ThreadPoolBackend(StubBackend(Response(200, body="pong"))) as backend:
        ...     future = backend.send(Request("https://api.example.com/ping"))
        ...     future.result().body_str()
        'pong'

    Real-world use — fan out over a shared session, sync code throughout::

        from action0.client.backends.requests import RequestsBackend
        from action0.client.backends.futures import ThreadPoolBackend

        with RequestsBackend() as inner, ThreadPoolBackend(inner) as backend:
            client = APIClient(backend, "https://api.example.com/v1")
            futures = [client.send(GetItem(item_id=item_id)) for item_id in range(100)]
            items = [future.result() for future in futures]  # Future[Item] each
    """

    def __init__(
        self,
        inner: Backend[Response],
        pool: "ThreadPoolExecutor | None" = None,
        *,
        max_workers: "int | None" = None,
    ) -> None:
        """
        :param inner: the synchronous backend that actually sends (put
                      instrumentation hooks there — this wrapper stays out
                      of the way)
        :param pool: the executor to run sends on; ``None`` creates (and
                     owns) one, shut down again by :py:meth:`close`
        :param max_workers: the size of the created pool (``None`` is the
                            executor's default); ignored when a ``pool``
                            is given
        """
        self._inner = inner
        self._pool = (
            pool
            if pool is not None
            else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action0-client")
        )
        self._owns_pool = pool is None

    @property
    def inner(self) -> Backend[Response]:
        """The wrapped synchronous backend doing the actual sends."""
        return self._inner

    def send(self, request: Request) -> "Future[Response]":
        """
        Run the wrapped backend's send on the pool.

        :param request: the request to send
        :return: a Future of the response; transport errors surface when
                 its result is retrieved
        """
        return self._pool.submit(self._inner.send, request)

    def map(self, result: "Future[T]", fn: Callable[[T], S]) -> "Future[S]":
        """
        Apply a function inside a Future result of :py:meth:`send`: the
        returned Future resolves to ``fn`` of the original result, and
        failures (of the send or of ``fn``) propagate. The function runs
        via a done-callback, so no pool thread is spent waiting.

        :param result: a Future as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Future of the return value of ``fn``
        """
        chained: "Future[S]" = Future()

        def propagate(done: "Future[T]") -> None:
            if done.cancelled():
                chained.cancel()
                return
            error = done.exception()
            if error is not None:
                chained.set_exception(error)
                return
            try:
                chained.set_result(fn(done.result()))
            except BaseException as fn_error:  # noqa: BLE001 — must reach the Future
                chained.set_exception(fn_error)

        result.add_done_callback(propagate)
        return chained

    def close(self, wait: bool = True) -> None:
        """
        Shut down the pool — but only if this backend created it; a pool
        that was passed in is left to its owner. The wrapped backend is
        never closed here.

        :param wait: whether to block until running sends finished
        """
        if self._owns_pool:
            self._pool.shutdown(wait=wait)

    def __enter__(self) -> "ThreadPoolBackend":
        """
        :return: the backend itself, closed again when the ``with`` block
                 ends
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """
        Close the backend on leaving the ``with`` block.

        :param exc_info: the exception leaving the block, if any (ignored)
        """
        self.close()

    def __repr__(self) -> str:
        """
        :return: the backend with its wrapped backend, e.g.
                 ``ThreadPoolBackend(StubBackend(0 requests))``
        """
        return f"{self.__class__.__name__}({self._inner!r})"

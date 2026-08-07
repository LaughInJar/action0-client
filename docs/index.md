# action0-client

Backend-agnostic, fully typed HTTP API clients: describe your API once —
as typed operations — and run it synchronously, on asyncio or on Twisted,
just by plugging in a different backend. Built on
[action0-req](https://laughinjar.github.io/action0-req/) (the
request/response representation) and
[action0-url](https://laughinjar.github.io/action0-url/) (the URL
representation).

```shell
uv add "action0-client[httpx]"    # not on PyPI yet — install from GitHub for now
```

**Highlights**:

- One {py:class}`~action0.client.client.Client` /
  {py:class}`~action0.client.api.APIClient`, three execution models: the
  backend decides whether `send()` returns a value, an awaitable or a
  Twisted `Deferred` — and the type checker knows which, including the
  per-operation result type (`Item`, `Awaitable[Item]`, `Deferred[Item]`).
- Backends are {py:class}`~typing.Protocol`s
  ({py:class}`~action0.client.backend.SyncBackend` /
  {py:class}`~action0.client.backend.AsyncBackend` /
  {py:class}`~action0.client.backend.DeferredBackend`) — implement two
  methods and anything can drive the same clients. Built-in:
  [requests](https://requests.readthedocs.io/),
  [httpx](https://www.python-httpx.org/) (sync + async) and
  [Twisted](https://twisted.org/), each behind an optional dependency.
- Endpoints as typed dataclasses:
  {py:class}`~action0.client.operation.Operation` fixes method and path
  per class, the field specifiers of {py:mod}`action0.client.fields`
  place typed fields into query, headers, path templates or the JSON
  body.
- Instrumentation {py:class}`~action0.client.hooks.Hook`s (logging,
  metrics, tracing, request decoration) and uniform error translation
  into one exception family, in every execution model.
- Batteries for testing API clients without a server:
  {py:mod}`action0.client.testing` ships recording stub backends for all
  three execution models.
- Fully typed (checked with mypy strict, pyright and ty), Python 3.11+.

The `action0` namespace is simply the one the author likes to use for
personal projects.

```{toctree}
:maxdepth: 2

usage
api
```

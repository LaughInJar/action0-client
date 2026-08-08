# The pieces

- {py:class}`~action0.req.request.Request` /
  {py:class}`~action0.req.response.Response` (from
  [action0-req](https://laughinjar.github.io/action0-req/)) describe
  *what* is on the wire.
- A **backend** performs the HTTP I/O — through whichever library and
  execution model you chose. Backends implement one structural protocol,
  {py:class}`~action0.client.backend.Backend`, generic over what `send()`
  wraps the response in (`Backend[Response]` is a sync backend,
  `Backend[Awaitable[Response]]` an asyncio one, ...); the built-in ones
  live in {py:mod}`action0.client.backends`.
- {py:class}`~action0.client.client.Client` sends raw requests through a
  backend ("ring 0").
- {py:class}`~action0.client.operation.Operation` +
  {py:class}`~action0.client.api.APIClient` describe *an API*: typed
  endpoint classes in, typed results out ("ring 1").

The key idea: **the backend decides the execution model, and the types
follow it.** With a sync backend `send()` returns the result, with an
async backend an `Awaitable`, with a Twisted backend a `Deferred` — the
same client and operation classes throughout, verified by mypy, pyright
and ty.

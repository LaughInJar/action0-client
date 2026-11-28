# Changelog

All notable changes to action0-client are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- `MockRouter` in `action0.client.testing`: mocking that does not depend
  on the backend. Activated as a context manager — or through the
  `action0_mock` pytest fixture, which a bundled pytest plugin provides
  without making pytest a dependency — it answers the requests of every
  backend built on the base classes, for the whole process. Hooks, error
  translation and the retrying and caching backends still run.
  - Routes match on method, scheme, host, port, path (exact, prefix,
    regex or `{name}` template), a subset of query parameters and
    headers, and the body (`content`, `json`, `data`).
  - A route answers with a fixed response, a sequence, a sync or async
    callable, a raised `TimeoutError` / `TransportError`, or lets the
    request through.
  - `route.calls`, `call_count`, `assert_all_called()`,
    `assert_all_mocked()`; an unmatched request raises
    `UnmockedRequestError` and fails the test again when the router is
    left. `block_network=True` refuses every connection attempt.
  - Mocked responses are delivered in the body mode the request asks
    for. `ChunkedBody` / `respond(chunks=[...])` mocks a body arriving
    in pieces, or breaking off midway.
  - The API follows respx where it fits; the testing guide has a
    comparison table.
- The stub backends have a `router` with the same route API. Routes
  answer what they match, the scripted responses the rest.
- `Httpx2Backend` and `AsyncHttpx2Backend` in
  `action0.client.backends.httpx2`, behind the new `httpx2` extra (also
  part of `all`): the httpx backends for the httpx2 fork, with the same
  options and behaviour. They share one implementation with the httpx
  backends, are precisely typed for `httpx2.Client` /
  `httpx2.AsyncClient`, and work with and without
  `httpx2.alias_httpx()`. Note that httpx2 verifies TLS against the
  operating system's trust store rather than certifi.
- Async backends can be shared between event loops: created without a
  client, `AsyncHttpxBackend`, `AsyncHttpx2Backend` and `AiohttpBackend`
  keep one client per running loop, closed when the loop shuts down.
  `client_factory=` (httpx, httpx2) and `session_factory=` (aiohttp)
  configure those clients.
- `HttpxBackend.client`, `AsyncHttpxBackend.current_client()` and
  `AiohttpBackend.current_session()` (and the httpx2 counterparts).
- Response body handling is chosen per request with
  `RequestOptions(body=BodyMode.PRELOAD | STREAM | SPOOL)`, or once per
  operation via `Operation.request_options`. `SPOOL` writes the body
  into a `SpooledBody` while it arrives — in memory up to
  `spool_max_memory` bytes (default 1 MiB), in a temporary file (in
  `spool_dir`) beyond that — and releases the connection before `send`
  returns. Supported by the requests, httpx, aiohttp, urllib3 and urllib
  backends and through `ThreadPoolBackend`; the Twisted backend supports
  `PRELOAD` and `SPOOL`.
- Async backends never block the event loop while spooling: in-memory
  writes run inline, file writes in a worker thread (asyncio or trio);
  the Twisted backend uses the reactor's thread pool and pauses the
  transport during a write.
- Retries can cover parsing: with
  `RetryPolicy(retry_parse_errors=(PayloadError,))` a response that has
  an acceptable status but cannot be parsed (an HTML page on a 200, a
  truncated document) is thrown away and the request sent again — in
  every execution model, with one attempt counter for transport errors,
  statuses and parse failures.
- `PayloadError`, a subclass of `APIError`, raised by
  `JsonOperation.load` for an empty or malformed body.
  `RetryPolicy.should_retry_parse_error`.
- Caching is decided per request: `RequestOptions(cacheable=...)` makes
  a request cacheable although its method is not GET/HEAD (or keeps one
  out of the cache), `cache_ttl` sets its lifetime and `cache_key`
  replaces the computed key.
- `CachePolicy.max_body_size`: response bodies above this size are not
  stored. `CachePolicy.ttl_for(request)`.
- The caching guide shows adapters for a key-value cache with
  `get` / `set` / ttl, sync and async.
- `RequestOptions`: typed per-request options, stored in `request.meta`
  under the single key `action0-client`. Set them with
  `RequestOptions(...).apply(request)`, read them with
  `RequestOptions.of(request)`.
- `Operation.request_options`: a class attribute holding the options for
  every request of an operation.
- `RequestOptions.retry_safe`: a request or operation can declare itself
  safe to retry although its method is not idempotent (a `POST` search
  or translation endpoint), or unsafe although it is.
  `RetryPolicy.applies_to` honours it.
- `reserved_field_names()`: the names an operation field cannot use, for
  tools that generate operation classes.
- `SyncBackendWrapper`, `AsyncBackendWrapper` and `DeferredBackendWrapper`:
  public base classes for backends that wrap another backend. They
  provide `inner`, a precisely typed `map` and `repr()`; a subclass
  overrides `send`. This is the way to run an awaited step before each
  request, such as a rate limiter, which a synchronous `Hook` cannot do.
- A guide page on backend wrappers, including the stacking order of
  cache, retry and throttle wrappers and what hooks on the inner backend
  see.

### Changed

- `action0.client.testing` is now a package; the import paths of the
  stub backends are unchanged. The stubs are rebuilt on a private
  `MockRouter`. Their constructor, `requests`, responders, hooks and the
  "last response repeats" rule behave as before, with two differences: a
  canned in-memory body is delivered in the body mode the request asks
  for, and a response returned by a responder callable without a
  `request` gets the current one attached.
- While a `MockRouter` is active it also answers the stub backends.
- `AsyncHttpxBackend` and `AiohttpBackend` without a passed-in client
  create their client per event loop on first use. Before,
  `AsyncHttpxBackend` created one client in its constructor, and both
  backends failed with "Event loop is closed" on a second
  `asyncio.run()`. `aclose()` now closes the client of the loop it is
  called in.
- A single client passed to an async backend is bound to the loop of
  its first use. A use from another loop raises a `RuntimeError` that
  names the fix instead of the HTTP library's internal error.
- A client of the other library of the httpx family
  (`httpx2.Client` given to `HttpxBackend`, or the reverse) is refused
  with a `TypeError`.
- `JsonOperation.load` raises `PayloadError` instead of a plain
  `APIError` for an empty or malformed body. `PayloadError` is an
  `APIError`, so existing handlers keep working.
- Stacked retrying backends no longer multiply their attempts: the
  outermost one retries, inner ones pass through. Two default policies
  used to mean up to 9 requests and now mean 3. If you relied on the
  product, raise `attempts` on the outer policy.
- A response that a retry throws away is closed before the wait, so a
  streamed body releases its connection instead of waiting for garbage
  collection.
- A caching backend does not store a response the operation cannot
  parse.
- `APIClient.send` puts the operation's parse step on the request
  (`request.meta["action0-client.parse"]`) for the retrying and caching
  backends. Each response is still parsed exactly once.
- The cache key of a request with a body now includes a digest of the
  body. Before, the body was ignored — harmless for the default GET/HEAD
  policy, but a policy that enabled POST would have answered different
  bodies from one entry. Keys of requests without a body are unchanged,
  so existing cache entries stay valid.
- A request whose body is a streaming producer is not cached unless it
  carries an explicit `cache_key`.
- Responses are handed to a `CacheStore` without their `request`
  reference. The request is not needed to serve a hit, and it carried
  credentials (an `Authorization` header, a password in the URL) and the
  request's `meta` into the store. A custom store that read
  `response.request` now finds `None`.
- A cache hit is delivered in the body mode the request asks for.
- An error while a streamed response body is read (a dropped
  connection, a read timeout) is now raised as `TransportError` /
  `TimeoutError` with the HTTP library's exception as `__cause__`.
  Before, the library's own exception came through, so code handling
  mid-body failures depended on the backend. Code that catches e.g.
  `httpx.ReadError` or `requests.exceptions.ChunkedEncodingError` while
  iterating a body has to catch `TransportError` instead.
- Streamed bodies are closed deterministically: `response.close()`, a
  `with` block or reading to the end releases the connection, also for a
  body that was never read. Garbage collection remains the fallback.
- The backends' `stream=True` argument is now the backend's default
  body mode; a request's own `body` option takes precedence.
- Requires action0-req 0.3.0 (was 0.1.1) and action0-url 0.2.1 (was
  0.1.0), each capped below its next minor release: a minor release of
  a 0.x package may change behaviour. action0-req 0.2.0 provides
  `SpooledBody` and `Response.close()`, which the body modes build on.
  Both releases change behaviour that reaches you through the `Request`,
  `Response` and `Url` objects of this library — a streamed body can be
  read once and says so, header names and values are validated, a `Url`
  without a scheme no longer gets `https`, `repr(request)` redacts
  secret query parameters, and more. See the changelogs of
  [action0-req](https://github.com/LaughInJar/action0-req/blob/main/CHANGELOG.md)
  and
  [action0-url](https://github.com/LaughInJar/action0-url/blob/main/CHANGELOG.md).
  The requests of this library are built as before; in particular
  `Operation.as_request()` without a base URL still gives the path with
  its leading `/`.

### Fixed

- `UrllibBackend` with a streamed body silently returned a truncated
  body when the connection ended before the announced `Content-Length`.
  It now raises `TransportError`, like the other backends.

- The names an operation field cannot use are now derived from the
  operation base classes instead of being listed by hand. Besides
  `method`, `path`, `accept` and `default_location` this covers the new
  `request_options` and the methods `as_request`, `parse`, `check`,
  `load`, `load_json`, `serialize_value` and `serialize_json_value`. A
  field with one of the method names used to be accepted and then failed
  on every send with a `TypeError`; it is now refused when the class is
  defined, with a message showing the fix (`check_: bool =
  query(name="check")`). The set is closed: later per-operation settings
  become fields of `RequestOptions`.
- The `Retrying*Backend` and `Caching*Backend` classes are now
  subclasses of the wrapper base classes. Their constructors and
  behaviour are unchanged, except that `map` is delegated to the wrapped
  backend instead of being reimplemented. For the backends shipped with
  this library the result is the same.

## 0.1.0 — 2026-08-08

First release: the `Backend` protocol and the backends for requests,
httpx, aiohttp, urllib3, urllib, Twisted and thread pools; `Client`,
`Operation`, `JsonOperation` and `APIClient`; hooks; retry and caching
wrappers; and the stub backends for testing.

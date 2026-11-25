# Changelog

All notable changes to action0-client are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

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
- Requires the action0-req release that provides `SpooledBody` and
  `Response.close()`.

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

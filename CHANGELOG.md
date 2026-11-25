# Changelog

All notable changes to action0-client are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

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

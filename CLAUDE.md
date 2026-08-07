# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`action0-client` is a Python library for building backend-agnostic, fully typed HTTP API clients: the same client and operation classes run synchronously, on asyncio or on Twisted — the plugged-in backend decides, and the static types follow it. It ships the `action0.client` package (`action0` is a PEP 420 namespace package) from a `src/` layout, is built with hatchling, and uses `uv` for environment/dependency management. Runtime dependencies are `action0-req` and `action0-url` (not on PyPI yet, resolved from GitHub via `[tool.uv.sources]`); the HTTP libraries (`requests`, `httpx`, `twisted[tls]`) are optional extras and dev-group dependencies.

## Rules

- **Never commit without asking.** Also never push, tag, or publish on your own.
- **Branches + PRs.** All changes go through feature branches and GitHub pull requests that Simon reviews and merges — never commit to `main` directly. (Only the initial implementation was built directly on `main`; that phase is over.)
- **Discuss first.** Always present the plan and the intended edits and get agreement before changing files.
- Every code change comes with: tests, docstrings, inline comments where the code isn't self-explanatory, and updated usage examples in `README.md` and the Sphinx docs (`docs/usage.md`).
- Before considering work done, run ruff, mypy, pyright, ty, and pytest (commands below) and fix what they report.
- Supported Python versions: 3.11 up to the latest release. Don't use syntax or stdlib features introduced after 3.11, and don't rely on behavior removed in newer versions.

## Commands

`uv run` syncs the environment automatically (the dev dependency group, which includes all optional backend libraries, is installed by default), so no separate install step is needed.

```sh
uv run pytest                                        # all tests
uv run pytest tests/action0/client/test_client.py    # one file
uv run pytest tests/action0/client/test_init.py::PackageTestCase::test_version  # one test

uv run ruff check      # lint (add --fix to autofix)
uv run ruff format     # format
uv run mypy            # type-check (strict; files are configured in pyproject.toml)
uv run pyright         # type-check
uv run ty check        # type-check

uv run python examples/petstore.py   # the runnable example (network-free demo)

uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html  # build docs

uv build               # build sdist + wheel into dist/
```

`pytest` also runs the `>>>` examples in the docstrings as doctests (`--doctest-modules` over `src/`), so docstring examples must produce their shown output exactly.

## Architecture

The layout under `src/action0/client/`:

- `backend.py` — the heart of the design. ONE structural protocol, `Backend[SendResultT_co]`, generic over what `send()` wraps the `Response` in (`Response` / `Awaitable[Response]` / `Deferred[Response]` / anything else — `SyncBackend`/`AsyncBackend`/`DeferredBackend` are mere `TypeAlias`es of those instantiations). The second protocol method `map(result, fn)` applies a function *inside* the wrapper (plain call / await / `addCallback`); it is the runtime composition hook for `APIClient` and is typed loosely (`Any`) on the protocol — "the same wrapper around a different value type" is inexpressible for arbitrary wrappers (Python has no higher-kinded types); implementations type their `map` precisely. Also defines the two public TypeVars: `SendResultT_co` (what `Client` is generic over) and `BackendT_co` (bound `Backend[Any]`, what `APIClient` is generic over). Base classes `BaseSyncBackend`/`BaseAsyncBackend`/`BaseDeferredBackend` implement `send` as a template around an abstract `_send` (raw I/O only) adding `Hook` instrumentation and `translate_error`. `BaseDeferredBackend` imports twisted only lazily inside `send`, so the module (and the stub subclass) is importable without twisted.
- `client.py` — `Client[SendResultT_co]`: fully execution-model-agnostic, `send()` returns exactly what the backend's `send` returns — derived from the backend, works for wrapper types the library has never heard of, zero overloads, no twisted imports. Trade-off: `client.backend` is typed as the `Backend` protocol, not the concrete class (users keep their own reference for `close()` etc.).
- `operation.py` — `Operation[R_co]`: `@dataclass_transform` base class; `__init_subclass__` applies `dataclasses.dataclass(kw_only=True)` and validates (reserved names; path placeholders ↔ `path_param()` fields; at most one body form). ClassVars `method`, `path` (template with `{name}` placeholders), `accept`, `default_location` (placement of specifier-less fields, default QUERY). `as_request(base_url)` renders the path template, serializes fields per location (`None` = omitted; enums → value, dates → ISO, bools → true/false, lists → repeated params) and assembles the JSON body (+ Content-Type). `parse()` = `check()` (2xx or `APIError`) + abstract `load()`. `JsonOperation[R_co]` implements `load` via JSON decode + overridable `load_json` (default returns payload as-is, for `JsonOperation[Any]`). No `typing.get_type_hints` anywhere — field placement lives in `dataclasses.field(metadata=...)`, so user modules may freely use `from __future__ import annotations`.
- `fields.py` — the PEP 681 field specifiers `query()`, `header()`, `path_param()`, `json_field()`, `json_body()`, `body()` producing `dataclasses.field(metadata={"action0-client": FieldSpec(...)})`. The wire-name parameter is called `name`, NOT `alias` — PEP 681 reserves `alias` for renaming the `__init__` parameter and ty actually implements that. `repr=False` keeps secrets out of operation reprs.
- `api.py` — `APIClient[BackendT_co]`: backend + `base_url` + default `headers`; pipeline is `prepare(operation.as_request(base_url))` → `backend.map(backend.send(request), operation.parse)` — runtime-agnostic. The typing facade cannot be: "the backend's wrapper around R" needs higher-kinded types, so `send` is `@overload`ed **on the self type** for the shipped models (`self: APIClient[Backend[Response]] -> R`, `...[Backend[Deferred[Response]]] -> Deferred[R]`, `...[Backend[Awaitable[Response]]] -> Awaitable[R]` — Deferred before Awaitable is load-bearing, a Deferred *is* awaitable) plus a final catch-all overload returning `Any`, so backends with other wrappers are usable, just untyped. Covariance of `BackendT_co` makes `APIClient[RequestsBackend]` assignable to `APIClient[Backend[Response]]`, which resolves the overloads; `client.backend` keeps the concrete backend type. `prepare()` merges default headers (gaps only) and is the override point for signing/auth.
- `errors.py` — `ClientError` → `TransportError` (with `.request`; `TimeoutError` additionally subclasses the built-in `TimeoutError`) and `APIError` (with `.request`/`.response`). Backends translate library exceptions via `translate_error`, chaining the original as `__cause__`.
- `hooks.py` — `Hook` (no-op base: `on_request` may replace the request, `on_response(request, response, elapsed)` may replace the response, `on_error` observes) and `LoggingHook` (logs via `repr()`, which redacts secrets).
- `backends/` — one module per optional HTTP library: `requests.py` (`RequestsBackend`; multi-value response headers via urllib3 raw headers, producer bodies as chunk iterators), `httpx.py` (`HttpxBackend`/`AsyncHttpxBackend`; header lines and `multi_items()` keep multi-value fidelity; producer bodies stream sync/async), `twisted.py` (`TwistedBackend` over `Agent`/`RedirectAgent`, `readBody`, `_RequestBodyProducer` via `cooperate`, timeout via `addTimeout`; the global reactor is imported lazily in `__init__`, never at module import). All accept their library's native client/session/agent (then never closed by the backend) or create their own (then closed by `close()`/`aclose()`).
- `testing.py` — `StubBackend`/`AsyncStubBackend`/`DeferredStubBackend` (canned `Response`s or raising responder callables; last response repeats; requests recorded; hooks run for real) and `deferred_result()` for unwrapping synchronously-fired Deferreds in tests.

Conventions:

- The version is single-sourced as `__version__` in `src/action0/client/__init__.py`; hatch extracts it with the regex in `[tool.hatch.version]`. Bump it only there.
- Releases: pushing a `vX.Y.Z` tag triggers `.github/workflows/release.yml`, which re-runs all checks, verifies the tag matches `__version__`, builds, and publishes to PyPI via trusted publishing (environment `pypi`). Never bump the version, tag, or publish on your own — releasing is the user's call.
- Tests mirror the `src/` layout under `tests/action0/client/` and are `unittest.TestCase` classes, executed via pytest. `tests/action0/client/test_typing.py` pins the static return-type promises with `typing.assert_type` — its `check_*` functions are analyzed by all three checkers but never executed. Its `FutureBackend` (a `concurrent.futures.Future` wrapper) is the openness regression test: `Client.send` must type as `Future[Response]`, `APIClient.send` as `Any`.
- Zope interfaces (twisted's `IAgent`, `IResponse`, `IConsumer`, the global reactor) and static checkers don't mix: such values are typed `Any` with a comment, and `# type: ignore[...]` / `# ty: ignore[...]` are combined on one line where mypy and ty disagree.
- Ruff enforces one import per line (isort `force-single-line`), line length 99, `action0` as first-party.
- Docs live in `docs/` (Sphinx + Furo, MyST Markdown pages, autodoc for the API reference). Docstrings are Sphinx-reST (`:param:`, `:py:meth:` roles). CI builds them with `-W` on every run and deploys to GitHub Pages on pushes to `main`. Guide examples in `docs/usage.md` show exact outputs in `#` comments — keep them truthful. `examples/petstore.py` is the complete worked example (type-checked in CI, runnable without network).

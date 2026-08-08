"""
Backend-agnostic, fully typed HTTP API clients.

The library separates *what* is sent (requests, operations) from *how* it
is sent (the backend): the same client code runs synchronously, on asyncio
or on Twisted — only the backend changes, and the static types follow it.

- Ring 0 — raw HTTP: :py:class:`Client` sends
  :py:class:`action0.req.Request` instances through a backend
  (:py:data:`SyncBackend` / :py:data:`AsyncBackend` /
  :py:data:`DeferredBackend`).
- Ring 1 — typed APIs: :py:class:`Operation` /
  :py:class:`JsonOperation` describe endpoints as dataclasses,
  :py:class:`APIClient` sends them and returns parsed, typed results.

The built-in backends live in :py:mod:`action0.client.backends` (each
behind an optional dependency), the test doubles in
:py:mod:`action0.client.testing`.
"""

from .api import APIClient
from .backend import AsyncBackend
from .backend import Backend
from .backend import BackendT_co
from .backend import BaseAsyncBackend
from .backend import BaseDeferredBackend
from .backend import BaseSyncBackend
from .backend import DeferredBackend
from .backend import FuturesBackend
from .backend import SendResultT_co
from .backend import SyncBackend
from .caching import AsyncCacheStore
from .caching import CachePolicy
from .caching import CacheStore
from .caching import CachingAsyncBackend
from .caching import CachingDeferredBackend
from .caching import CachingSyncBackend
from .caching import MemoryCache
from .client import Client
from .errors import APIError
from .errors import ClientError
from .errors import TimeoutError
from .errors import TransportError
from .fields import FieldSpec
from .fields import Location
from .fields import body
from .fields import form_field
from .fields import header
from .fields import json_body
from .fields import json_field
from .fields import path_param
from .fields import query
from .hooks import Hook
from .hooks import LoggingHook
from .operation import JsonOperation
from .operation import Operation
from .retry import RetryingAsyncBackend
from .retry import RetryingDeferredBackend
from .retry import RetryingSyncBackend
from .retry import RetryPolicy

__version__: str = "0.1.0"

__all__ = [
    "APIClient",
    "APIError",
    "AsyncBackend",
    "AsyncCacheStore",
    "Backend",
    "BackendT_co",
    "BaseAsyncBackend",
    "BaseDeferredBackend",
    "BaseSyncBackend",
    "CachePolicy",
    "CacheStore",
    "CachingAsyncBackend",
    "CachingDeferredBackend",
    "CachingSyncBackend",
    "Client",
    "ClientError",
    "DeferredBackend",
    "FieldSpec",
    "FuturesBackend",
    "Hook",
    "JsonOperation",
    "Location",
    "LoggingHook",
    "MemoryCache",
    "Operation",
    "RetryPolicy",
    "RetryingAsyncBackend",
    "RetryingDeferredBackend",
    "RetryingSyncBackend",
    "SendResultT_co",
    "SyncBackend",
    "TimeoutError",
    "TransportError",
    "body",
    "form_field",
    "header",
    "json_body",
    "json_field",
    "path_param",
    "query",
]

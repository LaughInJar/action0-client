# Installation

Install `action0-client` from PyPI. The HTTP libraries are optional —
pick the extras matching the backend(s) you want:

```shell
uv add "action0-client[httpx]"

# extras: requests, httpx, aiohttp, urllib3, twisted, all
pip install "action0-client[requests,twisted]"
```

Without extras you get the core library — clients, operations, test stubs
— plus two stdlib-only backends that need no extra at all:
{py:class}`~action0.client.backends.urllib.UrllibBackend` (basic sync
HTTP via `urllib.request`) and
{py:class}`~action0.client.backends.futures.ThreadPoolBackend` (parallel
sends over a thread pool). For real workloads pick one of the library
backends.

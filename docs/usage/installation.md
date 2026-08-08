# Installation

`action0-client` is not published to PyPI yet; install it straight from
GitHub. The HTTP libraries are optional — pick the extras matching the
backend(s) you want:

```shell
uv add "action0-client[httpx] @ git+https://github.com/LaughInJar/action0-client"

# extras: requests, httpx, aiohttp, urllib3, twisted, all
pip install "action0-client[requests,twisted] @ git+https://github.com/LaughInJar/action0-client"
```

Without extras you get the core library — clients, operations, test stubs
— plus two stdlib-only backends that need no extra at all:
{py:class}`~action0.client.backends.urllib.UrllibBackend` (basic sync
HTTP via `urllib.request`) and
{py:class}`~action0.client.backends.futures.ThreadPoolBackend` (parallel
sends over a thread pool). For real workloads pick one of the library
backends.

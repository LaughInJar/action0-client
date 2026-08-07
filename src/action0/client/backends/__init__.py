"""
The built-in backend implementations, one module per HTTP library so that
only the library you actually use needs to be installed (install the
matching extra, e.g. ``pip install "action0-client[httpx]"``):

- :py:mod:`action0.client.backends.requests` —
  :py:class:`~action0.client.backends.requests.RequestsBackend` (sync)
- :py:mod:`action0.client.backends.httpx` —
  :py:class:`~action0.client.backends.httpx.HttpxBackend` (sync) and
  :py:class:`~action0.client.backends.httpx.AsyncHttpxBackend` (asyncio)
- :py:mod:`action0.client.backends.twisted` —
  :py:class:`~action0.client.backends.twisted.TwistedBackend` (Deferred)

Nothing is re-exported here on purpose: importing this package must not
pull in any of the optional libraries.
"""

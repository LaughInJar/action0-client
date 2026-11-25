# TODO

Planned work that is not scheduled for a release yet. Entries move to the
[changelog](CHANGELOG.md) when they ship.

## Streamed response bodies on the Twisted backend

`TwistedBackend` reads every response body in full before the Deferred
returned by `send` fires — into memory, or into a spooled body. Unlike
the other backends it cannot hand out the response at headers arrival
and stream the body afterwards: a request asking for `BodyMode.STREAM`
fails with a `ValueError`.

What it takes:

- a body protocol (`IProtocol` passed to `response.deliverBody`) that
  feeds an `AsyncIterableBody`, so the body can be consumed with
  `async for` from coroutines run via `ensureDeferred`;
- backpressure: pause the transport while the consumer is behind, resume
  when it catches up;
- a timeout that covers the idle time between chunks, since the overall
  `addTimeout` on `send` ends when the headers have arrived;
- cleanup when the consumer abandons the body (abort the connection).

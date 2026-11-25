"""
The link between :py:class:`~action0.client.api.APIClient` and the
backend wrappers: the operation's ``parse`` travels with the request, so
that a wrapper can find out whether a response is usable — and retry, or
not cache it — instead of only ever seeing status codes.

Parsing must still happen exactly once per response. :py:class:`ParseOnce`
remembers the outcome for the response it has just seen, so the wrapper
asking early and the client's final ``map`` step share one run.
"""

from typing import Any
from typing import Callable

from action0.req import Request
from action0.req import Response

PARSE_KEY = "action0-client.parse"
"""The ``request.meta`` key under which
:py:meth:`APIClient.send <action0.client.api.APIClient.send>` puts the
:py:class:`ParseOnce` of the operation being sent."""


class ParseOnce:
    """
    An operation's ``parse``, run at most once per response: asking
    again for the same response object returns (or raises) what the first
    run produced.

    Only the latest response is remembered — a retry brings a new one,
    and the old outcome is of no further use.
    """

    def __init__(self, parse: Callable[[Response], Any]) -> None:
        """
        :param parse: the function turning a response into the result,
                      raising if it cannot
        """
        self._parse = parse
        self._response: "Response | None" = None
        self._result: Any = None
        self._error: "Exception | None" = None

    def failure(self, response: Response) -> "Exception | None":
        """
        Parse the response (unless that already happened) and report how
        it went, without raising.

        :param response: the response to parse
        :return: the exception parsing raised, ``None`` if it succeeded
        """
        if response is not self._response:
            self._response = response
            self._result = None
            self._error = None
            try:
                self._result = self._parse(response)
            except Exception as error:
                self._error = error
        return self._error

    def __call__(self, response: Response) -> Any:
        """
        The parsed result of the response.

        :param response: the response to parse
        :return: what the operation's ``parse`` returned
        :raises Exception: what the operation's ``parse`` raised
        """
        error = self.failure(response)
        if error is not None:
            raise error
        return self._result


def parse_failure(request: Request, response: Response) -> "Exception | None":
    """
    Whether the response is unusable for the operation that sent the
    request — for wrappers, which only have the request and the response
    in hand.

    :param request: the request that was sent
    :param response: the response that arrived
    :return: the exception the operation's ``parse`` raised for this
             response; ``None`` if it parsed, and also if the request
             was not sent by an :py:class:`~action0.client.api.APIClient`
             (then there is nothing to parse with)
    """
    parse = request.meta.get(PARSE_KEY)
    if not isinstance(parse, ParseOnce):
        return None
    return parse.failure(response)

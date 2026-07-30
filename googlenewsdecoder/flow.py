"""The decode algorithm, written once, driven either synchronously or asynchronously.

`decode_flow` is a generator: it yields `protocol.Request` objects and receives response
bodies back. It performs no I/O of its own, so the same flow runs against `requests`,
`httpx`, `urllib`, or a recorded fixture in a test -- and the sync and async entry points
stop being two copies of one algorithm.

    flow = decode_flow(url)
    result = drive(flow, Urllib3Transport())           # sync
    result = await drive_async(flow, HttpxAsyncTransport())   # async

The two drivers are the same thirteen lines with one `await` between them. Everything that
differs between sync and async lives there; nothing about the decode does.

This is the sans-I/O shape (sans-io.readthedocs.io), the same one `h11` uses.
"""


from collections.abc import Mapping

from . import protocol
from .errors import TransportError
from .limits import MAX_TOKEN_LENGTH


def failed(message: str, http_status: int | None = None) -> dict:
    """The failure shape. `http_status` is ABSENT, not None, when the failure had no HTTP status.

    Absent so that `result.get("http_status") == 429` needs no guarding and a parse failure can
    never be read as an HTTP one. Note the two senses of "status" in the returned dict: `status`
    is the success boolean this library has always returned, `http_status` is the response code.
    """
    out = {"status": False, "message": message}
    if http_status is not None:
        out["http_status"] = http_status
    return out


def _fetch_params(token: str, locale=protocol.DEFAULT_LOCALE):
    """Yield article-page GETs until one parses; return (params, last_error, last_status).

    `yield from` passes `send()` and `throw()` straight through, so both flows drive this the
    same way and neither carries its own copy. They did, and the copies had already drifted:
    only one of them fell back to a default message, and only one carried the note below.
    """
    last_error = None
    last_status = None
    for url in protocol.params_urls(token, locale):
        try:
            body = yield protocol.params_request(url)
        except TransportError as e:
            last_error = f"Request error in get_decoding_params: {e}"
            last_status = e.status
            continue
        params = protocol.parse_params(body)
        if params:
            return params, None, None
        # A page that parses to nothing is the common failure. The old code returned here
        # instead of trying the next candidate, so the documented RSS fallback only ever ran
        # on a transport exception -- never on the path that needed it.
        # A page that parsed to nothing is not a refusal, and reporting an earlier attempt's 429
        # alongside this message would describe two different requests as one. But a refusal is
        # the more actionable of the two, and on a partly-throttled address the orderings do
        # happen: first candidate 429s, second serves an interstitial that parses to nothing. So
        # a refusal already recorded wins, and the pair stays consistent because neither moves.
        if last_status is None:
            last_error = "Failed to fetch data attributes from Google News with the articles URL."
    return None, last_error or "Failed to fetch data attributes from Google News.", last_status


def decode_flow(
    source_url: str,
    max_token_length: int | None = MAX_TOKEN_LENGTH,
    locale: Mapping[str, str] | None = protocol.DEFAULT_LOCALE,
):
    """Yield the requests needed to decode `source_url`; return the result dict.

    Send each yielded `Request` and pass the response body back in. Throw a
    `TransportError` in to report a failed request -- the flow decides whether that
    is fatal or worth trying the next candidate URL.

    `locale` rides on the article-page GET so Google does not spend a redirect adding it; see
    `protocol.DEFAULT_LOCALE`.
    """
    token = protocol.article_id(source_url, max_length=max_token_length)
    if token is None:
        return {"status": False, "message": "Invalid Google News URL format."}

    # NOT short-circuited on `protocol.embedded_url`, deliberately. Some tokens do carry the
    # publisher URL inline, and answering from the token would skip both requests -- but the
    # token is caller-supplied, so doing that makes `decode()` hand back the caller's own
    # input as an authoritative answer. Anything beginning "http" would qualify, including a
    # URL with CRLF in it or one pointing somewhere the caller did not expect, and it would
    # come back with status True having asked Google nothing.
    #
    # The trade would be worth arguing about if it bought anything. It does not: sampling
    # current feeds finds essentially only opaque handles, so the fast path would almost never
    # fire. `protocol.embedded_url` remains available for callers who want to make that
    # decision themselves; the library does not make it for them.
    params, last_error, last_status = yield from _fetch_params(token, locale)
    if not params:
        return failed(last_error, last_status)

    try:
        body = yield protocol.decode_request(token, *params)
    except TransportError as e:
        return failed(f"Request error in decode_url: {e}", e.status)

    decoded = protocol.parse_decoded(body)
    if decoded is None:
        return failed("Parsing error in decode_url: no decoded url in response")
    return {"status": True, "decoded_url": decoded}


def drive(flow, transport, **kwargs) -> dict:
    """Run a flow to completion with a synchronous transport.

    StopIteration is caught around the GENERATOR calls only, never around the transport.
    Wrapping the whole loop conflates "the flow finished" with "the transport raised
    StopIteration" -- which a perfectly ordinary transport does the moment a `next()` inside
    it runs dry. That returned None in place of the documented dict, reported no error, and
    let a transport dictate the return value outright.
    """
    try:
        request = next(flow)
    except StopIteration as stop:
        return stop.value
    while True:
        try:
            body = transport(request, **kwargs)
        except TransportError as e:
            advance, arg = flow.throw, e
        else:
            advance, arg = flow.send, body
        try:
            request = advance(arg)
        except StopIteration as stop:
            return stop.value


async def drive_async(flow, transport, **kwargs) -> dict:
    """Run a flow to completion with an asynchronous transport. See `drive` on StopIteration."""
    try:
        request = next(flow)
    except StopIteration as stop:
        return stop.value
    while True:
        try:
            body = await transport(request, **kwargs)
        except TransportError as e:
            advance, arg = flow.throw, e
        else:
            advance, arg = flow.send, body
        try:
            request = advance(arg)
        except StopIteration as stop:
            return stop.value


def decode_batch_flow(
    source_urls,
    max_token_length: int | None = MAX_TOKEN_LENGTH,
    chunk_size: int = 50,
    locale: Mapping[str, str] | None = protocol.DEFAULT_LOCALE,
):
    """Decode many URLs, one signature fetch each plus one POST per chunk.

    Yields requests the same way `decode_flow` does; returns a list of result dicts
    ALIGNED TO THE INPUT ORDER. Callers never see the tags used to reorder the response.

    `chunk_size` bounds how many decodes ride in one POST. Probing found no ceiling: 400 RPCs
    in a single 270 KB body returned all 400 results. The default is well below that, because
    a larger chunk widens the blast radius of one failed POST, not because the endpoint
    objects. Either way the size need not be guessed correctly -- every position in a chunk is
    checked on the way out, and anything missing is reported as a failure against that URL
    specifically. A too-large chunk degrades into per-URL failures you can see, never wrong
    URLs you cannot.
    """
    results = [None] * len(source_urls)
    pending = []  # (position_in_results, token, signature, timestamp)

    for position, source_url in enumerate(source_urls):
        token = protocol.article_id(source_url, max_length=max_token_length)
        if token is None:
            results[position] = {"status": False, "message": "Invalid Google News URL format."}
            continue
        # See decode_flow on why this does not short-circuit on an inline URL.
        params, last_error, last_status = yield from _fetch_params(token, locale)
        if not params:
            results[position] = failed(last_error, last_status)
            continue
        pending.append((position, token, params[0], params[1]))

    for start in range(0, len(pending), chunk_size):
        chunk = pending[start : start + chunk_size]
        try:
            body = yield protocol.batch_decode_request([(t, s, ts) for _, t, s, ts in chunk])
        except TransportError as e:
            # Every position in the chunk gets the status, because one refused POST is the whole
            # chunk refused: a caller standing down on a 429 needs to see it on any of them.
            for position, *_ in chunk:
                results[position] = failed(f"Request error in decode_url: {e}", e.status)
            continue
        found = protocol.parse_batch_decoded(body)
        for tag, (position, *_) in enumerate(chunk, 1):
            url = found.get(tag)
            results[position] = (
                {"status": True, "decoded_url": url}
                if url
                else {"status": False, "message": "No result returned for this URL in the batch"}
            )

    return results

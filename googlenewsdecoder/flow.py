"""The decode algorithm, written once, driven either synchronously or asynchronously.

`decode_flow` is a generator: it yields `protocol.Request` objects and receives response
bodies back. It performs no I/O of its own, so the same flow runs against `requests`,
`httpx`, `urllib`, or a recorded fixture in a test -- and the sync and async entry points
stop being two copies of one algorithm.

    flow = decode_flow(url)
    result = drive(flow, RequestsTransport())          # sync
    result = await drive_async(flow, HttpxAsyncTransport())   # async

The two drivers are the same thirteen lines with one `await` between them. Everything that
differs between sync and async lives there; nothing about the decode does.

This is the sans-I/O shape (sans-io.readthedocs.io), the same one `h11` uses.
"""


from . import protocol
from .limits import MAX_TOKEN_LENGTH
from .transports import TransportError


def _fetch_params(token: str):
    """Yield article-page GETs until one parses; return (params, last_error).

    `yield from` passes `send()` and `throw()` straight through, so both flows drive this the
    same way and neither carries its own copy. They did, and the copies had already drifted:
    only one of them fell back to a default message, and only one carried the note below.
    """
    last_error = None
    for url in protocol.params_urls(token):
        try:
            body = yield protocol.params_request(url)
        except TransportError as e:
            last_error = f"Request error in get_decoding_params: {e}"
            continue
        params = protocol.parse_params(body)
        if params:
            return params, None
        # A page that parses to nothing is the common failure. The old code returned here
        # instead of trying the next candidate, so the documented RSS fallback only ever ran
        # on a transport exception -- never on the path that needed it.
        last_error = "Failed to fetch data attributes from Google News with the articles URL."
    return None, last_error or "Failed to fetch data attributes from Google News."


def decode_flow(source_url: str, max_token_length: int | None = MAX_TOKEN_LENGTH):
    """Yield the requests needed to decode `source_url`; return the result dict.

    Send each yielded `Request` and pass the response body back in. Throw a
    `TransportError` in to report a failed request -- the flow decides whether that
    is fatal or worth trying the next candidate URL.
    """
    token = protocol.article_id(source_url, max_length=max_token_length)
    if token is None:
        return {"status": False, "message": "Invalid Google News URL format."}

    # Some tokens carry the publisher URL outright, and reading one costs nothing.
    #
    # Do not expect this to fire often. The form is real -- it is the example in this
    # project's own README -- but sampling current feeds finds essentially only opaque
    # handles, so treat this as a free early exit rather than a way to cut request volume.
    # Anyone sizing a request budget should assume every URL costs the full round trip.
    embedded = protocol.embedded_url(token)
    if embedded is not None:
        return {"status": True, "decoded_url": embedded}

    params, last_error = yield from _fetch_params(token)
    if not params:
        return {"status": False, "message": last_error}

    try:
        body = yield protocol.decode_request(token, *params)
    except TransportError as e:
        return {"status": False, "message": f"Request error in decode_url: {e}"}

    decoded = protocol.parse_decoded(body)
    if decoded is None:
        return {"status": False, "message": "Parsing error in decode_url: no decoded url in response"}
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


def decode_batch_flow(source_urls, max_token_length: int | None = MAX_TOKEN_LENGTH, chunk_size: int = 50):
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
        # See decode_flow, including the note that this almost never fires on real tokens.
        embedded = protocol.embedded_url(token)
        if embedded is not None:
            results[position] = {"status": True, "decoded_url": embedded}
            continue
        params, last_error = yield from _fetch_params(token)
        if not params:
            results[position] = {"status": False, "message": last_error}
            continue
        pending.append((position, token, params[0], params[1]))

    for start in range(0, len(pending), chunk_size):
        chunk = pending[start : start + chunk_size]
        try:
            body = yield protocol.batch_decode_request([(t, s, ts) for _, t, s, ts in chunk])
        except TransportError as e:
            for position, *_ in chunk:
                results[position] = {"status": False, "message": f"Request error in decode_url: {e}"}
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

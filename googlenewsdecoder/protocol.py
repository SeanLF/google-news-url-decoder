"""The decode protocol, with no I/O in it.

Decoding a Google News URL is four pure steps and two HTTP requests:

    article_id(url)                 pure    the token is the last path segment
    -> params_urls(id)              I/O     GET the article page
    -> parse_params(html)           pure    scrape the signature and timestamp
    -> decode_request(id, sig, ts)  I/O     POST them to batchexecute
    -> parse_decoded(body)          pure    pull the publisher URL out

Everything here is a pure function over strings, and nothing in this module opens
a socket, so the transport is the caller's choice: `requests`, `httpx`, `urllib`,
or a recorded fixture in a test. `GoogleDecoder` and `GoogleDecoderAsync` are thin
wrappers over these, and share them rather than each carrying a copy.

Module scope imports only the standard library, so `import protocol` works on an
interpreter with nothing installed. `parse_params` reaches for `selectolax` when
it runs and falls back to a regex if it is absent -- the one third-party touch
here, deliberately lazy so it cannot make the module unimportable.

A `Request` says what to send without saying how to send it.
"""

import base64
import json
import re
from typing import NamedTuple
from urllib.parse import quote, urlparse

BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# The protobuf tag the frame opens with, and what an unwrapped token starts with when it is
# a handle rather than a URL.
_FRAME_TAG = bytes([0x08, 0x13, 0x22])
_OPAQUE_HANDLE_PREFIX = "AU_yqL"

# Opaque scaffold for the garturlreq RPC. The only variable fields are the
# article id, timestamp and signature. Note the THREE levels of array nesting in
# decode_request(): [[[rpc]]]. Two levels is silently rejected with HTTP 400.
_RPC_PARAMS = (
    '[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
    '"X","X",1,[1,1,1],1,1,null,0,0,null,0]'
)

_ARTICLE_PATHS = ("articles", "read")

# Both attributes must come off the SAME element. Two independent document-wide
# searches can pair a signature from one element with a timestamp from another,
# and a mismatched pair is worse than no pair: it builds a well-formed request
# that Google rejects, which surfaces as a confusing parse error much later.
_PARAMS_RE = re.compile(
    r"<[^>]*?data-n-a-sg=\"(?P<sg>[^\"]+)\"[^>]*?data-n-a-ts=\"(?P<ts>[^\"]+)\"[^>]*?>"
    r"|<[^>]*?data-n-a-ts=\"(?P<ts2>[^\"]+)\"[^>]*?data-n-a-sg=\"(?P<sg2>[^\"]+)\"[^>]*?>"
)


class Request(NamedTuple):
    """What to send. The caller decides how."""

    method: str
    url: str
    headers: dict
    body: bytes | None = None


def article_id(source_url: str, max_length: int | None = None) -> str | None:
    """The opaque article token from a Google News URL, or None if it is not one.

    `max_length` bounds the token before any caller base64-decodes it; the
    segment comes straight from a caller-supplied URL, so its size is not ours
    to choose.
    """
    try:
        parsed = urlparse(source_url)
    except Exception:
        return None
    path = parsed.path.split("/")
    if parsed.hostname != "news.google.com" or len(path) < 2:
        return None
    if path[-2] not in _ARTICLE_PATHS:
        return None
    token = path[-1]
    if not token or (max_length is not None and len(token) > max_length):
        return None
    return token


def _read_varint(data: bytes, position: int) -> tuple[int, int] | None:
    """(value, next_position) for the protobuf varint at `position`, or None if malformed.

    Seven bits per byte, little-endian, with the top bit marking "another byte follows".
    """
    value = shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift > 63:  # a length this large is malformed, not merely big
            return None
    return None


def unwrap_token(token: str) -> str | None:
    """The string packed inside an article token, or None if it does not unpack.

    The token is base64 around a protobuf frame: a tag, a varint length, then that many
    bytes. What comes out is either the publisher URL outright, or an opaque handle
    beginning `AU_yqL` that only batchexecute can resolve -- `embedded_url` tells them apart.

    Pure and offline. Four separate copies of this used to live in the decoder modules, and
    all four read the length as a single raw byte. That is only correct below 128: above it
    the varint is two bytes, so every longer payload came back a character short, and above
    255 the length was wrong outright. The result still began with "http", so it was returned
    as a successful decode rather than an error -- a truncated URL reported as the answer.
    """
    try:
        decoded = base64.urlsafe_b64decode(token + "==")
    except Exception:
        return None

    decoded = decoded.removeprefix(_FRAME_TAG)
    read = _read_varint(decoded, 0)
    if read is None:
        return None
    length, start = read
    payload = decoded[start : start + length]
    if len(payload) != length:
        # The frame claims more than it carries. Returning the short read would be the same
        # silent truncation this function exists to have stopped doing.
        return None
    try:
        # A protobuf string field is UTF-8 by definition. Decoding it as latin-1 -- which the
        # numbered decoders did, because they sliced a latin-1 string rather than bytes -- turns
        # every non-ASCII URL into mojibake that still starts with "http": spiegel.de/münchen
        # comes back as spiegel.de/mÃ¼nchen. Failing here is the right outcome for a payload
        # that is not a valid string; the caller falls back to asking Google.
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def is_opaque_handle(unwrapped: str) -> bool:
    """Whether an unwrapped token still needs the batchexecute round trip.

    Distinct from `embedded_url() is None`, which is also true for a token that unwraps to
    something that is neither a handle nor a URL. The flows do not currently use the
    distinction -- they attempt the RPC whenever no URL was found, including for that third
    case -- so this exists for callers doing their own triage, not as an optimisation the
    library already makes.
    """
    return unwrapped.startswith(_OPAQUE_HANDLE_PREFIX)


def _is_plausible_url(candidate: str) -> bool:
    """An http(s) URL with a host and nothing a header or a log line could be split on.

    `startswith("http")` is not this check. It admits the bare string "http", it admits
    "httpNOT-A-URL", and it admits embedded CRLF and NUL -- which matter because the result
    is handed to whatever the caller does next, often an HTTP client or a log.
    """
    # `str.isprintable()` already excludes every control character, so an explicit CRLF/NUL
    # check would be a subset of it written out longhand.
    if not candidate.isprintable():
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def embedded_url(token: str) -> str | None:
    """The publisher URL carried inside the token itself, or None if the RPC is required.

    **The decode flows deliberately do not call this**, and you should think before you do.
    The token is caller-supplied, so whatever comes out is the caller's own input, not
    something Google vouched for. Skipping the RPC on the strength of it means trusting a
    string that arrived with the URL you were asked to decode. It also buys very little:
    sampling current feeds finds essentially only opaque handles, so it rarely fires at all.

    What is returned is validated as far as a URL can be without fetching it -- an http(s)
    scheme, a host, and no control characters -- but "well-formed" is not "trustworthy".
    """
    unwrapped = unwrap_token(token)
    if unwrapped is None or is_opaque_handle(unwrapped):
        return None
    return unwrapped if _is_plausible_url(unwrapped) else None


def params_urls(token: str) -> tuple[str, ...]:
    """Article-page URLs to try, in order, to obtain the signature and timestamp.

    Google serves the attributes from either path; which one works varies, so
    the caller should try the next on a failure OR on a page that parses to
    nothing.
    """
    return (
        f"https://news.google.com/articles/{token}",
        f"https://news.google.com/rss/articles/{token}",
    )


def params_request(url: str) -> Request:
    """The article-page GET. Carries no headers of its own: what to send is the
    protocol's business, how to present yourself is the transport's."""
    return Request("GET", url, {})


def parse_params(html: str) -> tuple[str, str] | None:
    """(signature, timestamp) scraped from an article page, or None.

    Uses a real HTML parser when one is installed. The regex fallback below agreed with it
    on 12 live article pages, but agreement on today's markup is not soundness: a comment
    such as `<!-- data-n-a-sg="FAKE" -->` ahead of the real element defeats it, and
    fabricating a plausible pair is worse than finding none.
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        pass
    else:
        # Select on carrying BOTH attributes rather than on a fixed `c-wiz > div[jscontroller]`
        # path: real parsing still ignores comments and script bodies, and this additionally
        # survives Google rearranging the wrapper, which the fixed path would not.
        element = HTMLParser(html or "").css_first("[data-n-a-sg][data-n-a-ts]")
        if element is None:
            return None
        sg = element.attributes.get("data-n-a-sg")
        ts = element.attributes.get("data-n-a-ts")
        return (sg, ts) if sg and ts else None
    match = _PARAMS_RE.search(html or "")
    if not match:
        return None
    sg = match.group("sg") or match.group("sg2")
    ts = match.group("ts") or match.group("ts2")
    return (sg, ts) if sg and ts else None


def decode_request(token: str, signature: str, timestamp: str) -> Request:
    """The batchexecute POST that turns (token, signature, timestamp) into a URL."""
    rpc = [
        "Fbv4je",
        f'["garturlreq",{_RPC_PARAMS},"{token}",{timestamp},"{signature}"]',
    ]
    body = f"f.req={quote(json.dumps([[rpc]]))}".encode()
    return Request(
        "POST",
        BATCHEXECUTE_URL,
        # Content-Type is protocol-mandated: the endpoint only accepts a form body.
        {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body,
    )


def batch_decode_request(items) -> Request:
    """One POST carrying several decodes.

    `items` is a sequence of (token, signature, timestamp). Each RPC is tagged with its
    position, because the response comes back in an arbitrary order -- see
    `parse_batch_decoded`.
    """
    rpcs = [
        ["Fbv4je", f'["garturlreq",{_RPC_PARAMS},"{token}",{timestamp},"{signature}"]', None, str(i)]
        for i, (token, signature, timestamp) in enumerate(items, 1)
    ]
    body = f"f.req={quote(json.dumps([rpcs]))}".encode()
    return Request(
        "POST",
        BATCHEXECUTE_URL,
        {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body,
    )


def parse_batch_decoded(body: str) -> dict:
    """{position: url} from a batched response, keyed by the tag we sent.

    Google returns the frames in an arbitrary order -- measured 2, 4, 1, 3, 5 for a
    five-item batch -- so results MUST be keyed by the tag carried in each frame and
    never by the order they arrive in. Zipping request order against response order
    silently pairs each token with another article's URL, which is worse than failing:
    it produces a plausible wrong answer.
    """
    found = {}
    conflicted = set()
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            frames = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        for frame in frames:
            if not (isinstance(frame, list) and len(frame) > 6 and frame[0] == "wrb.fr"):
                continue
            tag = frame[6]
            # Canonical digits only. int() accepts " 1 ", "+1", "1_0", booleans and
            # non-ASCII digits, so a non-canonical tag would be silently read as a
            # position we never sent.
            if not isinstance(tag, str) or not tag.isascii() or not tag.isdigit():
                continue
            try:
                url = json.loads(frame[2])[1]
            except (json.JSONDecodeError, ValueError, TypeError, IndexError):
                continue
            if not (isinstance(url, str) and url.startswith("http")):
                continue
            position = int(tag)
            if position in found and found[position] != url:
                # Two different answers for one request. Which is right is unknowable, so
                # report neither -- a visible failure beats a plausible wrong URL.
                conflicted.add(position)
            found[position] = url
    for position in conflicted:
        found.pop(position, None)
    return found


def parse_decoded(body: str) -> str | None:
    """The publisher URL from a batchexecute response, or None if absent."""
    try:
        payload = json.loads(body.split("\n\n")[1])[:-2]
        url = json.loads(payload[0][2])[1]
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        return None
    return url if isinstance(url, str) and url.startswith("http") else None

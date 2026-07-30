# Google News Decoder

Google News hides every article behind a redirect. A link in its RSS feed looks like
`news.google.com/rss/articles/CBMiqwFBVV95cUxNMTRq...` and there is no way to tell from the
URL whether it points at Reuters, the BBC, or a local paper. This library turns those links
back into the publisher URLs they stand for.

That matters if you are aggregating news, deduplicating stories across sources, or showing
readers where a link actually goes before they click it. Google publishes no API for this, so
the work is a signature scrape followed by a call to an internal RPC -- fiddly enough that it
is worth having in one place rather than in every project that needs it.

## What you get

- **Batch decoding.** Many articles share a single POST instead of one each. Results come back
  in the order you asked for them, which is not free: the endpoint answers a batch in an
  arbitrary order (measured returning tags `2, 4, 1, 3, 5` for a five-item batch), so anything
  pairing request order with response order hands each article another article's URL.
- **Bring your own HTTP client.** A transport is any callable taking
  `(Request, timeout=, proxy=)` and raising `TransportError` on failure. `urllib3` is the
  default and the only hard dependency, `httpx` powers the async API from the `[async]` extra,
  and your own session, retry policy, rate limiter or tracing wrapper drops straight in.
- **A protocol layer with no I/O in it.** `protocol` is pure functions over strings -- what to
  send and what a response means, importing nothing outside the standard library. Drive it
  yourself if you would rather own the networking entirely.
- **Sync and async share one implementation.** The algorithm lives in `flow` as a generator;
  the two entry points differ only in which driver runs it.

## Example

```python
from googlenewsdecoder import decode, decode_batch

decode("https://news.google.com/rss/articles/CBMiqwFBVV95cUxNMTRq...")
# {'status': True, 'decoded_url': 'https://www.reuters.com/world/europe/...'}

results = decode_batch(urls)          # one POST per chunk, not one per article
# [{'status': True, 'decoded_url': ...}, {'status': False, 'message': ...}]
# same order as `urls`, always
```

Every call returns a dict rather than raising: `status` tells you whether it worked, and a
failure carries a `message` explaining which step gave up.

## Getting started

```sh
pip install googlenewsdecoder            # sync decoding
pip install googlenewsdecoder[async]     # adds httpx for decode_async
pip install googlenewsdecoder[socks]     # adds PySocks for socks5:// proxies
```

Decode a single URL:

```python
from googlenewsdecoder import decode

result = decode(url, interval=1)         # interval paces a batch of calls
if result["status"]:
    print(result["decoded_url"])
else:
    print("could not decode:", result["message"])
```

Use a different HTTP client. A transport is any callable taking `(Request, timeout=, proxy=)`
that returns the body as text and raises `TransportError` on failure:

```python
from googlenewsdecoder import decode
from googlenewsdecoder.transports import Urllib3Transport

decode(url, transport=Urllib3Transport())         # the default, stated explicitly
decode(url, transport=my_session_backed_callable) # your pooling, retries, tracing
```

Four things worth knowing before you rely on it:

- **Requests time out after 15 seconds** (`limits.DEFAULT_TIMEOUT`), applied to connect and read
  on both transports. `decode()` takes no `timeout=`; to change it, pass a transport that
  supplies its own.
- **Responses are capped at 32 MiB decompressed** (`limits.MAX_RESPONSE_BYTES`), counted decoded
  because that is where a compressed bomb expands. Exceeding it raises `TransportError` rather
  than returning a truncated page, so it is one of the reasons a decode can fail.
- **The default transport is shared process-wide,** which is what keeps every decode on one
  pooled connection. Google's throttle is sensitive to connection count, so constructing a
  transport per call is measurably worse. If you build your own, hold onto it. Call `close()`
  when you are done with one you own, or use it as a context manager; do not close
  `default_transport()`, since everything else in the process is using it. The async transport
  has `aclose()` and `GoogleDecoderAsync` works as an async context manager.
- **`interval` is clamped** to `limits.MAX_INTERVAL` (3600s) rather than rejected.

Async, which needs the `[async]` extra:

```python
from googlenewsdecoder import decode_async

result = await decode_async(url)
```

## Rate limits

Google throttles this endpoint per IP address and publishes no limit, no `Retry-After`, and no
rate-limit headers. Two things are worth knowing before you build on it:

- The **article-page fetch** draws the throttling, and it behaves as a budget rather than a
  rate: nine of nine addresses were refused after 19-63 of them, and pacing did not raise the
  total.
- How much you get **depends on the address**: residential fares several times better than
  datacenter or VPN.
- **Reuse connections.** Measured one arm per address on previously unused addresses: clients
  opening a connection per request were refused 4 of 4 after 24-88 articles; pooled clients
  were refused 1 of 5, the rest running to the end of the token supply. The default transport
  pools and `decode()` shares one, so this is already done for you.

So this package ships no rate limiter: adapting the rate cannot buy more of a fixed budget.
On a 429 you usually want to stand down for the rest of the batch. A transport is a plain
callable, so pacing, retries and standing down are all wrappers -- the `transports` module
docstring carries `with_retries` and `stop_on_429` as copyable examples, not as exports.

## Proxies

```python
decode(url, proxy="http://user:pass@host:port")
decode(url, proxy="socks5://user:pass@host:port")   # needs the [socks] extra
```

SOCKS goes through `urllib3.contrib.socks` and PySocks, the same path `requests` uses.
Environment `HTTP_PROXY`/`NO_PROXY` are not consulted: pass `proxy=` explicitly.

## Migrating from 0.1.x

`gnewsdecoder` still works and still returns what it always did, so most code needs no change.

The five numbered decoders are gone. They were five standalone implementations of one decode,
four carrying their own copy of the request envelope -- which is why a change at Google's end
meant a new decoder version rather than an edit. They are not aliased, because their contracts
disagreed with each other and a silent alias would hand you a shape you did not ask for.
Reaching for one now raises an error naming its replacement.

| removed | use | difference to expect |
|---|---|---|
| `decoderv1(url)` | `protocol.embedded_url(protocol.article_id(url))` | still pure and offline, but returns `None` where `decoderv1` handed back the original URL unchanged |
| `decoderv2(url)` | `decode(url)` | returns a dict, not a bare string; does not raise |
| `decoderv3(url)` | `decode(url)` | the failure key is `message`, not `error` |
| `decoderv4(urls)` | `decode_batch(urls)` | results stay aligned to input order |
| `new_decoderv1(url)` | `decode(url)` | same call, same return shape, current name |

`get_decoding_params()` and `decode_url()` are no longer public methods on the decoder classes;
the same steps live in `protocol` as pure functions.

The minimum Python is now 3.10, since 3.9 left security support in October 2025.

## Layers

Enter wherever suits you:

| module | what it owns |
|---|---|
| `protocol` | pure functions: what to send, what a response means. No I/O. |
| `flow` | the algorithm as a generator, plus a sync and an async driver |
| `transports` | how bytes actually move, swappable, urllib3 by default |
| `errors` | `TransportError`, the vocabulary both sides of the seam share |

`protocol`, `flow` and `errors` do not import `transports`, so you can drive the algorithm with
your own I/O without touching this package's HTTP code at all. That is checked in CI rather
than promised -- see `.importlinter`.

## Contributing

Tests run without a network -- every HTTP entry point is substituted, so nothing depends on
Google being reachable or on the decode contract of the day. That is enforced rather than
promised: `tests/conftest.py` takes the sockets away, so a test that reaches out fails naming
itself.

```sh
pip install -r requirements.txt   # the package, plus pytest and ruff
python -m pytest tests/ -q
ruff check .
```

MIT licensed.

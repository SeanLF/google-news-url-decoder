# Google News Decoder

Google News hides every article behind a redirect. A link in its RSS feed looks like
`news.google.com/rss/articles/CBMiqwFBVV95cUxNMTRq...` and there is no way to tell from the
URL whether it points at Reuters, the BBC, or a local paper. This library turns those links
back into the publisher URLs they stand for.

That matters if you are aggregating news, deduplicating stories across sources, or showing
readers where a link actually goes before they click it. Google publishes no API for this, so
the work is a signature scrape followed by a call to an internal RPC — fiddly enough that it
is worth having in one place rather than in every project that needs it.

## What you get

- **Batch decoding.** Many articles share a single POST instead of one each. Results come back
  in the order you asked for them, which is not free: the endpoint answers a batch in an
  arbitrary order (measured returning tags `2, 4, 1, 3, 5` for a five-item batch), so anything
  pairing request order with response order hands each article another article's URL.
- **Bring your own HTTP client.** A transport is any callable. `requests` is the default;
  `urllib` ships as a zero-dependency option, `httpx` powers the async API, and your own
  session, retry policy, rate limiter or tracing wrapper drops straight in.
- **A protocol layer with no I/O in it.** `protocol` is pure functions over strings — what to
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

Use a different HTTP client — anything callable that takes a request and returns the body:

```python
from googlenewsdecoder import decode
from googlenewsdecoder.transports import UrllibTransport

decode(url, transport=UrllibTransport())          # no third-party HTTP dependency
decode(url, transport=my_session_backed_callable) # your pooling, retries, tracing
```

Async, which needs the `[async]` extra:

```python
from googlenewsdecoder import decode_async

result = await decode_async(url)
```

## Rate limits

Google throttles this endpoint per IP address and publishes no limit, no `Retry-After`, and no
rate-limit headers. Two things are worth knowing before you build on it:

- The **article-page fetch** is what draws the throttling. Batching collapses the POSTs, not
  the fetches, so it reduces round trips without reducing your exposure.
- How much you get **depends on the address**. The same code and pacing behaves very
  differently from a residential connection than from a datacenter or VPN address.

`transports.AdaptiveRateLimit` wraps any transport and adjusts its own pacing in response to
429s, rather than asking you to guess a number that would be wrong on a different host.

## Proxies

```python
decode(url, proxy="http://user:pass@host:port")
decode(url, proxy="socks5://user:pass@host:port")   # needs the [socks] extra
```

SOCKS goes through `requests` and PySocks. `UrllibTransport` refuses a SOCKS proxy outright
rather than quietly sending traffic direct, because urllib has no SOCKS support.

## Layers

Enter wherever suits you:

| module | what it owns |
|---|---|
| `protocol` | pure functions: what to send, what a response means. No I/O. |
| `flow` | the algorithm as a generator, plus a sync and an async driver |
| `transports` | how bytes actually move — swappable, `requests` by default |

## Contributing

Tests run without a network — every HTTP entry point is substituted, so nothing depends on
Google being reachable or on the decode contract of the day.

```sh
python -m pytest tests/ -q
```

MIT licensed.

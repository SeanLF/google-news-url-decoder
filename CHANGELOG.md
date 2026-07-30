# Changelog

## 0.2.0

Breaking. The five numbered decoders are removed and the minimum Python is 3.10. See
[Migrating from 0.1.x](README.md#migrating-from-01x) for the mapping; `gnewsdecoder` is
unchanged and most code needs no edit.

### Fixed

**The default transport walled itself on the consent interstitial.** `requests.request()`
builds a Session per call, whose cookie jar replayed the `SOCS` cookie Google sets on the
article's 302 back into `consent.google.com`, which is what makes that endpoint render its
page instead of bouncing you to the article. A bare `urllib` fetch keeps no jar and got the
article from the same addresses, which is how the jar was identified as the cause. The sync
transport is now urllib3-backed, which implements no cookies at all and strips credentials
cross-origin. httpx has no equivalent that survives a redirect chain, and a no-op cookie jar
alone was measured still leaking on the third hop, so the async transport resolves the chain
itself and applies both rules explicitly. Only walled addresses were affected, so it was
invisible from a residential connection. Ruled out first: TLS fingerprint, `Accept`,
`Accept-Encoding`, `Connection`, and the `CONSENT` cookie (see `probes/README.md`).

**A decoded URL could come back silently truncated.** The payload length inside an article
token is a protobuf varint, but it was read as a single raw byte. That is only correct below
128, so any embedded URL of 128 bytes or more lost its last character, and past 255 the length
was wrong outright. Because the shortened value still began with `http` it was returned as a
**successful** decode -- so a caller received a broken link with no error to catch. News URLs
with a slug and tracking parameters cross 128 bytes routinely. Affected `decoderv1`,
`decoderv3` and `decoderv4` on 0.1.x.

**Non-ASCII URLs came back as mojibake.** The payload is a protobuf string, which is UTF-8 by
definition, but it was decoded as latin-1 -- so `spiegel.de/münchen` became
`spiegel.de/mÃ¼nchen`. Like the truncation above, the result still began with `http` and was
reported as a success.

**Tokens from any host were decoded.** The guard read
`hostname == "news.google.com" and path[-2] == "articles" or "read"`, which Python groups as
`(... and ...) or "read"` -- true for every URL. `decoderv2("https://evil.com/x/CBMiTOKEN")`
returned decoded bytes instead of refusing. Closes #18.

**A batch could hand an article another article's URL.** `decoderv4` paired request order with
response order. The endpoint answers a batch in arbitrary order (measured returning tags
`2, 4, 1, 3, 5` for five items), so results are now keyed by the tag that was sent. A missing
result is reported against its own URL rather than shifting the ones after it.

**`import googlenewsdecoder` failed without httpx.** `__init__` imported the async decoder
while `setup.py` never required it. Using async without the extra now raises an error naming
the extra, instead of a bare `ModuleNotFoundError` from three frames down.

**A proxy on the async path silently did nothing.** A per-request proxy was forwarded as
`extensions={"proxy": ...}`, which httpx accepts and ignores -- httpcore reads the proxy the
client was built with. Traffic went direct while the code read as though it were proxied.
Passing a late proxy now raises and says how to construct the transport instead.

**A timeout on the async path was dropped** when the caller supplied their own client.

**Decompression is bounded and its errors are typed.** A ~1 MB gzipped response was measured
expanding to 1 GB. A body whose declared encoding does not match its content used to raise
`BadGzipFile`/`zlib.error`/`EOFError`, none of which a caller catching `TransportError` sees.

**Oversized tokens are rejected before being decoded.** The token is the last path segment, so
its size is caller-controlled.

**The RSS fallback never ran.** `params_urls` offers two article-page URLs, but a page that
parsed to nothing returned immediately instead of trying the second -- so the documented
fallback only fired on a transport exception, never on the case that needed it.

### Changed

- **Three layers you can enter at any level.** `protocol` is pure functions over strings with
  no I/O and no third-party imports; `flow` is the algorithm as a generator; `transports` is
  how bytes move. The sync and async decoders were 88% the same code and are now one
  implementation with two drivers.
- **A transport is any callable**, so your own client, session, retry policy, rate limiter or
  tracing wrapper drops in. The default is `Urllib3Transport`; `RequestsTransport` is an alias
  of it, since urllib3 is what `requests` used underneath anyway.
- **`TransportError` moved to `googlenewsdecoder.errors`**, and is still importable from
  `googlenewsdecoder` and from `googlenewsdecoder.transports` as before. It is the vocabulary
  shared by the algorithm and whatever performs the I/O, so it belongs to neither: while it
  lived in `transports`, writing your own driver meant importing the very module you were
  replacing. `protocol` and `flow` now import no transport at all.
- **`decode_batch`** shares one POST across many articles and returns results in input order.
- **Dependencies are two**: `urllib3` and `selectolax`. `httpx` and `PySocks` moved to the
  `[async]` and `[socks]` extras, having previously been listed as required.
- **Environment `HTTP_PROXY`/`NO_PROXY` are no longer consulted.** Chosen, not inherited: it
  makes "an explicit proxy always wins" true by construction rather than by the workaround the
  old urllib path needed. Pass `proxy=` explicitly.
- **`python_requires` is 3.10**, up from 3.9 (end of security support: October 2025).
- `get_decoding_params()` and `decode_url()` are no longer public methods; the same steps are
  pure functions in `protocol`.

### Security

**`decode()` never answers from the token itself.** Some tokens carry the publisher URL inline,
and returning it would skip both HTTP requests. The token is caller-supplied, though, so doing
that hands back a string that arrived with the input rather than one Google vouched for --
`status: True`, nothing verified. Any value beginning `http` qualified, including URLs
containing CRLF and hosts the caller did not expect, and Google's frame tag was not required.
It also saved nothing measurable, since current feeds carry essentially only opaque handles.

`protocol.embedded_url` remains public for callers who want to make that trade knowingly, and
now validates what it returns: an http(s) scheme, a real host, and no control characters.

### Added

- CI: pytest across 3.10–3.14, a job that installs without extras, ruff, and `import-linter`
  enforcing the module layering (`protocol` cannot import an HTTP client; the sync and async
  decoders cannot import each other).
- A CI job that runs the suite at the **declared dependency floor**, pinned, on the
  `python_requires` floor. `pip install .[all]` resolves to the newest urllib3, so `>=2.7.0` was
  a claim nobody ran, and the decompression bound rests on what 2.7 provides.
- Tests run with sockets removed, so "no network" is enforced rather than promised.
- `probes/` -- the scripts behind the README's rate-limit claims, so they can be re-measured
  rather than trusted. Excluded from the wheel and sdist. `probes/runner/` runs one across VPN
  exits; see `docs/probe-harness.md`.
- `CONTRIBUTING.md` and `docs/probe-harness.md`.
- **`Urllib3Transport.close()`**, plus context-manager support, and `max_pools` bounding how many
  proxies it keeps pools for (least-recently-used first). Previously a `PoolManager` per proxy was
  kept forever with no way to hand the sockets back, which a rotating proxy list grows without
  limit. Note that `PoolManager.clear()` does not itself close anything on urllib3 2.7, so the
  sockets are closed explicitly. Do not close `default_transport()`: it is shared process-wide.
- **A locale on the article-page GET** (`protocol.DEFAULT_LOCALE`). Google answers a bare
  `/articles/<token>` with a 302 to the same path plus its own `hl`/`gl`/`ceid`, so stating it
  removes a round trip: measured taking a decode from three requests to two on clean exits, and a
  forced `en-US` is accepted from exits Google would have assigned otherwise. Overridable via
  `flow.decode_flow(locale=...)`; `None` restores the bare URL. A walled exit redirects anyway.
- **`timeout=` on `decode`, `decode_async`, `decode_batch` and both decoder classes**
  (not the deprecated `gnewsdecoder` shims), and **`http_status` on a failed result.** Both exist because
  a consumer had to bypass the top layer to get them: `decode()` hardcoded its timeout, so the only
  way to change it was to drive `decode_flow` by hand, and a refusal arrived as prose, so the only
  way to branch on a 429 was to wrap a transport and catch it before the flow flattened it. Neither
  was a reason to reach past `decode()`. `http_status` is absent rather than None when the failure
  had no HTTP status, so a parse failure is never mistaken for one.
- **`decode_batch(chunk_size=50)`** bounds how many decodes ride in one POST. No ceiling was found
  (400 RPCs in one 270 KB body returned 400 results); the default is low so one failed POST has a
  smaller blast radius.
- **`interval` is clamped** to `limits.MAX_INTERVAL` (3600s) rather than rejected, so a caller's
  own backoff tuning is never silently truncated but an absurd value cannot pin a thread for
  thirty-one years.

### Notes on rate limits

Google throttles this endpoint **per IP address**, with no published limit, no `Retry-After`
and no rate-limit headers. Three things measured, in case they save you the experiment:

- It behaves as a **budget, not a rate**. Requests run clean until the budget is gone; pacing
  them out does not raise the total. Replicated across nine addresses: all nine were refused
  after 19-63 article GETs, unpooled.
- **Only the article-page GET counts.** Batching collapses the POSTs, so it saves round trips
  without reducing exposure.
- **The unit of the budget is unresolved, and probably not resolvable with shared VPN addresses.**
  Refusal has landed anywhere from 29 article fetches to not at all in 1000, and the spread looks
  structured by address rather than random, so it swamps the 2x effect that would distinguish
  counting requests from counting fetches. Two designs failed to separate them. Treat this
  library's round-trip savings as bandwidth and latency wins, which are measured, and not as
  headroom. What IS settled: exhaustion belongs to the address. Pooled
  and unpooled clients issue the same requests per article fetch, and unpooled is refused far
  sooner, so connections cost something. Yet a pooled client on an address an unpooled one had
  just exhausted was refused 12 of 12, so pooling delays exhaustion rather than exempting you
  from it. Whether requests cost anything on their own is untested: it needs connections held at
  one while requests climb. Treat the counts above as what happens to an unpooled client, and
  "fewer requests buys more decodes" as unproven.
- **How much you get depends on the address.** A residential connection fared several times
  better than datacenter and VPN addresses in the same window.
- **IPv4 and IPv6 are different addresses**, so a dual-stack host has two budgets. Measured
  with one host's IPv6 refusing every request while its IPv4 answered in the same minute.
- **Connection reuse has no measurable effect on how much you get.** Twenty arms, one per address,
  two windows, addresses never used before: pooled at 6-31 connections refused at a median of 96
  article fetches, unpooled at 104-595 connections at a median of 86, exact permutation p = 0.22.
  A 10x difference in connections and no resolvable difference in outcome; the spread between
  addresses is 4-5x, which swamps it. Pool for the handshakes and the sockets, not for the budget.
  The measurement below, which showed a clean separation, ran two arms per address and so had them
  sharing a budget. Original: one arm per address, on eleven addresses
  never used before, each arm doing the same work: clients opening a fresh connection per
  request were refused 4 of 4, after 24-88 articles and 72-179 connections. Pooled clients
  were refused 1 of 5, the rest reaching the token supply's end at ~100 articles on 1-6
  connections. It is not a clean per-connection count -- the one pooled refusal came at 92
  requests over 3 connections -- so the mechanism is not fully characterised, but pooling is
  the lever. The default transport pools, and `decode()` shares one. `probes/connections.py`.

  Design note, because it bit twice: both arms on one address cannot answer this. They share
  that address's budget, so whichever runs second inherits the remains -- 8 of 11 pooled arms
  were refused at request 1 straight after a fresh arm spent it. Use `ARM=fresh` / `ARM=pooled`
  on separate exits.

## 0.1.7 and earlier

See the commit history.

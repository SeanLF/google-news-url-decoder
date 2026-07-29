# Probes

Scripts for measuring how Google actually behaves, since it documents none of it. **Not tests,
and not shipped** — every one makes real requests, and `MANIFEST.in` and `setup.py` keep this
directory out of the sdist and the wheel.

| probe | question |
|---|---|
| `degrade.py` | How many article fetches does this address get before it is refused? |
| `mixed.py` | Do the POSTs count toward that, or only the article GETs? |
| `recovery.py` | Does a spent budget come back, and do search feeds draw on the same one? |
| `consent_survey.py` | How often is the consent interstitial served instead of an article? |
| `tls_fingerprint.py` | Do the shipped transports hand Google different handshakes? (no Google requests) |
| `connections.py` | Is the throttle counted per request, or per TCP connection? |
| `walled.py` | Is this exit walled, and does the installed library survive it? |
| `cookie_policy.py` | Does blocking cookie storage clear the wall, or only hand-following redirects? |
| `hook_vs_loop.py` | Can a response hook replace the redirect loop? |
| `prior_art.py` | Do urllib3 and the published consent-cookie techniques work? |
| `budget.py` | What does one decode cost, and how many do you get? |
| `async_transport.py` | Does the async transport carry the consent bug? |
| `decode_with.py` | Does a given installed version actually decode today? |
| `fetch_tokens.py` | Get live tokens to feed the others. |

```sh
python probes/degrade.py                  # LIMIT and GAP come from the environment
python probes/fetch_tokens.py 6 tokens.txt
PYTHONPATH=. python probes/decode_with.py checkout tokens.txt
```

Each prints one JSON line. `_common.py` holds the shared HTTP helpers and `dedupe()` — feed
every token list through that, since a repeated token costs the same as a new one and tells you
nothing you did not already learn.

## What to expect

**Throttling varies enormously by address, so measure your own.** Across one wall-clock window
we saw a datacenter address take 400 requests without a single 429, a residential connection
stop around 80, and shared VPN exits stop between 22 and 43. Any constant baked into a library
would be one of those numbers, and wrong for everyone else, which is why the package encodes
none and leaves pacing to the caller's own transport wrapper.

**An open question: does connection reuse change the budget?** One pooled run reached 15,256
article GETs across nine exits with no 429, where unpooled clients are refused after 19-63.
That is a real observation with no established cause. The first attempt to test it was
confounded -- `retries=False` left the pooled arm not following redirects, so it did one hop
per token against the unpooled arm's three, and "survived longer" partly meant "did less
work". The corrected re-run could not separate them because every exit was already spent.
Re-run `connections.py` on rested addresses before believing either answer.

Token age is not a factor: tokens collected weeks earlier still decode.

**The interstitial is earned by replaying Google's own cookie.** From a walled address:

```
GET /articles/<token>        -> 302  consent.google.com   Set-Cookie: SOCS=...
GET consent.google.com/m?... -> 303  back to the article      (when SOCS is NOT sent back)
GET /articles/<token>        -> 200  the article, 1036 KB
```

Send `SOCS` back and you get the interstitial (644 KB) instead. A client with no cookie jar
sails through; one with a jar walls itself. Ruled out first: TLS fingerprint (see
`tls_fingerprint.py`), `Accept`, `Accept-Encoding`, `Connection`, and the `CONSENT` cookie.
Invisible from a residential address, where nothing is walled.

**IPv4 and IPv6 are separate budgets.** One host's IPv6 was hard-429'd while its IPv4
answered in the same minute.

**Following the redirects by hand is the only thing that works.** Measured on walled exits,
these all still get the interstitial because `resolve_redirects` re-derives cookies from the
raw response: a `DefaultCookiePolicy(allowed_domains=[])` jar (empty jar, cookie still sent),
a block-everything policy, and a `response` hook stripping `Set-Cookie`. See
`cookie_policy.py` and `hook_vs_loop.py`.

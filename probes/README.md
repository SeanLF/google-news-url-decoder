# Probes

Scripts for measuring how Google actually behaves, since it documents none of it. **Not tests,
and not shipped** -- every one makes real requests, and `MANIFEST.in` and `setup.py` keep this
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
| `locale_params.py` | Does sending the locale up front skip Google's redirect? |
| `pooled_vs_fresh.py` | Is a pooled client still served on an address that just refused an unpooled one? |
| `async_transport.py` | Does the async transport carry the consent bug? |
| `decode_with.py` | Does a given installed version actually decode today? |
| `fetch_tokens.py` | Get live tokens to feed the others. |

```sh
python probes/degrade.py                  # LIMIT and GAP come from the environment
python probes/fetch_tokens.py 6 tokens.txt
PYTHONPATH=. python probes/decode_with.py checkout tokens.txt
```

Each prints one JSON line. `_common.py` holds the shared HTTP helpers and `dedupe()` -- feed
every token list through that, since a repeated token costs the same as a new one and tells you
nothing you did not already learn.

## Running one across VPN exits

Throttling is per address, so the interesting runs hold everything constant except the exit.
`runner/` does that, with [gluetun](https://github.com/qdm12/gluetun) holding the tunnel:

```sh
export GNEWS_LAB_DIR=~/.gnews-lab            # credentials and results, never in this repo
docker build -f probes/runner/Dockerfile -t gnews-probe .
probes/runner/probe latvia walled SERVER_HOSTNAMES=node-lv-01.protonvpn.net
probes/runner/parallel budget LIMIT=60
```

`probes/entrypoint.sh` is the in-container half: it confirms which address we are leaving from
and refuses to run otherwise. Setup, credentials for either protocol, and the one trap worth
knowing -- `parallel` holds wall-clock constant, not the per-address budget -- are in
[docs/probe-harness.md](../docs/probe-harness.md).

## What has been ruled out

Three explanations for the throttle were formed and each killed by the next measurement. Recorded
so nobody derives them again:

| hypothesis | killed by |
|---|---|
| a rate limit | 70 requests at 10/s ran clean; 33 at 0.24/s were throttled |
| a fixed quota | the slower rate hit the wall at a third of the volume |
| novelty of distinct articles | 5 repeated articles throttled at 30, 4,331 distinct ones at 33 |

Those measurements came from a laptop VPN session, one exit at a time, and ranged from 30 requests
to 96+ under nominally identical conditions -- a wider spread than any variable being manipulated,
which is what motivated rotating exits automatically rather than trusting any single run.

## What to expect

**Throttling varies enormously by address, so measure your own.** Across one wall-clock window
we saw a datacenter address take 400 requests without a single 429, a residential connection
stop around 80, and shared VPN exits stop between 22 and 43. Any constant baked into a library
would be one of those numbers, and wrong for everyone else, which is why the package encodes
none and leaves pacing to the caller's own transport wrapper.

**Connection reuse has no measurable effect on the budget.** This section said "dominates the
budget" for a long time. It does not survive a controlled run.

Twenty arms, one per address, across two wall-clock windows, on addresses this harness had never
touched, with a 600-token supply so the feed was never the constraint:

| arm | connections | refusals (article fetches) | median |
|---|---|---|---|
| pooled | 6-31 | 48, 69, 81, 88, 105, 120, 166, 189 | 96 |
| unpooled | 104-595 | 29, 44, 85, 88, 125, 152 | 86 |

A 10-fold difference in connections, and an exact permutation test on the means gives p = 0.22.
The direction favours pooling and the effect may be real, but it is smaller than 14 arms can
resolve and much smaller than the 4-5x spread between addresses. Resolving it would need about 82
arms per side.

The earlier measurement that showed a clean separation (unpooled 24-88, pooled reaching ~100) ran
two arms per address, which share its budget: the confound described below. Pool anyway, for the
handshakes and sockets, and because 595 connections for 125 fetches is rude. Do not pool expecting
more decodes per address.

Two arms on ONE address cannot measure this, and it took two confounded runs to see why. They
share that address's budget, so the second arm inherits the remains -- 8 of 11 pooled arms
refused at request 1 right after a fresh arm spent it. An earlier attempt failed differently:
`retries=False` left the pooled arm not following redirects, so it fetched no article at all
and "survived longer" meant "did a third of the work". Use `ARM=` on separate, rested exits.

**But pooling does not rescue a spent address.** One process, one tunnel, one address: an unpooled
client was refused after 43 article fetches, and a pooled client on that same address immediately
after was refused 12 times out of 12. So exhaustion belongs to the address and persists, and being
pooled buys a longer run rather than an exemption. `pooled_vs_fresh.py`.

**And pooled clients are refused too, sooner than the earlier runs suggested.** Three exits, one
pooled arm each, pushed with a supply of 600 tokens so the feed could not be the constraint: all
three were refused with a real 429, at 49, 128 and 139 article fetches on 9, 12 and 6 connections,
which is 177 to 510 round trips. An earlier pooled run reached about 330 fetches on one connection
and stopped transferring instead, which was read as a possible ceiling and was not one.

Read those alongside the controlled run above rather than on their own. These three addresses had
all been used earlier the same day, which is the confound that has bitten every version of this
measurement, and the 20-arm run that controlled for it found no resolvable connection effect at
all. What survives here is only the ordering of the raw counts, and the ordering is what the
permutation test says cannot be distinguished from chance.

## What this harness cannot measure

Worth knowing before designing a run, because two days of arms went into finding out.

**Per-address variance is larger than any effect we have tried to detect.** Refusal has landed
anywhere from 29 article fetches to not at all in 1000. Within a single stratum it is 4x; overall
better than 20x. It also looks structured rather than random: small-country exits refused at 48-189
fetches while Germany, the UK and South Africa ran 900-1000 fetches and 2000-3400 round trips
without a 429. So an arm's result says as much about which address it drew as about what it did.

**Which means a 2x effect needs roughly 16 arms per stratum, and the stratum is not ours to
choose.** Whether an address is walled -- and therefore whether a fetch costs 2 hops or 4 -- is
Google's decision, discovered after the tunnel is up. Balanced strata mean 40-50 arms.

**Roughly a third to a half of arms die of infrastructure.** Five consecutive failures ends a run,
and the errors are `MaxRetryError`, `TimeoutError`, `URLError` -- the tunnel or the exit, not the
endpoint. A run of 14 `request_cost` arms produced zero clean refusals: they stalled, or they
exhausted the token supply first.

**So the unit of the budget is unresolved.** Whether it counts requests or article fetches decides
whether this library's round-trip savings buy headroom or only latency. Tested two ways and neither
settled it: arms differing in round trips per fetch (blurred by the wall appearing mid-run), and a
post-hoc check of which quantity refusal clusters in across refused arms, where the coefficient of
variation came out 0.45 for fetches against 0.49 for round trips. The second weakly favours
requests. Both are swamped by the variance above.

The savings are justified on bandwidth and latency, which are measured and stable. Do not claim
they buy budget.

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

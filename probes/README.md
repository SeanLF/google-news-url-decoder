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
would be one of those numbers, and wrong for everyone else — which is why `AdaptiveRateLimit`
responds to what it observes instead.

Two things held across every address we tested:

- Where a limit appeared at all, it behaved as a **budget rather than a rate**: requests ran
  clean until it was gone, and spreading them out did not raise the total.
- **Only the article-page GET was counted.** Every refusal landed on a GET, including on a run
  that had just made 18 successful POSTs. Batching collapses POSTs, so it saves round trips
  without reducing exposure.

Token age is not a factor: tokens collected weeks earlier still decode.

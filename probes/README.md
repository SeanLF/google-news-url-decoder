# Probes

The README makes claims about how Google throttles this endpoint. These are the scripts those
claims came from, so the claims can be checked rather than taken on trust.

**They are not tests and they are not shipped.** Every one of them makes real requests to
Google, and running them spends the throttling budget of whatever address you run them from.
`MANIFEST.in` prunes this directory from the sdist and `setup.py` excludes it from the wheel;
`pytest tests/` never touches it.

## What each one answers

| probe | question |
|---|---|
| `degrade.py` | How many article fetches does an address get before it is refused? |
| `mixed.py` | Do the POSTs count toward the budget, or only the article GETs? |
| `recovery.py` | Does the budget come back, and do search feeds draw on the same one? |
| `consent_survey.py` | How often is the "Before you continue" interstitial served instead of an article? |
| `decode_with.py` | Does a given installed version actually decode today? |
| `fetch_tokens.py` | Get live tokens to feed the others. |

## Running one

```sh
python probes/degrade.py            # reads LIMIT and GAP from the environment
LIMIT=40 GAP=0.5 python probes/degrade.py
```

`decode_with.py` imports whichever `googlenewsdecoder` is on the path and reports which one it
found, so it can compare a release against a checkout:

```sh
python probes/fetch_tokens.py 6 tokens.txt
PYTHONPATH=. python probes/decode_with.py checkout tokens.txt
python probes/decode_with.py released tokens.txt      # whatever pip installed
```

Give both arms the same token file. Not because tokens go stale, but because the second run
would otherwise spend a different slice of a budget the first already drew down.

Each prints one JSON line. `_common.py` holds the shared HTTP helpers and `dedupe()` -- feed
every token list through that, because a repeated token costs the same budget as a new one and
tells you nothing you did not already learn.

## What was measured

Nine addresses in one wall-clock window, plus a residential control:

- It is a **per-IP budget, not a rate limit.** Every address ran clean until its budget was
  gone. 10 req/s and 0.5 req/s reached the same counts.
- **Only the article GETs are counted.** Every refusal landed on a GET, including a run that
  had just made 18 successful POSTs. Batching collapses POSTs, so it saves round trips without
  reducing exposure.
- **How much you get depends on the address**: ~80 residential, 22–43 across VPN exits.

Falsified along the way, and worth not re-deriving: a byte-based budget (gzip changed nothing
despite a real 6.5x bandwidth saving), a novelty rule (5 repeated articles throttled at 30
while thousands of distinct ones throttled at 33), and the consent wall as a soft precursor to
the 429 (nine clean runs, zero consent responses before refusal).

Token age is **not** a factor. Tokens sampled from across a corpus collected over several weeks
all still decoded. An earlier claim to the contrary was drawn while a malformed request envelope
was making everything fail.

Tokens carrying the publisher URL inline (`protocol.embedded_url`) are **rare** in current
feeds — sampling finds essentially only opaque handles — so assume every URL costs the full
round trip when sizing a budget.

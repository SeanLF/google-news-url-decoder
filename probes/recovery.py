"""Two questions the product decision actually hinges on.

1. Does the budget refill during a run? Our pipeline takes ~20 minutes. If a spent budget
   recovers meaningfully within that, 48 GETs can be paced across the run instead of being
   an all-or-nothing spend. If recovery is hourly or daily, pacing buys nothing and the only
   lever is resolving fewer articles.

2. Do Google News *search feeds* draw on the same budget as article pages? We fetch Reuters
   and Nikkei through news.google.com RSS searches on every run, so if those count, our real
   spend is higher than the 48 article GETs we have been counting.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import urllib.error

from _common import classify, emit, fresh_tokens, get

POLL_S = int(os.environ.get("POLL_S", "30"))
MAX_WAIT_S = int(os.environ.get("MAX_WAIT_S", "600"))


def one_article(token):
    body, _ = get(f"https://news.google.com/articles/{token}")
    return classify(body)


def spend_until_refused(tokens):
    n = 0
    for tok in tokens:
        try:
            one_article(tok)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return n
            continue
        except Exception:
            continue
        n += 1
        time.sleep(0.4)
    return None


tokens = fresh_tokens(limit=200)
out = {}

# Does a search feed still work once article GETs are refused? If the budget were shared,
# the feed should be refused too.
spent = spend_until_refused(tokens)
out["gets_before_429"] = spent
if spent is None:
    emit(note="never refused within the token supply", **out)
    raise SystemExit

try:
    feed = fresh_tokens(query="reuters", limit=5)
    out["search_feed_after_429"] = f"ok ({len(feed)} tokens)"
except urllib.error.HTTPError as e:
    out["search_feed_after_429"] = f"http {e.code}"
except Exception as e:
    out["search_feed_after_429"] = type(e).__name__

# Now watch for the budget to come back. Each poll uses a token we have NOT spent yet: a
# refused request is free, but the one that finally succeeds is not, and re-spending a token
# already counted above would corrupt the second-budget measurement below.
poll_tokens = iter(tokens[spent:] if spent else tokens)
waited = 0
recovered_at = None
while waited < MAX_WAIT_S:
    time.sleep(POLL_S)
    waited += POLL_S
    probe_token = next(poll_tokens, None)
    if probe_token is None:
        out["note"] = "ran out of unspent tokens while polling for recovery"
        break
    try:
        one_article(probe_token)
        recovered_at = waited
        break
    except urllib.error.HTTPError as e:
        if e.code != 429:
            recovered_at = waited
            break
    except Exception:
        continue

out["recovered_after_s"] = recovered_at
out["gave_up_after_s"] = None if recovered_at else waited

# If it came back, how much came back? Continue through the same iterator so this never
# re-requests a token the first budget or the polling loop already paid for.
if recovered_at:
    out["gets_on_second_budget"] = spend_until_refused(list(poll_tokens))

emit(**out)

"""Does the consent wall precede the 429? i.e. is it the soft form of the same block?

Fetch article pages continuously on one exit and record what Google serves at each step.
If the hypothesis holds we should see article... article, then a switch to consent, then
429 -- rather than consent appearing at random.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import urllib.error

from _common import classify, emit, fresh_tokens, get

LIMIT = int(os.environ.get("LIMIT", "80"))
GAP = float(os.environ.get("GAP", "1.0"))

try:
    tokens = fresh_tokens(limit=LIMIT)
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}")
    raise SystemExit

seq, first_consent, first_429 = [], None, None
for i, tok in enumerate(tokens, 1):
    try:
        body, _ = get(f"https://news.google.com/articles/{tok}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            first_429 = i
            seq.append("429")
            break
        seq.append(f"http{e.code}")
        continue
    except Exception:
        seq.append("err")
        continue
    kind = classify(body)
    seq.append(kind)
    if kind == "consent" and first_consent is None:
        first_consent = i
    time.sleep(GAP)

emit(requests=len(seq),
     first_consent_at=first_consent,
     first_429_at=first_429,
     articles=seq.count("article"),
     consents=seq.count("consent"),
     transitions="".join({"article": "a", "consent": "c", "429": "X"}.get(s, "?") for s in seq))

"""Does the consent wall precede the 429? i.e. is it the soft form of the same block?

Fetch article pages continuously on one exit and record what Google serves at each step.
If the hypothesis holds we should see article... article, then a switch to consent, then
429 -- rather than consent appearing at random.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import classify, emit, fresh_tokens, spend_until_refused

LIMIT = int(os.environ.get("LIMIT", "80"))
GAP = float(os.environ.get("GAP", "1.0"))

try:
    tokens = fresh_tokens(limit=LIMIT)
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}")
    raise SystemExit

seq, first_consent = [], None

def record(i, tok, body):
    global first_consent
    kind = classify(body)
    seq.append(kind)
    if kind == "consent" and first_consent is None:
        first_consent = i + 1

clean, refused = spend_until_refused(tokens[:LIMIT], gap=GAP, on_each=record)
if refused:
    seq.append("429")

emit(requests=len(seq),
     first_consent_at=first_consent,
     first_429_at=len(seq) if refused else None,
     articles=seq.count("article"),
     consents=seq.count("consent"),
     transitions="".join({"article": "a", "consent": "c", "429": "X"}.get(s, "?") for s in seq))

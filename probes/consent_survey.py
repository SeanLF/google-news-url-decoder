"""How common is the consent interstitial, and does declaring consent clear it?

One article fetch with no cookie, one with a consent choice. Both on the same connection,
so the exit IP cannot change between them -- which is what defeated the earlier attempts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import classify, emit, fresh_tokens, get

CONSENT = "CONSENT=YES+cb.20210328-17-p0.en+FX+000"

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}")
    raise SystemExit

url = f"https://news.google.com/articles/{tokens[0]}"
out = {"tokens": len(tokens)}
for label, cookie in (("no_cookie", None), ("with_consent", CONSENT)):
    try:
        body, size = get(url, cookie=cookie)
        out[label] = classify(body)
        out[label + "_kb"] = round(size / 1024)
    except Exception as e:
        out[label] = f"error {type(e).__name__}"
emit(**out)

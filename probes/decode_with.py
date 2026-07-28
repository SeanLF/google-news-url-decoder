"""Decode a fixed token list with whichever googlenewsdecoder is importable, and say so.

    python probes/decode_with.py fork tokens.txt

Run once per virtualenv to compare two versions. Both runs must see the SAME tokens in the
SAME order -- not because tokens go stale, but because the second run would otherwise be
spending a different slice of a budget the first run already drew down.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import dedupe

import googlenewsdecoder as g

label = sys.argv[1] if len(sys.argv) > 1 else "installed"
token_file = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TOKEN_FILE", "tokens.txt")

# Deduped: a repeated token costs a GET against the per-IP budget and tells us nothing we did
# not already learn the first time.
with open(token_file) as f:
    tokens = dedupe(f)

ok = fail = 0
messages = []
for tok in tokens:
    url = f"https://news.google.com/rss/articles/{tok}?oc=5"
    try:
        r = g.gnewsdecoder(url)
    except Exception as e:
        fail += 1
        messages.append(f"{type(e).__name__}: {e}")
        continue
    if isinstance(r, dict) and r.get("status") and str(r.get("decoded_url", "")).startswith("http"):
        ok += 1
    else:
        fail += 1
        messages.append(str(r.get("message") if isinstance(r, dict) else r)[:120])

print(json.dumps({
    "label": label,
    "version": getattr(g, "__version__", "?"),
    "path": g.__file__,
    "n": len(tokens),
    "ok": ok,
    "fail": fail,
    "sample_messages": messages[:3],
}), flush=True)

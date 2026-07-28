"""Write N fresh Google-News tokens to a file, for the other probes to spend.

    python probes/fetch_tokens.py 20 tokens.txt

A search feed hands out roughly 200 tokens for the cost of one request. That is the reason to
use it -- these are tokens you have not already spent against the per-IP budget. It is NOT
because stored tokens expire: tokens collected weeks earlier still decode.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import fresh_tokens

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
out_path = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TOKEN_FILE", "tokens.txt")

tokens = fresh_tokens(query=os.environ.get("QUERY", "world"), limit=n)
with open(out_path, "w") as f:
    f.write("\n".join(tokens) + "\n")
print(f"wrote {len(tokens)} fresh tokens to {out_path}")

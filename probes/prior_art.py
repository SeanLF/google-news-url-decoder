"""Two published approaches to this wall, against what the library ships. One GET per variant.

1. urllib3 implements no cookies at all -- its pools are explicitly not stateful clients --
   while still following redirects and stripping Cookie cross-host, in a library requests
   already depends on.

2. The scraping ecosystem (ScrapingBee, Whoogle) bypasses this by SENDING an accepted consent
   value rather than withholding cookies. `CONSENT=YES+` is the dead pre-2022 name; `SOCS`
   replaced it.

The two SOCS arms are weaker evidence than the others: a "consent" result means only that
THESE values did not work. Google never published an accepted value, so a rejected cookie and
an unrecognised one are indistinguishable from here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import urllib3
from _common import HEADERS, article_url, classify, emit, library_decode, record_body, require_tokens

# Values circulated as "consent already given"; both forms appear in the wild. Neither is
# documented by Google, and neither is known-good -- see the docstring.
SOCS_ACCEPTED = "SOCS=CAESHAgBEhIaAB"
SOCS_PLUS_CONSENT = "SOCS=CAESHAgBEhIaAB; CONSENT=YES+cb.20210328-17-p0.en+FX+000"

tokens = require_tokens(5)
out = {}

try:
    r = requests.get(article_url(tokens[0]), headers=HEADERS, timeout=30)
    out["bare"] = classify(r.text)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    http = urllib3.PoolManager(retries=urllib3.Retry(redirect=10, other=0, total=None))
    resp = http.request("GET", article_url(tokens[1]), headers=HEADERS)
    record_body(out, "urllib3", resp.data.decode("utf-8", "replace"))
except Exception as e:
    out["urllib3"] = f"error {type(e).__name__}: {e}"[:60]

for label, cookie, idx in (
    ("socs_accepted", SOCS_ACCEPTED, 2),
    ("socs_plus_consent", SOCS_PLUS_CONSENT, 3),
):
    try:
        r = requests.get(
            article_url(tokens[idx]), headers={**HEADERS, "Cookie": cookie}, timeout=30
        )
        record_body(out, label, r.text)
    except Exception as e:
        out[label] = f"error {type(e).__name__}"

try:
    out["loop"], _ = library_decode(tokens[4])
except Exception as e:
    out["loop"] = f"error {type(e).__name__}"

emit(**out)

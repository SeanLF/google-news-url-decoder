"""Two published approaches to this problem, against the hand-rolled loop.

1. urllib3 does not implement cookies at all -- its pools are explicitly not stateful clients
   -- while still following redirects and stripping Cookie cross-host. That is what the loop
   hand-rolls, in a library requests already depends on.

2. The scraping ecosystem (ScrapingBee, Whoogle) bypasses this wall by SENDING an accepted
   consent value rather than withholding cookies. `CONSENT=YES+` is the dead pre-2022 name;
   `SOCS` replaced it, and an accepted `SOCS` was never tested here.

One article GET per variant.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}
# Values circulated as "consent already given"; both forms appear in the wild.
SOCS_ACCEPTED = "SOCS=CAESHAgBEhIaAB"
SOCS_PLUS_CONSENT = "SOCS=CAESHAgBEhIaAB; CONSENT=YES+cb.20210328-17-p0.en+FX+000"

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

out = {}

import requests

try:
    r = requests.get(f"https://news.google.com/articles/{tokens[0]}", headers=HEADERS, timeout=30)
    out["bare"] = classify(r.text)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    import urllib3

    http = urllib3.PoolManager(retries=urllib3.Retry(redirect=10, other=0, total=None))
    resp = http.request("GET", f"https://news.google.com/articles/{tokens[1]}", headers=HEADERS)
    body = resp.data.decode("utf-8", "replace")
    out["urllib3"] = classify(body)
    out["urllib3_kb"] = round(len(body) / 1024)
except Exception as e:
    out["urllib3"] = f"error {type(e).__name__}: {e}"[:60]

for label, cookie, idx in (
    ("socs_accepted", SOCS_ACCEPTED, 2),
    ("socs_plus_consent", SOCS_PLUS_CONSENT, 3),
):
    try:
        r = requests.get(
            f"https://news.google.com/articles/{tokens[idx]}",
            headers={**HEADERS, "Cookie": cookie},
            timeout=30,
        )
        out[label] = classify(r.text)
        out[label + "_kb"] = round(len(r.text) / 1024)
    except Exception as e:
        out[label] = f"error {type(e).__name__}"

try:
    from googlenewsdecoder import decode_flow, drive
    from googlenewsdecoder.transports import RequestsTransport

    res = drive(
        decode_flow(f"https://news.google.com/rss/articles/{tokens[4]}?oc=5"),
        RequestsTransport(),
        timeout=30,
    )
    out["loop"] = "decoded" if res.get("status") else "failed"
except Exception as e:
    out["loop"] = f"error {type(e).__name__}"

emit(**out)

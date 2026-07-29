"""Is this exit walled, and does the shipped transport get past it?

`consent_survey.py` cannot answer this and never could: it fetches through `_common.get`,
which is urllib, and urllib is the ONE client Google never walls. That blind spot is why this
lab studied the consent interstitial for a whole session without seeing it.

The wall is served per address AND per client. On a walled address the article's 302 sets
`SOCS`, and any client that returns that cookie to consent.google.com is handed the 644 KB
interstitial instead of a 303 back to the article. urllib keeps no cookie jar so it sails
through; `requests.request()` builds a Session per call whose jar replays it.

Reports all three, so a row says both whether the exit is walled and whether the installed
library survives it:

    urllib          -- walled? never. The control: if this is not "article", the exit is
                       refusing outright (429) and the row says nothing about the wall.
    requests_raw    -- a bare requests.get, i.e. the pre-fix transport behaviour.
    library         -- a real decode through whatever transport `lib` ships, which is the
                       only line that speaks for production.

Four article GETs per exit at most, so a sweep across every config is cheap.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens, get

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

out = {"tokens": len(tokens)}

# Control. urllib is never walled, so anything other than "article" here means the exit is
# refused outright and the rest of the row is not about the interstitial.
try:
    body, size = get(f"https://news.google.com/articles/{tokens[0]}")
    out["urllib"] = classify(body)
    out["urllib_kb"] = round(size / 1024)
except Exception as e:
    out["urllib"] = f"error {type(e).__name__}"

# The pre-fix behaviour: requests following redirects inside one Session, jar and all.
try:
    import requests

    r = requests.get(f"https://news.google.com/articles/{tokens[1]}", headers=HEADERS, timeout=30)
    out["requests_raw"] = classify(r.text) if r.status_code == 200 else f"http {r.status_code}"
    out["requests_raw_kb"] = round(len(r.text) / 1024)
except Exception as e:
    out["requests_raw"] = f"error {type(e).__name__}"

# What production actually does, through whatever transport this build of the library ships.
try:
    from googlenewsdecoder import decode_flow, drive
    from googlenewsdecoder.transports import RequestsTransport

    result = drive(
        decode_flow(f"https://news.google.com/rss/articles/{tokens[2]}?oc=5"),
        RequestsTransport(),
        timeout=30,
    )
    out["library"] = "decoded" if result.get("status") else "failed"
    out["library_detail"] = str(result.get("message") or result.get("decoded_url"))[:80]
except Exception as e:
    out["library"] = f"error {type(e).__name__}"

# Present only on a build that carries the consent fix; makes the row self-describing about
# which code produced it, alongside the `lib` branch+SHA the image already stamps.
try:
    from googlenewsdecoder import transports

    out["has_consent_fix"] = hasattr(transports, "_follow_without_cookies")
except Exception:
    out["has_consent_fix"] = False

emit(**out)

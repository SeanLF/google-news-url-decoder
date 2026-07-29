"""Is this exit walled, and does the shipped transport get past it?

Three arms, one article GET each. `consent_survey.py` cannot answer this because it fetches
through `_common.get`, and urllib was not walled on any exit measured here.

    urllib          -- control. Not "article" means the exit is refusing outright, and the
                       rest of the row is about that, not about the interstitial.
    requests_raw    -- a bare requests.get: a Session per call, jar and all.
    library         -- a real decode through whatever transport this build ships, which is
                       the only arm that speaks for production.

`_kb` on the urllib arm is bytes and on the requests arm is characters, so compare each
against its own arm across exits rather than against the other.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import HEADERS, article_url, emit, get, library_decode, record_body, require_tokens

tokens = require_tokens(3)
out = {"tokens": len(tokens)}

try:
    body, size = get(article_url(tokens[0]))
    record_body(out, "urllib", body, size)
except Exception as e:
    out["urllib"] = f"error {type(e).__name__}"

try:
    import requests

    r = requests.get(article_url(tokens[1]), headers=HEADERS, timeout=30)
    record_body(out, "requests_raw", r.text)
    if r.status_code != 200:
        out["requests_raw"] = f"http {r.status_code}"
except Exception as e:
    out["requests_raw"] = f"error {type(e).__name__}"

try:
    out["library"], out["library_detail"] = library_decode(tokens[2])
except Exception as e:
    out["library"] = f"error {type(e).__name__}"

# Which code produced this row, alongside the `lib` branch+SHA the image stamps. Sniffs a
# PUBLIC name: the private helper this used to look for was deleted when the transport moved
# to urllib3, so the field had gone quietly False on every build including fixed ones.
try:
    from googlenewsdecoder import transports

    out["has_consent_fix"] = hasattr(transports, "Urllib3Transport")
except Exception:
    out["has_consent_fix"] = False

emit(**out)

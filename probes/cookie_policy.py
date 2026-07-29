"""Does blocking cookie STORAGE clear the wall, or only hand-following the redirects?

A review proposes replacing the manual redirect loop with a jar policy plus requests' own
redirect handling: `DefaultCookiePolicy(allowed_domains=[])` prevents `SOCS` being stored, so
nothing can replay it, and requests keeps its tested 307/308 and cross-host behaviour.

That contradicts an earlier measurement here, where a policy-blocked client was STILL served
the interstitial. "Not stored" and "not sent" are different claims, and only the second one
matters. So compare all four on the same exit, one article GET each:

    bare            -- control; walled address should give consent
    policy          -- blocked storage + requests' own redirect following
    policy_no_redir -- blocked storage + manual following, to separate the two changes
    library         -- whatever transport lib/ ships

Reports the Cookie header actually sent on the final hop, which is the thing in dispute.
"""

import os
import sys
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from _common import HEADERS, article_url, emit, library_decode, record_body, require_tokens

REDIRECTS = (301, 302, 303, 307, 308)
MAX_HOPS = 10


def blocked_session():
    from http.cookiejar import DefaultCookiePolicy

    s = requests.Session()
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return s


tokens = require_tokens(4)
out = {}

try:
    r = requests.get(article_url(tokens[0]), headers=HEADERS, timeout=30)
    record_body(out, "bare", r.text)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    s = blocked_session()
    r = s.get(article_url(tokens[1]), headers=HEADERS, timeout=30)
    record_body(out, "policy", r.text)
    out["policy_sent_cookie"] = r.request.headers.get("Cookie")
    out["policy_jar_size"] = len(s.cookies)
except Exception as e:
    out["policy"] = f"error {type(e).__name__}"

try:
    s = blocked_session()
    url = article_url(tokens[2])
    exhausted = True
    for _ in range(MAX_HOPS):
        r = s.get(url, headers=HEADERS, timeout=30, allow_redirects=False)
        if r.status_code not in REDIRECTS or not r.headers.get("Location"):
            exhausted = False
            break
        url = urljoin(url, r.headers["Location"])
    record_body(out, "policy_no_redir", r.text)
    if exhausted:
        # Still redirecting when the hops ran out. Say so, rather than classifying a 3xx stub
        # body as "unknown" and letting it read as a page Google served.
        out["policy_no_redir"] = f"still redirecting after {MAX_HOPS} hops"
except Exception as e:
    out["policy_no_redir"] = f"error {type(e).__name__}"

try:
    out["library"], _ = library_decode(tokens[3])
except Exception as e:
    out["library"] = f"error {type(e).__name__}"

emit(**out)

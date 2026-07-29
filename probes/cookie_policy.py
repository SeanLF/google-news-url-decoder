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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}


def blocked_session():
    from http.cookiejar import DefaultCookiePolicy

    import requests

    s = requests.Session()
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return s


try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

import requests

out = {}

try:
    r = requests.get(f"https://news.google.com/articles/{tokens[0]}", headers=HEADERS, timeout=30)
    out["bare"] = classify(r.text)
    out["bare_kb"] = round(len(r.text) / 1024)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    s = blocked_session()
    r = s.get(f"https://news.google.com/articles/{tokens[1]}", headers=HEADERS, timeout=30)
    out["policy"] = classify(r.text)
    out["policy_kb"] = round(len(r.text) / 1024)
    out["policy_sent_cookie"] = r.request.headers.get("Cookie")
    out["policy_jar_size"] = len(s.cookies)
except Exception as e:
    out["policy"] = f"error {type(e).__name__}"

try:
    from urllib.parse import urljoin

    s = blocked_session()
    url = f"https://news.google.com/articles/{tokens[2]}"
    for _ in range(10):
        r = s.get(url, headers=HEADERS, timeout=30, allow_redirects=False)
        if r.status_code not in (301, 302, 303, 307, 308) or not r.headers.get("Location"):
            break
        url = urljoin(url, r.headers["Location"])
    out["policy_no_redir"] = classify(r.text)
    out["policy_no_redir_kb"] = round(len(r.text) / 1024)
except Exception as e:
    out["policy_no_redir"] = f"error {type(e).__name__}"

try:
    from googlenewsdecoder import decode_flow, drive
    from googlenewsdecoder.transports import RequestsTransport

    res = drive(
        decode_flow(f"https://news.google.com/rss/articles/{tokens[3]}?oc=5"),
        RequestsTransport(),
        timeout=30,
    )
    out["library"] = "decoded" if res.get("status") else "failed"
except Exception as e:
    out["library"] = f"error {type(e).__name__}"

emit(**out)

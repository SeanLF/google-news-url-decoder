"""Does interleaving POSTs extend the GET budget, or is only the GET counted?

Three shapes, chosen by SHAPE:
  get    GET only                       -- the baseline that dies at ~27
  mixed  GET then POST per article      -- what decode() actually does
  batch  N GETs then one batched POST   -- what decode_batch() does
If only GETs are counted, all three should refuse after a similar number of GETs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import urllib.error
import urllib.request

from _common import UA, classify, emit, fresh_tokens, get

from googlenewsdecoder import protocol

SHAPE = os.environ.get("SHAPE", "mixed")
LIMIT = int(os.environ.get("LIMIT", "80"))
GAP = float(os.environ.get("GAP", "0.5"))


def params(token):
    """Signature and timestamp, using the library's parser rather than a private regex."""
    body, _ = get(f"https://news.google.com/articles/{token}")
    if classify(body) != "article":
        return None
    return protocol.parse_params(body)


def post(items):
    """The batched RPC, built by `protocol` -- deliberately not hand-rolled here.

    This probe measures Google, not the decoder, so there is no reason to isolate it from the
    library. There is a strong reason not to: a hand-copied envelope silently stops matching
    what Google accepts, and then the probe keeps reporting refusal counts for a request that
    was never going to work. That has already happened once in this project's history, and it
    cost a whole conclusion about token expiry.
    """
    request = protocol.batch_decode_request([(t, sg, ts) for t, sg, ts in items])
    headers = {"User-Agent": UA, **request.headers}
    req = urllib.request.Request(request.url, data=request.body, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


tokens = fresh_tokens(limit=LIMIT)
gets = posts = 0
refused = None
pending = []

try:
    for tok in tokens:
        p = params(tok)
        gets += 1
        if p is None:
            continue
        if SHAPE == "mixed":
            post([(tok, p[0], p[1])])
            posts += 1
        elif SHAPE == "batch":
            pending.append((tok, p[0], p[1]))
            if len(pending) == 20:
                post(pending)
                posts += 1
                pending = []
        time.sleep(GAP)
except urllib.error.HTTPError as e:
    refused = {"code": e.code, "after_gets": gets, "after_posts": posts,
               "verb": "POST" if e.geturl().startswith(protocol.BATCHEXECUTE_URL) else "GET"}
except Exception as e:
    refused = {"error": f"{type(e).__name__}"}

emit(shape=SHAPE, gets=gets, posts=posts, refused=refused)

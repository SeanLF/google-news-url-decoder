"""Bounds for network calls and caller-supplied input (see issue #18)."""

# Seconds, applied to connect and read, on BOTH transports. urllib3 applies no timeout unless
# asked, so an unresponsive peer would hold the calling thread open indefinitely. httpx has its
# own 5s default, but the async transport is passed this value explicitly rather than left to it:
# a caller supplying their own client got no timeout at all until that was fixed, and two
# transports disagreeing about how long "too long" is would be worse than either number.
DEFAULT_TIMEOUT = 15

# Characters. The token is the last path segment of a caller-supplied URL, so
# its size is not ours to choose. Real tokens run a few hundred characters, and
# bounding the length keeps a crafted URL from being base64-decoded into a
# matching allocation.
MAX_TOKEN_LENGTH = 8192

# Seconds. interval is passed straight to time.sleep()/asyncio.sleep(), so an
# arbitrarily large value pins a thread or task for that long (interval=10**9 is
# about 31 years). Set high enough that a caller's own backoff tuning is never
# silently truncated -- the aim is to bound the absurd, not to second-guess the
# caller -- and clamped rather than rejected so sane values are untouched.
MAX_INTERVAL = 3600


def clamp_interval(interval):
    """Bound a sleep interval to MAX_INTERVAL, leaving anything else untouched.

    Deliberately does no validation. min() would raise on a value it cannot
    compare to a number, and that error would name this module rather than the
    caller's argument, so such values are returned as-is and left for
    time.sleep() to reject with its own message.
    """
    try:
        return min(interval, MAX_INTERVAL)
    except Exception:  # not comparable to a number; let time.sleep() judge it
        return interval

# Bytes, counted DECODED, which is where a compressed bomb expands: a ~1 MB response was measured
# expanding to 1 GB in memory. Neither a plain `gzip.decompress()` nor httpx bounds that, and
# urllib3 only does when asked, so both transports enforce this themselves. Exceeding it raises
# TransportError rather than truncating. Article pages run about 1 MB decompressed, so this leaves
# generous headroom while refusing anything that could only be a bomb.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

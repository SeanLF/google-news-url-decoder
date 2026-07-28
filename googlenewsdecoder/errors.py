"""Exceptions shared between the algorithm and whatever performs the I/O.

A leaf module: it imports nothing else in this package, so both sides of the seam can depend
on it without depending on each other.

That is the whole reason it exists. `TransportError` lived in `transports`, which meant `flow`
imported `transports` purely to name the exception it catches -- and a caller writing their own
driver, precisely to avoid `transports`, still had to import `transports` to get the error type
they were required to raise. The vocabulary of the seam belongs to neither side of it.
"""


class TransportError(Exception):
    """A transport-level failure, independent of which client produced it.

    `status` carries the HTTP status when there was one, so a caller can back off on 429
    and skip on 404 without knowing which library did the sending.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

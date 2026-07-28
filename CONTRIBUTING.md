# Contributing

```sh
pip install -r requirements.txt   # the package, plus pytest and ruff
python -m pytest tests/ -q
ruff check .
```

Tests need no network and take under a second. If you want to check behaviour against live
Google, that is what `probes/` is for — it is not part of the suite.

## The shape, and why

Decoding is four pure steps and two HTTP requests, and they used to be fused together. Three
modules keep them apart:

| module | owns | may import |
|---|---|---|
| `protocol` | what to send and what a response means. Pure functions over strings. | standard library only, at module scope |
| `flow` | the algorithm, as a generator that yields requests | `protocol` |
| `transports` | how bytes actually move | `protocol` |

The direction is one-way: `limits` ← `protocol` ← `transports` ← `flow` ← `decoder`. If a lower
module needs something from a higher one, the dependency is pointing the wrong way — pass it in.

This is the sans-I/O shape ([sans-io.readthedocs.io](https://sans-io.readthedocs.io)), the same
one `h11` uses. The payoff is that the sync and async decoders stopped being two copies of one
algorithm, and that every test can drive a real decode without a socket.

## Traps this project has actually hit

Each of these cost real time here. They are not hypothetical.

### When Google changes the contract, change it in one place

The endpoint has broken repeatedly, and each break produced a new decoder module rather than an
edit — which is why there were five, four of them carrying their own copy of the request
envelope. Copies do not merely duplicate work: **they are wrong together**, and reviewing one
against the others reads as confirmation. All four shared a length-parsing bug for exactly that
reason.

The envelope now exists once, in `protocol`. When Google moves, that is the file. Adding a
`decoderv5` is the failure mode, not the fix.

### The length prefix is a varint

The payload length inside a token is a protobuf varint: one byte below 128, two above. Reading
it as a single byte is correct for short URLs and silently truncates longer ones — and since a
truncated URL still starts with `http`, it comes back as a **successful** decode. A wrong answer
that reports success is worse than an error.

If you touch `unwrap_token`, test across 127/128 and 255/256. Those are where the encoding
changes width, and `tests/test_embedded_url.py` parameterises them.

### A fixture builder is untested production code

That varint bug survived 114 passing tests, because the test helper encoded the length the same
wrong way the parser read it. Encoder and decoder shared one misunderstanding and agreed
perfectly, so every fixture was short enough to work.

Before trusting a green suite, ask what input would fail — then check the helper can even build
it. If it cannot, the suite is measuring the helper's range, not the code.

### Verify the thing you tested is the thing that ships

Three separate times here, the code under test was not the code being shipped: a stale `build/`
and `*.egg-info` made `pip install` build an older tree than the source beside it; `git checkout
-- <path>` restored from the index, so a "revert and confirm it fails" check silently tested the
patched code; and every test file put the checkout ahead of `site-packages`, so the CI job that
installs the built artifact was still testing the source tree.

`tests/conftest.py` now only adds the checkout to the path when the package is not installed.

### Probes must build requests with `protocol`

`probes/` measures Google, not the decoder, so there is no reason to isolate it from the
library — and a strong reason not to. A hand-copied envelope stops matching what Google accepts
without saying so, and the probe keeps reporting confident numbers about a request nobody makes.
That happened: a malformed envelope made every request fail, and the failures were attributed to
token expiry, a conclusion that survived into documentation before being retracted.

### Tests do not touch the network, and that is enforced

`tests/conftest.py` removes the sockets, so a test that reaches out fails naming itself. This is
not ceremony — it caught a behaviour change during the refactor that all other tests missed.
Substitute the transport instead; every entry point takes one.

### Do not tune against the rate limit

Google publishes no limit, no `Retry-After` and no rate-limit headers, so any constant in the
code would be someone's guess from their own address. What is measured (see `probes/README.md`):
it behaves as a per-IP **budget rather than a rate**, only the article GET is counted, and the
ceiling depends heavily on the address. Pacing does not raise the total.

`transports.AdaptiveRateLimit` responds to what it observes rather than encoding a number.

## Style

`ruff check .` is the whole of it; config in `ruff.toml`. `target-version` must track
`python_requires` in `setup.py` — left at the default, the `UP` rules will propose `X | None`
annotations that are a syntax error on the declared floor.

Comments here carry *why*, not what. Several record a decision that looks wrong until you know
what it is avoiding — please keep those, and please delete any that a change makes untrue.

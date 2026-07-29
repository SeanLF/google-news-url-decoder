# Contributing

```sh
pip install -r requirements.txt   # the package, plus pytest, ruff and import-linter
python -m pytest tests/ -q
ruff check .
lint-imports                      # the layering below, enforced
```

Tests need no network and take under a second. If you want to check behaviour against live
Google, that is what `probes/` is for — it is not part of the suite.

## The shape, and why

Decoding is four pure steps and two HTTP requests, and they used to be fused together. Three
modules keep them apart:

| module | owns | may import |
|---|---|---|
| `errors`, `limits` | the vocabulary of the seam, and the bounds | nothing in this package |
| `protocol` | what to send and what a response means. Pure functions over strings. | `errors`, `limits` |
| `flow` | the algorithm, as a generator that yields requests | `protocol`, `errors` |
| `transports` | how bytes actually move | `protocol`, `errors` |

`flow` and `transports` are siblings, not stacked: **neither imports the other.** That is what
makes "bring your own I/O" real rather than nominal. `TransportError` sits in `errors` for
exactly this reason — while it lived in `transports`, a caller replacing `transports` still had
to import `transports` to get the exception they were required to raise.

**This is enforced, not just described.** `.importlinter` holds five contracts and
`lint-imports` runs in CI: the layer order, `flow` and `protocol` not importing `transports`,
`protocol` importing nothing else in the package, `protocol` importing no HTTP client at all,
and the sync and async decoders staying independent. Prose about architecture decays the first
time someone adds a convenient import.

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

`tests/conftest.py` now picks a copy explicitly and refuses to run if it did not get the one
asked for. By default that is the checkout, so your edits are what runs. `TEST_INSTALLED_PACKAGE=1`
selects whatever pip installed, which is what CI uses to prove the built artifact works — and
every run prints which one it got, because that was the fact nobody could see.

Keying this off "is the package importable from anywhere" is what failed before: with the
package installed, sabotaging the source tree still produced a fully green suite, and bare
`pytest` and `python -m pytest` disagreed about the same code.

### A token is not an answer

Some article tokens carry the publisher URL inline, so `decode()` could return it without
asking Google at all. The flows deliberately do not, and `protocol.embedded_url` carries the
warning. The token arrives with the URL you were asked to decode — it is caller input, not
something Google vouched for — so answering from it hands back a string someone else chose,
with `status: True`, having verified nothing. Anything beginning `http` qualified, including
URLs with CRLF in them and hosts nobody expected, and the frame tag was not even required.

The fast path also bought nothing measurable: sampling current feeds finds essentially only
opaque handles. An optimisation with no measured benefit and a new trust surface is a bad
trade twice over.

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

So this package ships no rate limiter, and adding one needs evidence, not plausibility: a
limiter adapts the rate, and a rate cannot buy more of a fixed budget. Pacing belongs in a
caller's transport wrapper.

## Style

`ruff check .` and `lint-imports`; config in `ruff.toml` and `.importlinter`. Ruff's
`target-version` must track `python_requires` in `setup.py` — left at the default, the `UP` rules
will propose `X | None` annotations that are a syntax error on the declared floor.

Prefer the standard library to writing it out longhand. Recent examples from this repo:
`bytes.removeprefix` for the frame tag, `str.isprintable()` instead of enumerating control
characters, `dict.fromkeys` for order-preserving dedup.

Comments here carry *why*, not what. Several record a decision that looks wrong until you know
what it is avoiding — please keep those, and please delete any that a change makes untrue.

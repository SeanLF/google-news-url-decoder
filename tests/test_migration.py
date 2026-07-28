"""Removing the numbered decoders, and what a caller hits when they upgrade.

`decoderv1` through `decoderv4` and `new_decoderv1` are gone. They were five standalone
implementations of one decode whose contracts disagreed with each other -- bare string vs
status dict, `url` vs `decoded_url`, raising vs returning -- so aliasing them to the new API
would have handed callers a shape they did not ask for. They fail instead, naming the
replacement, because `AttributeError: decoderv3` tells nobody anything.
"""


import pytest

import googlenewsdecoder

REMOVED = ["decoderv1", "decoderv2", "decoderv3", "decoderv4", "new_decoderv1"]


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_name_says_what_to_use_instead(name):
    with pytest.raises(AttributeError) as excinfo:
        getattr(googlenewsdecoder, name)
    message = str(excinfo.value)
    assert name in message
    assert "removed in 0.2" in message
    assert "README" in message


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_name_is_not_importable_either(name):
    with pytest.raises(ImportError):
        __import__(f"googlenewsdecoder.{name}", fromlist=["decode_google_news_url"])


@pytest.mark.parametrize("name", REMOVED)
def test_the_guidance_is_lost_on_a_from_import(name):
    """A known limitation, asserted so nobody assumes otherwise.

    `from googlenewsdecoder import decoderv3` is how 0.1.x callers actually wrote it, and it is
    the one form that does NOT show the message above: CPython's IMPORT_FROM catches the
    AttributeError and raises a bare ImportError with no chaining, discarding the text. The
    README carries the mapping precisely because this path cannot.
    """
    namespace = {}
    with pytest.raises(ImportError) as excinfo:
        exec(f"from googlenewsdecoder import {name}", namespace)
    assert "README" not in str(excinfo.value), (
        "if this now passes the guidance through, delete this test and celebrate"
    )


def test_an_unknown_name_still_fails_normally():
    # The migration hook must not swallow ordinary typos into a misleading message.
    unknown = "definitely_not_a_real_name"
    with pytest.raises(AttributeError) as excinfo:
        getattr(googlenewsdecoder, unknown)
    assert "removed in 0.2" not in str(excinfo.value)


def test_the_documented_pre_1_0_entry_point_still_works():
    # `gnewsdecoder` is what the 0.1.x README told people to call, so it stays -- unlike the
    # numbered decoders, its contract is identical to `decode`'s.
    assert googlenewsdecoder.gnewsdecoder is not None


@pytest.mark.parametrize("name", ["decode", "decode_async", "decode_batch", "GoogleDecoder", "GoogleDecoderAsync"])
def test_the_replacements_are_all_present(name):
    assert getattr(googlenewsdecoder, name) is not None

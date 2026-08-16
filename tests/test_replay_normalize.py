"""The nonce mask: wide enough to cover the gap, narrow enough to stay honest."""

from __future__ import annotations

import re

from raven.replay.normalize import mask_untrusted_nonces
from raven.security.trust import wrap_untrusted


def test_masks_every_nonce_a_real_fence_carries():
    """The opening line names the nonce twice and the closing marker once, so a
    mask that catches only one leaves the transcript moving between runs."""
    first = mask_untrusted_nonces(wrap_untrusted("payload", source="read_file"))
    second = mask_untrusted_nonces(wrap_untrusted("payload", source="read_file"))
    assert first == second
    assert first.count("<nonce>") == 3
    assert not re.search(r"#[0-9a-f]{8}", first)


def test_leaves_model_authored_hex_alone():
    """Commit ids and hashes the model wrote are real content: masking them would
    hide a genuine behaviour change behind a tidy diff."""
    text = "fixed in a1b2c3d4 and the digest was deadbeef"
    assert mask_untrusted_nonces(text) == text


def test_leaves_a_bare_hash_reference_outside_a_fence_alone():
    assert mask_untrusted_nonces("see #1a2b3c4d for details") == "see #1a2b3c4d for details"


def test_different_payloads_still_differ_after_masking():
    a = mask_untrusted_nonces(wrap_untrusted("before", source="exec"))
    b = mask_untrusted_nonces(wrap_untrusted("after", source="exec"))
    assert a != b

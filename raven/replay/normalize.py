"""Masking for the few values a replay genuinely cannot pin.

Deliberately small. Every mask hides a real difference, so a mask that reaches
too far turns a regression into a clean diff -- when a transcript moves, the
first question is which source of drift to pin, not which pattern to widen.
"""

from __future__ import annotations

import re

# ``wrap_untrusted`` names its nonce twice in the opening line ("#<nonce>" and
# "tagged #<nonce>") and once in the closing marker.
_FENCE_NONCE_RE = re.compile(r"((?:BEGIN|END) UNTRUSTED [^\n#]*#)[0-9a-f]{8}")
_FENCE_TAGGED_RE = re.compile(r"(marker tagged #)[0-9a-f]{8}")

NONCE_TOKEN = "<nonce>"


def mask_untrusted_nonces(text: str) -> str:
    """Blank the per-call nonce in untrusted-data fences.

    ``raven.security.trust.wrap_untrusted`` mints it with ``secrets`` so injected
    content cannot forge the closing marker: it has to stay unpredictable, which
    means a replay can never reproduce it and comparisons have to mask it. Scoped
    to the two fence phrasings on purpose -- a blanket hex sweep would also erase
    commit ids and hashes the model itself produced, which are worth diffing.
    """
    masked = _FENCE_NONCE_RE.sub(rf"\g<1>{NONCE_TOKEN}", text)
    return _FENCE_TAGGED_RE.sub(rf"\g<1>{NONCE_TOKEN}", masked)

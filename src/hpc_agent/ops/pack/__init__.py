"""Domain-pack ops: bind, record-receipt, status (``docs/design/domain-packs.md``).

The ``ops`` surface over the ``state.pack`` / ``state.pack_receipts`` substrate.
These verbs recompute shas from disk and route every attestation through the ONE
kernel — they never interpret a declared pack value's meaning.
"""

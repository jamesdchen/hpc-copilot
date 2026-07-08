"""Domain-pack mutate/query verbs (``ops/pack/*``).

The pack subject: ``pack-bind`` (T4), ``pack-record-receipt`` (T5), and
``pack-status`` (T6). Each verb reads caller-referenced pack content as DATA and
routes every attestation through the ONE kernel (``state/attestation.py``); core
never imports, executes, or interprets pack logic (``docs/design/domain-packs.md``,
DP2/DP3).
"""

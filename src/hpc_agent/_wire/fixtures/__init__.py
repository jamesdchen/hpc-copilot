"""Cross-cutting wire shapes that are not a single CLI verb's input/output.

Despite the name, this package holds no test fixtures. It is the authoring
home for the *cross-cutting* shapes — ones that ride on many verbs' envelopes
or persist to disk as standalone artifacts, rather than being the ``*Input`` /
``*Result`` payload of one primitive. Verb-scoped shapes live in the sibling
packages (``actions/``, ``queries/``, ``validators/``, ``workflows/``); the
things here are deliberately verb-agnostic:

* ``envelope.py`` — the universal success/error envelope (``EnvelopeAdapter``)
  every CLI verb wraps its payload in.
* ``escalation.py`` — the unified "needs a decision" block (``Escalation``,
  #231) that rides on *either* envelope outcome.
* ``failure_features.py`` — the structured ``failure_features`` evidence block
  (#230) surfaced across diagnosis surfaces.
* ``axes.py`` — ``AxesConfig`` for ``<experiment>/.hpc/axes.yaml``.
* ``campaign_manifest.py`` — ``CampaignManifest`` for ``<campaign_dir>/manifest.json``.
* ``stages.py`` — the ``stages`` array spec (``StagesAdapter``).

**Why these names, and why moving a file is expensive.** None of the shapes
above end in the ``Spec`` / ``Input`` / ``Result`` / ``Report`` / ``Envelope``
suffix that ``scripts/build_schemas.py`` normally dispatches on, so each is
pinned by *name* in ``build_schemas.py::_NON_SUFFIX_MAPPING`` (e.g.
``Escalation`` → ``escalation.json``). Discovery in ``_build_schema_registry_for``
walks every non-private submodule under ``hpc_agent._wire`` and, critically,
only accepts a symbol *defined in the module it is walking*:
``build_schemas.py:139`` skips any object whose ``__module__`` differs from the
walked module (re-imports don't win). Because discovery keys on ``__module__``,
**moving one of these files changes the module a shape is discovered from** —
the emitted JSON's provenance shifts even though the mapping name is unchanged.
Any such move is therefore not free: it MUST be followed by
``python scripts/build_schemas.py --write`` and the roundtrip/parity checks in
``tests/_wire/test_schema_models_roundtrip.py``. Default to leaving files put.

**Where persisted-record schemas scatter (not here).** Shapes that persist a
record to disk but *are* a verb's payload stay under their verb, not in this
package — e.g. ``DecisionRecord`` (the journalled decision line) lives in
``_wire/actions/decision_journal.py`` and is re-exported by
``_wire/queries/decision_journal.py``; the conformance/pack receipt shapes live
under ``_wire/actions/*_record.py`` / ``*_receipt.py``. So the persisted-record
schemas are split by verb ownership across ``actions/`` and ``queries/`` rather
than gathered in one place; this docstring is the pointer that records that
scatter so the layout is discoverable without grepping.
"""

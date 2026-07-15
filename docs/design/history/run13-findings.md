# Run #13 findings docket (live, relay session 2026-07-15)

## 1. Template-compose tiebreak inverts on the receipt-bound domain pack
`[core]` Live: `audit-preflight` (template omitted) silently rebound the
causal_tune_linear audit from the rv program template to
`packs/quant/templates/quant_skeleton.py` (module_sha `dfac68f1`, the lab
copy). Every section then read `modified` against the new template prose and
the gate demanded fresh sign-offs on all five slugs when only
feature-construction had content changes — the human caught it at the
sign-off surface ("if only feature-construction was changed, why do I have
to sign off on the others?").

Root cause: `compose_audit_template`
(`state/pack_declarations.py`) breaks the multi-candidate tie with "the
FIRST pack that is the target of a `receipt_bindings` slot (the program
pack) wins over the domain skeleton" — but in harxhar-clean the
receipt-bound pack IS the domain skeleton (`quant`, via the gate-required
`quant-audit` slot) and the program pack (`rv`, the signed `rv_audit.py`,
`reader_calls` vocab) carries NO receipt binding. The heuristic's
receipt-bound ⇔ program-pack assumption is exactly backwards for the
two-layer quant/rv split (v0.2.0, user-ruled 2026-07-10). Consequence
beyond the sign-off churn: the sidecar/dossier would have carried the
`{pack: quant}` echo instead of the doctrinal `{pack: rv}` echo
(run-12 SESSION_HANDOFF rule).

Gate conduct note: the view_sha invalidation itself worked as designed —
sign-offs bind to section-body × template identity, and a template swap
MUST invalidate them. The defect is upstream, in the silent compose pick.

Live remedy relayed: re-run audit-preflight with
`template=packs/rv/templates/rv_audit.py` explicit → re-enter at lint →
the four unchanged sections re-hash to their previously signed shas; only
feature-construction needs a fresh sign-off.

Fix direction (NEEDS RULING on the selection rule): the principled signal
is the derivation edge, not receipt bindings — rv's manifest `derived_from`
names the quant skeleton sha, i.e. the derived (more specific) template is
the program pack and should win. Candidate rule: among audit_template
candidates, a pack whose template `derived_from` another candidate's
template wins; receipt-bindings tiebreak retired (or kept only as a
last-resort ordering). Whatever the rule, the compose disclosure should
name BOTH candidates and which rule picked the winner, so a wrong pick is
visible at preflight instead of at the sign-off surface.

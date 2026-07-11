# Run #12 findings docket (live, relay session 2026-07-10)

## 1. The on-ramp interviews for what the bound pack already knows
`/new-experiment-hpc` asked the human "Template .py — path, if one exists."
In a pack-opted-in repo the ACTIVE audit template is derivable: the lab
pack's `audit_template` seam (harxhar-clean: `packs/rv/templates/rv_audit.py`,
the prepared 5-slug-conformant template) is bound and gate-required — the
question is the pre-pack legacy surface surviving into a pack world, and an
open path question invites exactly the wrong answers (the unsigned 12-slug
spec, the legacy `specs/…run10.py`). Fix class: poka-yoke — the on-ramp
COMPOSES the default from the bound pack's seam and asks for confirmation,
not a path. Natural home: the experiment-setup materialization verb of the
three-tier distribution ruling (`domain-packs.md` drift log 2026-07-10) —
setup pins the lab pack into `.hpc/` and the template default falls out of
the pin. (User, run #12: "it should be assumed that the 5-slug template is
what is prepared for us to build a specific experiment off of.")

## 2. The on-ramp interviews for experiment_dir — mechanizable from context
The interview asked "experiment_dir — absolute path (this repo, or a
separate dir?)". User: "is there a way to mechanize this? there should be
enough context." There is: the session's cwd git root, when it carries an
`interview.json` / `.hpc` tree (or is where /new-experiment-hpc was
invoked), IS the experiment dir in the standard flow — the question is
another compose-silently-and-disclose seat, same class as finding 1 (the
template default). Fix: default experiment_dir = the invoking repo root;
disclose in the record; ask ONLY when the cwd carries no experiment
markers at all.

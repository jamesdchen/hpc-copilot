# hpc-agent docs — the map

One root per kind of document. A new doc must fit an existing root below —
if it doesn't, the reorg conversation comes first, not a new directory.

| Root | Admission rule |
|---|---|
| [`architecture.md`](architecture.md) | The single top-level overview — layered-DAG map of the package (roles, dependency order). |
| [`design/`](design/) | Why it is this way: per-feature design rationale + drift logs (one file per feature). `design/history/` holds superseded design plans kept for provenance. |
| [`internals/`](internals/) | How it is, for maintainers: subsystem deep-dives, operator workflow guides ([`internals/workflows.md`](internals/workflows.md)), recipes, audits. Operational-truth surface — contract pins (`tests/contracts/_doc_scan.py`) verify its script/path/count references stay live. |
| [`internals/principles/`](internals/principles/) | The engineering-principles sections indexed by [`internals/engineering-principles.md`](internals/engineering-principles.md); the index's section listing is GENERATED (`scripts/regen_all.py`). |
| [`primitives/`](primitives/) | One file per primitive. Frontmatter + the README catalog table are REGENERATED (`scripts/regen_all.py --write`) — never hand-edit those; the body below the closing `---` is hand-written. |
| [`generated/`](generated/) | Whole-file REGENERATED artifacts — never hand-edit; edit the source the regen reads. |
| [`plans/`](plans/) | Live or BANKED work only: plans not yet executed, banked rulings, the live backlog, runsheets not yet run. |
| [`history/`](history/) | Executed plans and fossils: `history/plans/` holds plan docs whose work has landed, finished sweeps/triages/audits, run runsheets, and retired handoff packages. Never retro-edited. |
| [`changelog/`](changelog/) | Released history — verbatim CHANGELOG entries older than the current minor series. |
| [`integrations/`](integrations/) | External integrator contract ([`integrations/CONTRACT.md`](integrations/CONTRACT.md)) — the wire surface other harnesses compose against. |
| [`reference/`](reference/) | Agent-facing wire contracts: CLI envelope, Python API, config precedence, env vars, reducer contract, scheduler states. |

## Where to start

- **New here?** The root [`README.md`](../README.md) — architecture and the human/agent quick starts.
- **Integrating from another harness?** [`integrations/CONTRACT.md`](integrations/CONTRACT.md).
- **Building a campaign loop?** [`internals/campaign.md`](internals/campaign.md).
- **Looking up a primitive?** [`primitives/README.md`](primitives/README.md).

Regen gate: `python scripts/regen_all.py --check` (CI) — if you edited
generated content by hand it will fail and your edit will be clobbered on the
next `--write`. Deferred rebakes are tracked in
[`internals/regen-debt-ledger.md`](internals/regen-debt-ledger.md).

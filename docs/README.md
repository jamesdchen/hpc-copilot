# claude-hpc docs

Navigation map for the docs tree.

## Where to start

- **New here?** Read the root [`README.md`](../README.md) first; it covers the overall architecture and the human / agent quick starts.
- **Integrating with MARs?** [`workflows/mars-integration.md`](workflows/mars-integration.md).
- **Building a campaign loop?** [`workflows/campaign.md`](workflows/campaign.md).
- **Want the wire contract?** [`reference/cli-spec.md`](reference/cli-spec.md).
- **Looking up a specific primitive?** [`primitives/`](primitives/) — one file per primitive, indexed at [`primitives/README.md`](primitives/README.md).

## Layout

```
docs/
├── README.md                  (this file)
├── workflows/                 multi-primitive flows + integration patterns
│   ├── memory-across-campaigns.md   interview → recall feedback loop
│   ├── campaign.md                  closed-loop iteration
│   ├── mars-integration.md          MARs harness integration
│   ├── migration-from-hpc-yaml.md   one-time migration recipe
│   └── mars/
│       └── experiment-runner.snippet.md
├── reference/                 wire contracts; agent-facing
│   ├── cli-spec.md            envelope shape, exit codes, error_codes
│   ├── cli-contract.md        CLI invocation contract
│   ├── agent-surface.md       what the agent sees
│   ├── boundary-contract.md   producer/consumer guarantees
│   └── config-precedence.md   config-resolution order
├── internals/                 subsystem deep-dives
│   ├── queue-wait-predictor.md
│   └── sync-checklist.md
├── primitives/                hybrid: frontmatter auto-generated, body hand-written
│   ├── README.md              indexed catalog (table is auto-regenerated)
│   └── *.md                   one per primitive
└── generated/                 whole-file auto-generated; do not edit by hand
    └── operations.md          `hpc-mapreduce capabilities` rendered as markdown
```

## What's auto-generated vs hand-written

Three categories. Visible signals where they exist:

| Location | Auto | Hand | Regenerator |
|---|---|---|---|
| `generated/*.md` | whole file | none | `scripts/build_operations_index.py` |
| `primitives/README.md` | catalog table (between BEGIN/END markers) | prose around it | `scripts/build_primitive_index.py` |
| `primitives/<name>.md` | YAML frontmatter (between `---` fences) | body below the closing `---` | `scripts/build_primitive_frontmatter.py` |
| Everything else | none | full file | n/a |

CI gates:
- `python scripts/build_operations_index.py --check`
- `python scripts/build_primitive_index.py --check`
- `python scripts/build_primitive_frontmatter.py --check`

If you edit auto-generated content (whole file or marker-bounded section), the next CI run will fail and your edits will be clobbered on regeneration. Edit the source instead — the registry decorator, the schema, or the frontmatter the regen reads.

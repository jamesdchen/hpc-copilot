# `quant` — the upstream DOMAIN pack (distributed content)

This is the **domain layer** of the bind-as-data rigor stack, shipped **in the
hpc-agent repo as distributed CONTENT** per the three-tier pack distribution
model (user-ruled 2026-07-10; see the drift log in
[`docs/design/domain-packs.md`](../../docs/design/domain-packs.md)). It is NOT
core, NOT a capability plugin, and NOT imported by anything under
`src/hpc_agent/`: core binds a pack AS DATA (relpath + raw-bytes sha), carries an
opaque `{pack, version, sha}` echo, and gates on named receipts — it never runs
or interprets a line of pack logic (DP1–DP4). Travelling in core's repo is
*distribution*, not a plugin registration; the trust lane stays content-addressed.

## What it is

A reusable, research-content-free quant methodology skeleton:

| File | Seam / role |
|---|---|
| `templates/quant_skeleton.py` | S4 `audit_template` — five section CONTRACTS as prose (data-selection → target-construction → feature-construction → baseline → metrics), no code bodies |
| `check/check_quant.py` | caller-side STRUCTURAL check → emits the `quant-audit` receipt (DP2: runs outside core) |
| `manifest.json` | GENERATED sealed integrity set (raw-bytes SHA-256 of the two files above) |
| `sweep.json` | build RECIPE (empty `sweep` — a domain pack pins no lab docs) |
| `build_quant_pack.py` | regenerates `manifest.json` (NOT itself sealed) |
| `.gitattributes` | `* -text` — pins verbatim bytes so shas don't drift on checkout |

Seam: `audit_template` → `templates/quant_skeleton.py`. Fills slot: `quant-audit`.

## Portability contract (zero lab symbols)

You could hand `packs/quant/` to an unrelated quant lab and it would be useful
without edits: **no symbol, path, filename, config/metric/transform name, or doc
reference from any one lab appears anywhere under this directory** (verified by
grep at the tier-1 move). The skeleton states CONTRACTS ("call the pinned
loader", "reproduce the baseline live and cite its config sha", "metrics come
from the lab's metrics module"), never a lab's concrete cell. The one fixed
interface is the three-array shape (`pred_raw` / `true_raw` / `baseline_pred_raw`).
Hard naming rule: **no pack / seam / slot name contains `harxhar`** — `harxhar`
is a model name, not a domain (and `manifest.json`'s `name` keys the journal
path `.hpc/packs/<name>.decisions.jsonl`).

The concrete realized-volatility program instance (`rv`) does NOT live here — it
is a lab's CONSUMED instance and stays lab-side (harxhar-clean), created at
program setup by instantiating this skeleton.

## Rebuild

```
python packs/quant/build_quant_pack.py            # regenerate manifest.json
python packs/quant/build_quant_pack.py --check    # CI: fail if stale
```

`check/check_quant.py` verifies whichever ACTIVE program template a deployment
passes via `--template`; with none given it self-checks this pack's own skeleton
(a portable default).

## Design

Full design + the three-tier ruling and its open sub-rulings:
[`docs/design/domain-packs.md`](../../docs/design/domain-packs.md).

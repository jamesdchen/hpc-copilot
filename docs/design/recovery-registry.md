# Proposal: Central typed recovery registry

Status: 8 of 9 kinds ported (contract test, CLI verb). Migration items 1–5
below are done; the one remaining kind (`ssh_unreachable`) is deliberately
deferred per item 6 — see the migration plan at the bottom.

## Problem

Today's `ErrorEnvelope.remediation` string is constructed three ways:

1. **Class-default** — every subclass of `errors.HpcError` declares a
   class-level `remediation` constant (15 + 1 inline in
   `cli/_helpers.py:320`).
2. **Per-raise override** — call sites pass `remediation=...` into the
   exception constructor (only one such site exists today — `_helpers.py:320`,
   the schema-aware `spec_invalid`).
3. **Slash-skill prose** — `hpc-submit/SKILL.md` literal-strings out a recovery
   menu for `already_in_flight` (Step 1b, commit `8986cf5c`); `hpc-aggregate`
   does the symmetric thing in its Step 1b; all three orchestrator skills
   embed the auto-retry-inline menu (commit `88a3869a`). None of those menus
   is keyed off failure-feature evidence — they're tied to *prose paragraphs*
   keyed off `next_step_hint` / spawn-failure text matching.

These three layers drift independently. The empirical drift case is
documented in CHANGELOG.md 0.10.5: the agent's offered recovery options for
`already_in_flight` (`/monitor-hpc`, `--no-canary`, "force") all missed the
correct fix (`hpc-agent reconcile …`), because the skill prose hadn't been
updated. Adding `reconcile` required a per-skill prose edit.

There is also no enumeration of "what kinds can fire" — `FailureCategory`
covers classifier output (9 values + 1 unknown + preempted), envelope
`ErrorCode` covers another taxonomy (15 values), and the slash-skill prose
introduces virtual kinds like `already_in_flight` and `submission_incomplete`
that don't appear in either Python enum.

## Goals

- One canonical place per failure `kind` that lists `{cli_command,
  when_to_use}` recovery options.
- Stable, queryable from the CLI (`hpc-agent recoveries --kind <name>`) so
  SKILL.md prose can reference the enumerated options by name rather than
  re-embedding them.
- A contract test that asserts every known `kind` has a registry entry, and
  that every `ErrorEnvelope.remediation` is sourced from the registry.
- Open the door for the agentic resolver (#234) to read the registry as the
  candidate-action source.

## Non-goals (this prototype)

- Replacing every existing class-default `remediation` constant in
  `errors.py`. The 15 generic remediations stay where they are this round —
  the registry only ports the kinds with a *menu* (≥2 options ordered by
  safety), not single-shot one-liners.
- Editing SKILL.md prose. The WS4 agent is updating those files in parallel;
  this proposal lands the data-layer module + CLI verb + contract test, and
  documents the migration. SKILL.md edits land in a follow-up.
- Per-cluster customization. The registry is flat; per-cluster differences
  (SGE vs SLURM `qdel` syntax) should be carried by the CLI command itself
  via `--scheduler <sge|slurm|…>` flags. See open question 2.

## Module shape

```
src/hpc_agent/recovery/
├── __init__.py          # re-exports registry + lookup helpers
├── registry.py          # the canonical {kind: RecoveryMenu} dict
└── cli.py               # @primitive recoveries — list / show entries
```

### Types

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

# Open vocabulary covering every recovery-keyable failure kind.
# Strictly broader than FailureCategory (classifier-emitted) and ErrorCode
# (envelope-emitted): the registry also keys on prose-only kinds like
# `already_in_flight`, `submission_incomplete` that slash-skill recovery
# menus address but no Python code emits today.
RecoveryKind = Literal[
    # FailureCategory subset (kinds with a recovery menu, not all 13)
    "gpu_oom",
    "system_oom",
    "walltime",
    "node_failure",
    # ErrorCode subset (envelope-emitted with a recovery menu)
    "combiner_failed",
    "outputs_missing",
    "ssh_unreachable",
    # Prose-only / slash-skill-emitted kinds (the empirical drift cases)
    "already_in_flight",
    "submission_incomplete",  # the open verify-canary bug
    "spawn_worker_died",      # the 0.10.3 inline-mode fallback
]


class RecoveryOption(BaseModel):
    """One concrete recovery path."""

    model_config = ConfigDict(extra="forbid")

    cli_command: str = Field(
        description=(
            "Literal command string the operator runs. May contain "
            "<placeholder> tokens (e.g. <run_id>, <scheduler>) the "
            "caller substitutes at emit time."
        ),
    )
    when_to_use: str = Field(
        description=(
            "One-sentence guidance on when this option is appropriate. "
            "Should distinguish itself from the other options in the "
            "menu (no two options should be applicable in the same case)."
        ),
    )
    safety_rank: int = Field(
        ge=0,
        description=(
            "Lower is safer / more reversible. Caller may sort by this when "
            "rendering the menu; the primary recommendation is rank=0."
        ),
    )


class RecoveryMenu(BaseModel):
    """The complete recovery menu for one failure kind."""

    model_config = ConfigDict(extra="forbid")

    kind: RecoveryKind
    summary: str = Field(
        description=(
            "One-sentence description of what this failure kind means — "
            "the framework's diagnosis, separate from the per-call message."
        ),
    )
    options: list[RecoveryOption] = Field(
        min_length=1,
        description="Ordered by safety_rank ascending.",
    )
    references: list[str] | None = Field(
        default=None,
        description=(
            "Optional issue / commit refs that motivated each option, for "
            "audit when an option's wording drifts."
        ),
    )

    def remediation_text(self, *, placeholders: dict[str, str] | None = None) -> str:
        """Render the menu as the `remediation` string for the envelope.

        Format: '(a) <cmd1> — <when1>; (b) <cmd2> — <when2>; …'. Stable
        across calls (no random ordering); placeholders substituted with
        the caller-supplied dict (e.g. {'run_id': 'foo-bar'}).
        """
        ...
```

### Registry

```python
REGISTRY: dict[RecoveryKind, RecoveryMenu] = {
    "already_in_flight": RecoveryMenu(
        kind="already_in_flight",
        summary="A prior run for this cmd_sha is recorded as in_flight in the journal.",
        options=[
            RecoveryOption(
                cli_command="/monitor-hpc",
                when_to_use="The prior submit really is still running.",
                safety_rank=0,
            ),
            RecoveryOption(
                cli_command=(
                    "hpc-agent reconcile --run-id <run_id> "
                    "--scheduler <scheduler> --experiment-dir <experiment_dir>"
                ),
                when_to_use=(
                    "The cluster state is gone (scratch wiped, manual qdel, "
                    "cluster bounce); reconcile polls the cluster, sees the "
                    "dir is missing, marks the journal abandoned, unblocks "
                    "the next submit."
                ),
                safety_rank=1,
            ),
            RecoveryOption(
                cli_command="--no-canary",
                when_to_use=(
                    "Only when the prior run's canary is the in-flight one "
                    "AND the operator has independently confirmed it "
                    "succeeded — NOT a generic workaround for a "
                    "journal-cluster mismatch."
                ),
                safety_rank=2,
            ),
        ],
        references=["#257", "8986cf5c"],
    ),
    # … other entries
}
```

## CLI verb

`hpc-agent recoveries` — Tier-1 `@primitive` so the registry walk picks it
up. Two subcommands via positional argument:

```bash
hpc-agent recoveries list
# → {"ok": true, "data": {"kinds": ["already_in_flight", …]}}

hpc-agent recoveries show --kind already_in_flight
# → {"ok": true, "data": {"kind": "already_in_flight", "summary": "…",
#    "options": [{"cli_command": "/monitor-hpc", "when_to_use": "…",
#    "safety_rank": 0}, …]}}
```

A SKILL.md paragraph can then say: "branch on lifecycle_state; on
'still in-flight', return `spec_invalid: already_in_flight` with the
remediation from `hpc-agent recoveries show --kind already_in_flight`."
The skill prose stops embedding the literal menu; the operator (or the
agent loading the SKILL.md) reads the canonical version from the verb.

## ErrorEnvelope wiring

A new helper:

```python
# in hpc_agent/recovery/__init__.py
def remediation_for(
    kind: RecoveryKind,
    *,
    placeholders: dict[str, str] | None = None,
) -> str:
    return REGISTRY[kind].remediation_text(placeholders=placeholders)
```

Three ported failure kinds wire ErrorEnvelope via this helper:

- `errors.CombinerFailed.remediation` → `remediation_for("combiner_failed")`
- new `errors.SubmissionIncomplete` (the open verify-canary bug) →
  `remediation_for("submission_incomplete")`
- new `errors.AlreadyInFlight` → `remediation_for("already_in_flight")`

Class-default remediations stay valid for everything else; the migration
plan ports them one at a time.

## Contract test shape

```python
# tests/contracts/test_recovery_registry.py
def test_every_known_kind_has_a_registry_entry():
    from hpc_agent.recovery.registry import REGISTRY, RecoveryKind
    from typing import get_args
    for kind in get_args(RecoveryKind):
        assert kind in REGISTRY, f"RecoveryKind {kind!r} missing registry entry"

def test_class_default_remediations_match_registry_for_ported_kinds():
    # For each port-target error class, assert its class-default `remediation`
    # is byte-equal to `remediation_for(<kind>)` — drift catcher.
    ...

@pytest.mark.parametrize("kind", _UNPORTED_KINDS)
@pytest.mark.xfail(reason="kind not yet ported to registry — see migration plan")
def test_unported_kinds_are_ported(kind):
    from hpc_agent.recovery.registry import REGISTRY
    assert kind in REGISTRY
```

The xfail list is the migration punch list.

## Migration plan

Port in this order (cheapest-first / highest-leverage-first):

1. **`already_in_flight`** (prototype) — duplicates across 2 SKILL.md files;
   the empirical drift case. Once registry is read, both skills point to the
   verb.
2. **`submission_incomplete`** (prototype) — net new; closes the open
   verify-canary `job_ids ∈ (None, [])` gap (SESSION_HANDOFF "Still open").
   Requires new `errors.SubmissionIncomplete` subclass.
3. **`spawn_worker_died`** (prototype) — the inline-mode fallback hint
   currently hand-rolled in `_kernel/lifecycle/invoke.py`
   (`missing_credential_remediation`). Recovery menu is the
   `HPC_AGENT_INVOKER=inline` step from commit `88a3869a`.
4. **`combiner_failed`**, **`outputs_missing`** (DONE) — ported from the
   class-default remediations in `errors.py` as multi-option menus. The
   `errors.py` class constants are intentionally left in place for now (the
   registry menu is the canonical source; re-pointing the emit sites at
   `remediation_for(...)` is a separate follow-up to avoid changing envelope
   byte-output in the same change that lands the menus).
5. **`gpu_oom`**, **`system_oom`** (DONE), **`walltime`**, **`node_failure`**
   (already ported) — ported from `failure_signatures.CATALOG.suggested_fix`
   (`gpu_oom` → `increase-mem-per-gpu factor 1.5`, `system_oom` →
   `increase-mem factor 1.5`), with the `gpu_oom` menu also carrying the
   sharded-vs-unsharded discrimination from
   `ops/recover/resolve.py::_gpu_oom_action` (more memory per GPU when
   `tp_size == 1`, reshard/`tp_size` bump when already sharded). The flat
   `suggested_fix` dict is rendered as the menu format.
6. **The remaining `errors.HpcError` subclasses** — port iff they grow a
   multi-option menu. Single-line `remediation` constants stay in `errors.py`
   for now (they're not drift-prone — they're 1:1 with a class). This is why
   **`ssh_unreachable`** (in the `RecoveryKind` open vocabulary but backed by
   the single-line `errors.SshUnreachable.remediation` / `SshSlotWaitTimeout`)
   remains the sole un-ported kind and the lone entry on the contract test's
   migration punch list: it has no multi-option menu today, so porting it
   would only relocate a 1:1 string. Port it if/when it grows scheduler-
   specific or multi-step recovery options.

Per-call-site update:

- For each `raise SpecInvalid(msg, remediation=…)`, replace with `raise
  SpecInvalid(msg, remediation=remediation_for(<kind>, placeholders=…))`.
- For each SKILL.md menu paragraph, replace with prose pointing at `hpc-agent
  recoveries show --kind <kind>` and a one-sentence summary.

## Open questions

1. **Slash-skill human-facing prose vs registry-as-SoT.** Today
   `hpc-submit/SKILL.md` is written for the *agent* (a Claude session
   loading the file as part of its skill). Should the slash-skill prose
   embed `hpc-agent recoveries show --kind <kind>` as a CLI call the agent
   runs, or should `install-commands` materialize the rendered menu into
   the SKILL.md at install time? The first keeps prose drift-free but adds
   a CLI round-trip; the second avoids the round-trip but reintroduces a
   sync window.
2. **Per-cluster recovery variants.** SGE uses `qdel`, SLURM uses
   `scancel`; the `reconcile` option already takes `--scheduler`. Other
   options may have scheduler-specific variants (e.g. memory-bump syntax).
   Do per-cluster variants belong in `RecoveryOption` (one option per
   scheduler) or in CLI command flags (one option whose `cli_command`
   references `<scheduler>` and `recoveries show` doesn't substitute)?
3. **Multi-feature failures.** When a failure carries `kind="gpu_oom"`
   AND `attempts_this_episode.count>=N`, the safe recovery shifts from
   "increase memory" to "give up and escalate." Should the registry key
   on the cross-product (`(kind, attempt-bucket)` tuple) or should the
   caller post-filter `REGISTRY[kind].options` against the
   `failure_features` evidence?
4. **Should `remediation_text` be the SoT for both human and agent
   surfaces?** The agentic resolver (#234) wants structured `options` it
   can choose between programmatically; the human envelope wants a single
   line of prose. Keeping `remediation_text` as a thin renderer over the
   structured `options` lets both consumers stay byte-stable; risk is the
   prose ends up unreadable when options multiply. Open.
5. **Where do placeholder substitutions live?** The CLI verb cannot know
   the call-site `<run_id>`. Should `recoveries show` emit the un-substituted
   menu (current proposal), or accept `--placeholder run_id=foo` flags?

---
name: redeploy-runtime
verb: mutate
side_effects:
- ssh: <cluster> (re-ship framework runtime files, then verify)
- writes-cluster: <remote_path>/.hpc/{_hpc_combiner.py,_hpc_dispatch.py,templates/}
    + hpc_agent/
idempotent: true
idempotency_key: (run_id, deploy_root)
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent redeploy-runtime [--experiment-dir <dir>] --run-id <run_id> [--use-cache]
  python: hpc_agent.ops.redeploy_runtime.redeploy_runtime
---
# redeploy-runtime

Re-ship the framework runtime files an existing run depends on — the combiner
(`.hpc/_hpc_combiner.py`), the dispatcher, the job templates, the importable
stubs — and verify they landed. Submits nothing.

The repair the 2026-07-30 incident did not have. The deployed combiner went
missing from the cluster; the first symptom was the cross-wave reduce failing
hours later with a message about wave partials, and the only way back was a
human hand-launching the reduce over ssh. Every path that ships these artifacts
was welded to a *submit*, and a run being aggregated must not be resubmitted to
get its combiner back.

## Usage

```
hpc-agent redeploy-runtime --experiment-dir <dir> --run-id <run_id>
```

Safe on a run that is queued, running, or finished: the transfer is
temp-then-rename, so replacing `.hpc/_hpc_dispatch.py` under a live array does
not tear it.

## Inputs

| field | required | meaning |
| --- | --- | --- |
| `experiment_dir` | yes (CLI defaults to cwd) | Repo root — locates the journal. |
| `run_id` | yes | The run whose deploy roots to repair. `ssh_target` and `remote_path` are read from its journal record; there is no cluster or path flag to get wrong. |
| `use_cache` | no (default `false`) | Honour the content-hash deploy cache instead of bypassing it. Off by default **on purpose**: a cache hit on a file that is no longer on the cluster is precisely the dropout this verb repairs. |

## Outputs

```json
{
  "ok": true,
  "run_id": "…",
  "ssh_target": "user@host",
  "deploy_roots": ["/scratch/exp", "/scratch/exp/.hpc/trees/ab12cd34ef56"],
  "combiner_rel": ".hpc/_hpc_combiner.py",
  "verified": {"/scratch/exp": {"state": "present", "sha": "98b4…"}}
}
```

`verified[root].state` is one of `present` / `stale` / `absent` / `unknown`.
`ok` is true only when EVERY root reads back `present` — a root whose probe
line never arrived reads `unknown` and does not green the verb, because absence
of evidence is not evidence of repair.

Two deploy roots are repaired when the run is pinned to a content-addressed
code tree (§10.S4). They serve different consumers:

- the **base** `remote_path` is where every control-plane combine runs — the
  per-wave combine, the fused batch, and the `--final` reduce all pass
  `record.remote_path`;
- the **tree** (`job_env["REPO_DIR"]`) is where the JOB `cd`s at run time, so
  its copy is what the array's tasks import.

Repairing both is about covering both consumers, not about the combine leg
following `REPO_DIR` — it does not. (An earlier draft of this page claimed the
combine leg reads the tree while the reduce reads the base; that was wrong and
is corrected here so it is not re-derived.)

Per-root verdicts are attributed by a tag the probe emission carries, not by
the order lines arrive, so a truncated read drops the roots it lost and reports
the rest correctly instead of sliding survivors into the wrong slot.

## Errors

| code | when |
| --- | --- |
| `spec_invalid` | No `run_id`; no journal record for it; or the run has no `remote_path` (a pure-API backend has no tree to deploy into). |
| `ssh_unreachable` | The transfer could not reach the login node. |
| `remote_command_failed` | The transfer failed, or the verification probe did. |

## Idempotency

Idempotent on `(run_id, deploy_root)`. Re-running re-ships the same
package-versioned bytes and re-verifies; the artifacts are content-identical,
so a second run changes nothing but the mtimes and the answer stays `present`.
Running it when nothing was wrong is a cheap no-op with a positive verdict —
which is why the `combiner_missing` recovery menu lists it as the first thing
to try.

## Notes

- Named as the rank-0 option of the `combiner_missing` recovery menu
  (`hpc_agent/recovery/registry.py`), which is what
  `errors.CombinerMissing` attaches as its remediation. The command in the
  refusal and the command in `recoveries-show` are the same string by
  construction.
- The verification probe is the same presence+sha snippet
  `deploy_runtime`'s prelude and the wave-combine preflight fold into their own
  execs (`execution/mapreduce/deployed_artifact.py`) — one definition of what
  "the combiner is deployed" means, across all three.
- This verb does not touch the run's own state: no journal write, no scheduler
  contact, no results. It only restores framework files.
- **Scope, honestly:** the presence check it verifies covers the COMBINER only.
  `deploy_runtime` ships ~16 artifacts and every one rides the same manifest
  that can claim a file is current after it is gone — the 2026-06-08
  `.hpc/templates/` wipe is the same class. This verb re-ships them all (the
  cache is bypassed), but it only *verifies* the combiner. Generalising the
  probe to the whole deploy set is named future work, not a closed gap.

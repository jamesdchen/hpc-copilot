# ops/validate/

## What + why

`ops/validate/` holds the catalog of single-concern pre-submit
validators. Each primitive checks one invariant the submit pipeline
needs to hold before any SSH happens — executor function signatures
match the kwargs callers will pass, the input dataset path resolves
and is readable, the requested wall-time / array size fits the
self-QoS limit, stochastic experiments carry the seed marker, and the
requested walltime is sane against the cluster's historical
distribution. The composite `validate-campaign` workflow (which lives
in `meta/campaign/validate.py`) runs every applicable validator and
aggregates findings into one envelope; per-skill flows pick individual
validators à la carte.

## Invariant

`ops/validate/` promises: typed spec in → list of `ValidatorFinding`
records out (severity `error` / `warning` / `info`), no remote I/O,
no mutation of local state. A finding never raises — the validator
records the problem and returns; the caller decides whether to abort.
A validator that crashes is itself a finding (`validator_crashed`).

## Public vs internal

- Each `*.py` (except `__init__.py`) is one public primitive module:
  `executor_signatures.py`, `input_dataset.py`, `self_qos_limit.py`,
  `stochastic_marker.py`, `walltime_against_history.py`.
- No internal-only files.

## Composition with `validate-campaign`

`meta/campaign/validate.py` calls four of these (input_dataset,
stochastic_marker, walltime_against_history, executor_signatures) by
routing through `hpc_agent.runner` (the cross-subject primitive
bridge — see the architecture doc's "Cross-subject composition"
section). New validators are added here; the campaign workflow
opt-in surfaces them as its skill scope grows.

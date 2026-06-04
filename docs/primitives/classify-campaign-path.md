---
name: classify-campaign-path
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent classify-campaign-path --source-path <source_path>
  python: hpc_agent.incorporation.classify_campaign_path.classify_campaign_path
---
# classify-campaign-path

Stdlib-only AST pattern-match for a campaign's `tasks.py`: is it a
**manual fixed grid** (Path A) or **strategy-driven adaptive sampling**
(Path B)? Read-only fast path used by the campaign `path` decision
point — on a confident hit the worker branches without spending LLM
context; only an unparseable / unrecognized `tasks.py` falls through to
the LLM.

## Purpose

The manual-vs-strategy split is a *structural fact about the code on
disk*: does `tasks.py` import an optimizer (Optuna / scikit-optimize /
Hyperopt / Ax / Nevergrad / …) and drive it with the
`ask`/`tell`/`prior` loop, or does it enumerate a fixed grid? That is
exactly the kind of signal an AST scan resolves deterministically — the
same migration `classify-axis-easy` makes for the `axis_class` point.
This primitive moves the common cases out of the LLM and routes the
verdict through the shared decision kernel
(`hpc_agent._kernel.decision.decide`).

## Output

Returns `{path, decided_by, signals, supports_async_concurrency,
reason, candidates}`:

- `path` — `manual` / `strategy` / `unclassifiable`.
- `decided_by` — `code` on a confident hit (optimizer signals →
  `strategy`; a clean parse with none → `manual`); `judgement` when the
  source did not parse and the point escalates with both candidates.
- `signals` — the optimizer imports / ask-tell calls the scan found
  (the evidence behind a `strategy` verdict).
- `supports_async_concurrency` — a code signal the `concurrency` point
  consults: True only when the strategy is explicitly built for parallel
  asks (Optuna, or an explicit `constant_liar`). Whether the strategy
  *can* run async is a code fact; *how aggressively* to run it in flight
  stays a judgement call.
- `candidates` — on the `judgement` (unclassifiable) branch, the
  options offered to the LLM (`manual`, `strategy`).

Total and never raises: a parse error surfaces as `unclassifiable` in
the envelope `data`, never on an error channel (hence `error_codes:
[]`).

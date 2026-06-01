# Behavioral eval harness (`tests/eval/`)

Structural, regression-grade evaluation of **agent decisions** — the thing
that varies in production. The ~186 unit/contract/property/snapshot tests pin
the *mechanics* of every primitive; none of them catch a prompt/skill edit
that regresses **decision quality** while every contract test stays green.
Given a natural-language request + a repo, does the agent resolve the **right
`submit` spec** — right cluster, right grid/axes, right wave plan, sane
resources? That is what this harness grades. (Pattern borrowed from lara-hpc's
"docstring-as-test" NL→behavior suite; we take the *design*, not the code.)

**Scope (issue #204):** we assert AGENT DECISIONS, not absolute scientific
results. A handful of high-signal cases beats a large flaky suite.

## Two tiers (the lara api / --no-api split)

| Tier | Marker | Needs | What it does |
|---|---|---|---|
| **Offline** | *(none — default)* | nothing (no network, no key) | Unit-tests the `recursive_compare` grader, then drives the **deterministic** half of the submit decision (grid expansion + cluster→backend + `plan-throughput` waves + resource defaulting) for every case and grades it. |
| **LLM** | `slow` + `ANTHROPIC_API_KEY` | a real worker | Drives the real decision skill (inline `WorkerInvoker` / `claude -p`) and grades the resulting envelope. **Skipped** without a key so default + slow-tier CI stay free and offline. |

The offline tier is more than a grader unit test: `resolve_offline` runs the
same `clusters.yaml` load, the same `plan-throughput` pure function, and the
same documented CPU/ML-vs-GPU/DL resource rule the production path uses. A
planner change that mis-packs a 300-task grid, or a default that flips
CPU↔GPU, fails here — offline, for free.

## Layout

```
tests/eval/
  recursive_compare.py   # the structural, float-tolerant grader (stdlib-only)
  cases/__init__.py      # the corpus: EvalCase list (request + fixture + expect)
  resolve.py             # resolve_offline (deterministic) + resolve_via_llm (seam)
  fixtures/<repo>/        # self-contained fixture repos (clusters.yaml + executors + data)
  gold/<case_id>.yaml    # machine snapshots of each resolved spec (regen tripwire)
  _gold.py / regen.py    # read/write + re-baseline the gold snapshots
  test_eval.py           # the pytest entry point (both tiers)
```

## Running

```bash
# Offline tier (default; no key, no network):
pytest -q tests/eval

# LLM tier — opt-in, key-gated; SKIPS cleanly without a key:
pytest -q tests/eval -m slow                 # no key  -> 12 skipped
ANTHROPIC_API_KEY=... pytest -q tests/eval -m slow   # with key
```

The default repo-wide `pytest -q` excludes `slow`, so the LLM tier never runs
there. With a key present the LLM cases currently **xfail** (the autonomous
free-text→envelope driver is intentionally unwired in this first slice — see
`resolve.resolve_via_llm`); the grader + offline corpus are complete, and the
seam is key-gated so it can be filled in without touching them.

## The grader: `recursive_compare`

A ~ tiny, stdlib-only structural diff over dict/list/scalar JSON. Walks the
**gold** shape (a case's `expect` block), so:

- **Subset match for dicts** — the gold names only the fields it cares about;
  extra keys on the candidate (a real envelope) are ignored.
- **Length + element-wise for lists** — a grid of 6 axis values is *not* a
  grid of 5.
- **Exact where it must be** — `cluster`, `grid_points`, `backend`, `axes`.
  (Numbers get a tiny default band so `6` and `6.0` agree, but off-by-one
  still fails; bools are categorical and never float-match `1`/`0`.)
- **Tolerant where it should be** — pass `tolerant={"mem_mb": Tol(rel=0.1),
  "walltime_sec": Range(3600, 21600)}` for heuristic resource asks. Keep
  `cluster`/`grid_points` *out* of `tolerant`.

## Adding a case

1. Append an `EvalCase` to `CASES` in `cases/__init__.py`:
   - `id` — stable slug (names the gold file + the parametrized test).
   - `request_eval` / `request_user` — the same intent in two registers
     (precise vs casual); both must resolve to the same spec.
   - `fixture_repo` — a dir under `fixtures/` with a `clusters.yaml` (+
     executors/data as needed). Reuse `forecasting_repo` / `vision_repo` or
     add a new one.
   - `parsed_axes` — `{axis: [values...]}` an upstream intent parser would
     emit (`executor` is the axis listing executor ids). The offline tier
     Cartesian-expands these; the LLM tier re-parses the request itself.
   - `expect` — the structural gold (cluster, grid_points, axes, resources,
     wave_plan, …), with `tolerant=` for any heuristic numeric.
2. Snapshot its gold:
   ```bash
   HPC_EVAL_REGEN=1 pytest -q tests/eval        # regen + run in one command
   #   or:  python -m tests.eval.regen <case_id>
   ```
3. Commit the new gold file alongside the case.

## Re-baselining gold

Gold YAML under `gold/` is a machine snapshot of the *full* offline-resolved
spec — the regression tripwire. When a **deliberate** change to the
deterministic resolution (a planner heuristic, a resource default, an edited
case) makes it stale, regen and **review the diff**:

```bash
HPC_EVAL_REGEN=1 pytest -q tests/eval     # rewrites gold/*.yaml, still passes
python -m tests.eval.regen                # equivalent standalone script
```

Never regen just to silence a failing test without reading the diff — a
surprising gold change is the suite doing its job.

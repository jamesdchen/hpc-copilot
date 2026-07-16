<!--
  ARCHITECT-MEMO template — copy this file to docs/plans/<program>/ARCHITECT-MEMO.md
  and fill every < ... > placeholder. Delete the HTML comments as you go.

  The memo SETTLES the design so the build units are pure execution. Every
  premortem HIGH/blocking finding must resolve to a binding call HERE; the units
  carry those calls verbatim as `design_constraints` in unit-specs.json (the
  machine-readable twin). If this memo contradicts a unit's brief, THE MEMO WINS.

  Distilled from: handoff-packages-2026-07-12, latency-elimination-2026-07-16,
  daemon-engineering-2026-07-16.
-->

# <PROGRAM NAME> — ARCHITECT MEMO (final)

<DATE> · baseline `main @ <sha>` (memo written at `<sha>`) · inputs: <sweep
docket / N Opus verdicts / M premortem lenses — all folded here>. Machine-readable
twin: `unit-specs.json` in this directory.

<!-- Name any verdict you could NOT settle and how it is carried (gated, PLAUSIBLE-
     UNVERIFIED, deferred). Nothing is silently assumed true. -->
Missing/unsettled verdict: <claim> — carried as <GATED / PLAUSIBLE-UNVERIFIED>,
gated behind unit <id>'s verify-during-build first step (never assumed true).

---

## 0. SETTLED DESIGN CALLS (premortem finding → binding resolution)

<!-- The core of the memo. One row per HIGH/blocking finding. The Resolution column
     becomes a unit's design_constraints entry. MEDIUM/LOW findings stay in the
     premortem files and bind to their unit's constraints without a row here. -->

| # | Finding (lens) | Binding resolution |
|---|---|---|
| Δ1 | <finding, cite the premortem lens> | <the call the units build to; name the unit(s) that carry it> |
| Δ2 | <…> | <…> |

<!-- If your program has no premortems, replace the table with a numbered list of
     the deltas-from-draft design decisions (latency §0 style). -->

**User/maintainer mandate placement**: <any USER-RULED item that outranks the
normal wave ordering — e.g. "unit X's materialization leg is user-mandated and
dispatches as soon as Wave N integrates, regardless of telemetry">.

**DECLINED (no build, no ruling unless reopened)**: <list the claims you are NOT
acting on and why — contradicts a prior posture / ~0 marginal gain / did not
reproduce>.

---

## 1. WAVE PLAN + DEPENDENCY EDGES

<!-- Waves group file-disjoint units that dispatch in parallel. Number them however
     the program reads best (integer 0,1,2… or DW0,DW1,DW3-rung1…) — the checker
     treats the wave label as an opaque key; just be consistent. -->

**Wave 0 — preconditions (sequential, no swarm)**
- (a) **Claim source of truth = `git status`, re-run at dispatch time.** Snapshot
  at memo time: dirty = <files>. Every dirty file is claimed by in-flight work;
  a unit touching one hard-gates on its land and begins with an explicit
  rebase-first step (re-read the seam — docket line numbers are untrusted).
- (b) This plan package + the ruling-needed docket to the maintainer.
- (c) Operator steps (no repo file): <env purges, hook trims, …>.
- (d) Shared pre-wave units (test-fakes instrumentation, etc.) merge before dispatch.

**Wave 1 — <theme> (<N> parallel units, file-disjoint)**
<unit-id> <one-line title> · <unit-id> <one-line title> · …
Integration: merge order <arbitrary except X last> → one regen → push → CI matrix green.

**Wave 2 — <theme> (<N> parallel units)**
<units>. Edges: <cross-unit dependencies — "2.1 needs 1.2 landed", "2.3 lists
2.6's waiter on its KEEP list", …>.

<!-- …one block per wave… -->

**Program-wide integration checklist (each wave close)**: ordered merge →
`python scripts/regen_all.py --write` ONCE → universal trio (`ruff check --fix`,
`ruff format`, `mypy src/hpc_agent`) → targeted batteries incl. named `-m slow`
legs → push → GitHub CI matrix green (Linux gates the win32-skips) →
enforcement-map rows for the wave appended to
`docs/internals/engineering-principles.md` → wall-clock/outcome deltas recorded
in this directory's `telemetry.md`. **Units never commit regenerated artifacts.**

---

## 2. EXPECTED OUTCOME PER WAVE

| Wave | Units | Expected result (verifier-corrected) |
|---|---|---|
| <0> | <units> | <the measurable win / correctness fix this wave lands> |
| <1> | <units> | <…> |

---

## 3. RULING-NEEDED DOCKET (maintainer answers R1–R<N>; nothing here is silently decided)

<!-- One block per ruling. Each names the unit it gates, the amendment (constraint
     OF the grant), and your recommendation. Units gated on a ruling say "iff R<N>"
     in unit-specs.json. -->

**R1 — <topic> (unit <id>).** <state of the substrate; the exact decision>.
Amendment (constraint of the grant): <…>. Recommend: <grant / deny / defer, with
the condition>.

**R2 — <topic> (unit <id>).** <…>

---

## 4. PER-UNIT PRE-PUSH BATTERY

Universal, every unit: `.venv/Scripts/python.exe -m pytest` targeted + backgrounded
(never `uv run`; full suite = GitHub CI, never local) + `ruff check --fix` /
`ruff format` / `mypy src/hpc_agent` + `python scripts/regen_all.py --check`.

| Unit | Targeted tests (default tier) | Extra `-m slow` targets | Lints beyond universal |
|---|---|---|---|
| <id> | <tests/…> | <-m slow files, or —> | <named lints, or —> |

---

## 5. ENFORCEMENT-MAP ROWS OWED

Each landing unit owes its row(s) in `docs/internals/engineering-principles.md`'s
enforcement map at wave integration (fire path named). Assignment: <row → unit
map>. Full row texts are carried per unit in `unit-specs.json`. Every NEW lint
lands with a synthetic-violation fire test (repo standard).

---

## 6. RESIDUAL RISK REGISTER (top 5, from the premortems)

1. <the program's dominant failure mode> — <how it is contained / designed out>.
2. <…>

## Drift log

<!-- Append-only. Record supersessions, mid-flight re-scopes, and any call that
     changed after the memo was written. -->
- <DATE>: <what changed and why>.

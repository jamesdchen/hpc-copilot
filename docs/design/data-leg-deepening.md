# Deepening the INPUT-DATA leg — moving reproducibility capture from DILIGENCE toward MECHANICAL

2026-07-17 · baseline `main @ 6d12cdac` (clean tree). **Design-scoping unit** —
the sole file this unit writes is this memo. No `src/**` change, no commit, no
stash. Cite `path::symbol`; where this doc and the code disagree, the code and
its enforcement-mapped tests win. Concurrent sessions touch
`reconcile`/`mapreduce`, `provenance-manifest`, and `agent_assets`; this memo
READS everywhere and flags coordination points.

## The gap this memo attacks

`docs/plans/reproducibility-program-2026-07-17.md` ranks the DILIGENCE/ABSENT
links by damage × frequency and puts **INPUT DATA capture opt-in (§1) at rank
\#1** — highest damage, high frequency (default undeclared), "the classic crisis
link." The failure it leaves open, verbatim from that audit:

> A run that declares neither `input_datasets` nor `input_roots` writes a
> **byte-identical sidecar with both data fields `null`** and is **silently
> invisible to all data-drift attribution** — no warning at submit. This is
> precisely the quiet-corruption class the manifest was built for: same
> filename, silently rebuilt bytes, every downstream number subtly wrong,
> nothing ever throwing.

**U-DATA1 (landed, `ops/submit_blocks.py::_input_data_brief` L731-778)** raised
the floor: at the S1 resolved boundary a run that declared no input roots now
rides a NEVER-BLOCKING nudge pointing at the ONE declaration field. But U-DATA1
is a *pointer to a human action*, not capture — **the actual fingerprinting is
still OPT-IN**: `state/data_manifest.py::data_identity` (L165-191) returns `None`
the instant `declared_input_roots` (L107-131) reads no
`audited_source.input_roots`. Capture depends on the human (i) declaring roots
and (ii) minting the manifest. This memo scopes how to make capture MORE
mechanical **without overreach** — the hard constraint being that **you cannot
perfectly know what data a black-box script read**, so every option below buys a
different slice of the crisis and leaves a named residual.

## The existing mechanism, as-built (what we deepen, not replace)

| Piece | Symbol | Behavior today |
|---|---|---|
| the ONE input declaration | `state/data_manifest.py::declared_input_roots` L107-131 | reads `interview.json`'s `audited_source.input_roots` via the tolerant `state/interview_doc.py::iter_interview_docs`; **a hardcoded `data/` default is REFUSED by design** — `None` when nothing declared |
| the content-hash manifest | `state/data_manifest.py::mint_manifest` L297-341 | `{relpath:{sha256,size,built_by?}}` over every file under the declared roots + a canonical manifest-doc sha; `(size,mtime)` cache; a mint journal |
| the comparable identity leg | `state/data_manifest.py::data_identity` L165-191 | one sha over `manifest["files"]`; `None` when no roots declared / no manifest minted → the sidecar's `data_manifest_sha` is `null` |
| the second data fingerprint | `state/run_sha.py::compute_data_sha` L213-270 | over the spec's declared `input_datasets` (DVC md5 / raw bytes / `"absent"` sentinel); independent opt-in surface |
| drift, verdict-free | `state/data_manifest.py::compute_drift` L398-439 → `DriftReport` | `matched/drifted/new/missing` counts; `unmanifested=True` when never minted |
| the S1 drift disclosure | `ops/data_manifest.py::render_manifest_disclosure` L105-140 | code-rendered, VERDICT-FREE, NEVER raises/gates; `None` when nothing declared+minted (brief byte-identical) |
| **U-DATA1 the uncaptured nudge** | `ops/submit_blocks.py::_input_data_brief` L731-778 | at S1, an undeclared run rides `no_input_data_declared`; a declared run adds NO key; a wrong-shaped declaration says `checked:False` |

**The doctrine anchor that every option must reckon with.** In TWO places core
deliberately REFUSES to guess data: `declared_input_roots`'s docstring ("a
hardcoded `data/` default is REFUSED by design … core never guesses which
directories are data") and `ops/data_manifest.py::data_manifest`'s
`SpecInvalid` on no-roots-no-declaration. This is a *stated design decision*, so
any option that captures a directory core was not told about is a softening of
it — the difference between the options is **how far** they soften it and
**whether disclosure makes the softening honest.**

**The scan machinery that already exists** (so options a/c/d compose rather than
invent): `infra/transport/_disclose.py::disclose_deploy_payload` (L~90-137)
already walks the experiment repo — `rglob`, exclude-filtered through the shared
`_effective_excludes`/`_path_excluded`, walk-capped, fail-open — and **attributes
bytes to top-level roots** (`top_roots`), surfaced at S1 as `brief["deploy_payload"]`.
And `ops/detect_entry_point.py::_scan_candidates` (L~150-193) is the shipped
precedent for the exact posture the doctrine anchor demands: **scan the repo,
DISCLOSE candidates, let the human confirm — never silently assume.**

---

## The four options along the mechanical-vs-effort gradient

Each: what it BUYS, its RESIDUAL blind spot (what data still drifts invisibly),
platform concerns (native-Windows control plane + Linux cluster), and doctrine
fit (disclose-not-gate; amplification — never block; never silently assume).

### (a) AUTO-DETECT candidate input roots — *disclose candidates, human confirms*

Scan the experiment repo/cwd for **data-shaped** directories the executor likely
reads (heuristics over the shared `disclose_deploy_payload` walk: top-level dirs
by byte weight; conventional names `data/`, `datasets/`, `inputs/`, `raw/`;
data-shaped extensions `.csv/.parquet/.h5/.npz/.feather/.arrow`; DVC `.dvc`
pointers), and surface them **as candidates on the S1 brief for the human to
confirm into `input_roots`** — never mint over them silently.

- **Mechanical-ness: LOW-MEDIUM.** Detection is mechanical; *capture* still
  waits on a human confirm, so it's a smarter nudge (candidates, not a bare
  pointer), not by-default capture.
- **Cost: SMALL-MEDIUM.** Rides the existing repo walk; the only new code is the
  data-shaped classifier + a brief seat.
- **Residual:** a data dir that isn't data-shaped (odd name, no telltale
  extension), and — shared with c/d — **any data read from OUTSIDE the repo**
  (an absolute path, `/scratch`, a network mount, an S3/DB URL) is invisible to
  a repo scan. The human can also decline every candidate.
- **Platform:** pure **local control-plane** scan of the repo present at submit
  time → fine on native Windows, zero cluster dependency.
- **Doctrine fit: EXCELLENT.** It is the `detect_entry_point` posture exactly —
  propose + disclose + human-confirms — so it does NOT cross the "core never
  guesses" line (it proposes, it does not capture). Amplification-pure. **No
  ruling needed.**

### (b) FILESYSTEM-ACCESS CAPTURE at run time — *the compute node records what the task opened*

Instrument the task on the compute node (strace/ptrace, `LD_PRELOAD` open-shim,
eBPF/fanotify) to record every path the process actually read, then fingerprint
that set.

- **Mechanical-ness: HIGHEST.** It captures the *actual bytes read* — including
  data outside the repo and dynamically-chosen paths — which no repo scan can.
- **Cost: HEAVY.** And the capture is *noisy*: the raw open-set is dominated by
  libc, the Python stdlib, `.so`s, and temp files, so it must be filtered back
  down to "input data" — which re-introduces exactly the heuristics (a) uses,
  now on a far larger set.
- **Residual:** still misses mmap/lazy-read edge cases; and the noise-filter's
  false-negatives are themselves a new silent blind spot.
- **Platform: SEVERE — the disqualifier.** ptrace/strace/eBPF are **Linux-only**
  and routinely **unavailable on shared HPC** (no root, ptrace disabled on
  locked-down login/compute nodes, container/seccomp restrictions), and vary per
  scheduler/site. The **native-Windows control plane cannot even prototype it
  locally.** It also violates the "import-safe on every runtime surface" house
  rule (`docs/internals/engineering-principles.md` Q3) and is an
  actuation-adjacent instrument on the compute node — outside the observe/judge
  scope.
- **Doctrine fit: POOR.** Heavy, platform-coupled, and the wrong altitude.
  **Not recommended.** Kept in the memo only to name the ceiling: "most
  mechanical" is not "right."

### (c) DECLARE-BY-CONVENTION — *a `data/` dir captured by default unless opted out*

When nothing is declared, treat a conventional `data/` directory (the single
most-cited crisis pattern) as an input root **and mint over it by default** —
DISCLOSING that it did — unless opted out (`HPC_NO_DATA_CONVENTION=1`, the
`HPC_NO_DOUBLE_CANARY` precedent, or an interview flag). This **flips opt-in →
opt-out** for the common case.

- **Mechanical-ness: HIGH** for repos that follow the convention — capture
  happens with zero human action, which is the actual move toward MECHANICAL.
- **Cost: SMALL.** One resolver change + the mint trigger + the opt-out switch.
- **Residual:** repos whose data is NOT under `data/` (elsewhere, or an absolute
  path) are still uncaptured — but paired with (d) that blind spot is *disclosed*
  rather than silent.
- **Platform:** local scan → fine on Windows, no cluster concern.
- **Doctrine fit: THE RULING.** This is the one option that touches the exact
  functions whose docstrings say "core never guesses which directories are
  data." Capturing-and-disclosing `data/` by convention is **not** the
  silent-assume failure (a hardcoded default that acts invisibly): the human is
  TOLD on the S1 brief ("captured `data/` by convention — opt out with X") and
  can reject it, and it never gates. But it *is* a softening of a stated design
  decision — a **default flip** — so it **needs the user's sign-off** (see
  Ruling RD1). Grantable precisely because disclosure + opt-out keep it inside
  disclose-not-gate; but not the agent's call to grant.

### (d) POST-HOC COVERAGE DISCLOSURE — *fingerprint what we have, name what we don't*

Fingerprint the declared + (a)-detected roots and DISCLOSE the **coverage** at
the S1 boundary: *"captured N roots (`data/`, `inputs/`); M candidate dirs
unconfirmed (`raw/`, `fixtures/`); runs may also read data outside the repo —
uncaptured."* Extends the existing `_input_data_brief` seat.

- **Mechanical-ness: N/A — it captures nothing new.** Its job is to make the
  **blind spot itself mechanical and visible**: convert a silent-`null` into a
  named coverage number at the human boundary.
- **Cost: SMALL.** Rides (a)'s detection and U-DATA1's shipped brief seat.
- **Residual:** by itself it changes no bytes — data still drifts invisibly
  unless the human acts on the disclosed candidates. It is the *honesty layer*,
  not the capture layer.
- **Platform:** local, fine on Windows.
- **Doctrine fit: PERFECT.** Pure disclosure, never gate, never assume — the
  most doctrine-pure move on the board, and the one that keeps (c)'s opt-out
  default HONEST (an opt-out capture that never disclosed its own
  incompleteness would be a new silent hole; (d) forecloses that).

---

## Ranking

| By **capture power** (mechanical-ness) | By **doctrine-fit × cost** (what should ship) |
|---|---|
| b > c > a > d | a ≈ d > c > b |

The two orderings crossing is the whole point: **(b) is the most mechanical and
the least shippable** (platform/cost/altitude), while **(a)+(d) are the most
doctrine-pure and cheapest but don't flip the default**, and **(c) is the only
one that moves capture to by-default — at the price of a ruling.** The
recommendation weights doctrine + platform + cost and rejects (b) outright.

## Recommended build path: (a) as the engine, **c+d** as the shipped surface

Build **(a)** as the shared detection engine, feeding **both** (c) the
by-convention default-capture of `data/` **and** (d) the coverage disclosure of
everything detected-but-unconfirmed. c is the mechanical win (the common case
captured with no human action); d is the mandatory companion that keeps c's
opt-out default honest; a is the engine both stand on. **(b) is OUT.**

If the user **declines RD1** (the default flip), ship **a+d alone** — it's
ruling-free, raises the diligence floor materially over U-DATA1 (candidates +
coverage instead of a bare pointer), and keeps confirm-to-capture. a+d is the
fallback the moment c is blocked; it is not wasted work, it is c's substrate.

**Files & size** (extend shipped verbs / compose shipped walks; render is pure
code; every unit is IDENTITY/COUNTING over opaque records; no new gate; bare `y`
stands):

| Unit | Files | Size |
|---|---|---|
| (a) detection engine | NEW `ops/detect_input_data.py` (or a `state/` helper) composing `infra/transport/_disclose.py`'s exclude-filtered walk + a data-shaped classifier; **coordinate** the shared-walk reuse so no copy drifts | SMALL-MEDIUM |
| (c) by-convention default capture **[behind RD1]** | a resolver `effective_input_roots` beside `state/data_manifest.py::declared_input_roots` that falls to the `data/` convention when nothing is declared, gated by `HPC_NO_DATA_CONVENTION`; the mint/`data_identity` path picks it up; **the two "core never guesses" docstrings + the verb `SpecInvalid` must be re-authored, not contradicted** | SMALL (doctrine-loaded) |
| (d) coverage disclosure | extend `ops/submit_blocks.py::_input_data_brief` to emit `{captured_roots, candidate_unconfirmed, outside-repo caveat, coverage_line}`; optional additive sidecar `data_capture_coverage` (backfill-`None`, byte-identical when absent) so coverage is durable, not brief-only | SMALL |
| plumbing | regen (the six scripts) · a boundary/contract test (the **never-gate** pin, the **byte-identical-when-declared** pin, the **detect-discloses-candidates-never-auto-mints** pin — the dossier AST-scan precedent) · one `docs/internals/engineering-principles.md` enforcement row · this doc → build spec | SMALL |

Total: **MEDIUM.** Coordinate with the `provenance-manifest` session (whether a
`data_capture_coverage` marker joins the signed manifest — likely yes, the cheap
schema bump the env-lock work just took, `PROVENANCE_MANIFEST_SCHEMA_VERSION`).

**Disclosure-not-gate check:** ✓ for (a)/(c)/(d). (c) captures-and-discloses,
never refuses; (d) is pure observation; (a) proposes, never mints. A contract
test asserts each disclosure path has no `raise`/gate branch (the
`data-manifest` never-blocking pin).

## The ruling flagged

**RD1 — declare-by-convention: flip input-data capture from opt-in to opt-out
for a conventional `data/` dir?** *Recommend YES, disclosed + opt-out.* It is
the single change that moves the \#1 reproducibility gap from DILIGENCE toward
MECHANICAL for the common case, at SMALL cost, and disclosure + opt-out keep it
inside the bare-`y`/disclose-not-gate doctrine. **But it is a default flip that
directly softens a stated design decision** (`declared_input_roots` /
`data_manifest` both refuse to guess a `data/` dir today), so it **is not the
agent's call — it needs the user's sign-off.** If declined, ship **a+d** (no
ruling) and leave capture confirm-gated. Everything else in the build path
(the (a) detection engine, the (d) coverage disclosure) is ruling-free and ships
regardless.

---

## Drift log

**2026-07-17 — created (design-scoping only).** Cites the reproducibility
program's gap \#1 (`docs/plans/reproducibility-program-2026-07-17.md` §1 + the
damage×frequency table: INPUT DATA capture opt-in, rank 1, highest damage) and
the extraction scoping doc (`docs/plans/clean-reproduction-extraction-2026-07-17.md`).
Scopes how to move the input-data reproducibility link from DILIGENCE toward
MECHANICAL **without overreach** — the bounding constraint being that a
black-box script's true read-set is unknowable, so each option buys a slice and
names a residual. Read the as-built opt-in mechanism fully:
`state/data_manifest.py` (`declared_input_roots` L107-131, `data_identity`
L165-191, `mint_manifest` L297-341, `compute_drift` L398-439),
`ops/data_manifest.py::render_manifest_disclosure` L105-140, U-DATA1's nudge
`ops/submit_blocks.py::_input_data_brief` L731-778, the second fingerprint
`state/run_sha.py::compute_data_sha`, and the doctrine anchor (core REFUSES to
guess a `data/` dir in two places). Established that the shipped repo-scan
machinery (`infra/transport/_disclose.py::disclose_deploy_payload`) and the
disclose-candidates posture (`ops/detect_entry_point.py::_scan_candidates`)
already exist for options (a)/(c)/(d) to compose. Scored four options along the
mechanical-vs-effort gradient — (a) auto-detect candidates, (b) filesystem-access
capture, (c) declare-by-convention, (d) post-hoc coverage disclosure — each with
mechanical-ness / cost / residual / platform (native-Windows control plane +
Linux cluster) / doctrine fit. **Ranking:** capture power b > c > a > d;
doctrine-fit × cost a ≈ d > c > b — the crossing is the finding. **(b) rejected
outright** (Linux-only tracing unavailable on locked-down HPC, un-prototypable
from the Windows control plane, noisy, actuation-adjacent — "most mechanical" ≠
"right"). **Recommended build path: (a) as the shared detection engine feeding
c+d** — (c) by-convention default-capture of `data/` as the mechanical win,
(d) coverage disclosure as the mandatory honesty companion that keeps (c)'s
opt-out honest, (a) the engine both stand on; MEDIUM, files listed, every unit
disclosure-not-gate. Fallback **a+d** (ruling-free) if RD1 is declined.
**One ruling flagged — RD1: flip input-data capture opt-in → opt-out for a
conventional `data/` dir (recommend YES, disclosed + opt-out), a default flip
that softens a stated design decision and therefore needs the user's sign-off.**
No `src/**` touched; no commit; no stash. Coordination point named with the
concurrent `provenance-manifest` session (a `data_capture_coverage` marker under
the signature).

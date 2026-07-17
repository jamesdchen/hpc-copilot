# MEASUREMENT — the stateless dispatch floor (R4 measure-then-decide)

Protocol for `scripts/measure_dispatch_floor.py`, the harness that produces the
evidence the maintainer's **ARCHITECT-MEMO sec 2a** ruling gates on. The script
is the *evidence producer*; it does **not** rule. It measures the real per-call
floor per surface on the primary Windows box and prints the numbers sec 2a names
as the gate. A human applies step 3.

## Why this exists (sec 2a, verbatim intent)

> "if stateful warm processes are convenient but break constantly, perhaps the
> best thing is to get the stateless CLI path latency down so much that the warm
> start doesn't matter."

Sec 2a amends R4:

1. Latency waves 1–2 (the stateless program) land FIRST.
2. Then MEASURE the real post-wave per-call floor per surface (hook, CLI verb,
   block-drive span) on the primary Windows box — **including the irreducible
   spawn + Defender/AV tax the census left unmeasured**.
3. **DW1+ (the 13 units of daemon lifecycle machinery) builds ONLY if the
   residual gap still justifies the program** — daemon warm call ≈ 15–20 ms vs
   projected stateless floor. Otherwise the WS-DAEMON package stands as
   design-of-record, shelved. This INVERTS the prior cut-line (which shipped
   DW0–DW2 unconditionally).
4. D-FSYNC extracts and builds NOW regardless (a correctness win for the
   stateless path too) — it is **not** gated by this measurement.

So this harness gates DW1+ only. It has no bearing on D-FSYNC.

## When to run

- **After latency waves 1–2 land AND a fresh wheel is installed** into
  `.venv` (`importlib.metadata.version("hpc-agent")` reflects the post-wave
  build). The whole point is to measure the *post-wave* stateless floor; a
  pre-wave run is a baseline only, explicitly labelled as such.
- On the **primary Windows box** (the box the memo names). The spawn + AV tax is
  platform- and machine-specific; a number from another box does not gate.
- On a **clean tree**. The script records `git rev-parse HEAD` and a
  `git status --porcelain` dirty-line count and prints a loud banner when the
  tree is dirty. A dirty-tree run is labelled `dirty=True` in the JSON and MUST
  NOT be cited as a gate number — it is a mid-swarm smoke only.
- Latency unit 1.3 (`hpc_agent/__init__` de-eagerment) is a HARD prerequisite of
  the fast-path numbers (memo Δ17). If 1.3 has not landed, the `import` and
  `fast_path` surfaces are inflated and the gate reading is not valid.

## Usage

```
.venv/Scripts/python.exe scripts/measure_dispatch_floor.py
.venv/Scripts/python.exe scripts/measure_dispatch_floor.py --runs 7 \
    --fast-verb describe find --full-verb capabilities --out report.json
```

- `--runs N` — samples per surface (default 7). Report is median + min + all
  samples per surface.
- `--fast-verb …` — the verb argv treated as the fast-path surface (default
  `describe find`). **Set this to the actual post-wave fast-path verb** if the
  mapping changed.
- `--full-verb …` — the full-registry-walk verb (default `capabilities`).
- `--out PATH` — JSON report path (default: a timestamped file in the temp dir).
- `--cold-claim` — assert this is the first run after a real reboot; labels
  `boot_state: user-asserted-cold`. Do not pass it otherwise.

Never uses ssh; never mutates the repo. The interpreter is whatever runs the
script (`sys.executable`) — always launch it with `.venv/Scripts/python.exe`.

## Surfaces measured

| key | what | why |
|-----|------|-----|
| `bare` | `python -c pass` | irreducible spawn + Defender/AV tax, no import |
| `bare2` | same spawn, 2nd series | spread vs `bare` **is** the spawn/AV variance signal |
| `import` | `python -c "import hpc_agent"` | spawn + top-level package import |
| `fast_path` | cold `hpc-agent <fast-verb>` in a fresh subprocess | the CLI-verb rung floor |
| `full_walk` | cold `hpc-agent <full-verb>` | the full-registry-walk floor |
| `hook` | `python -m …stop_multiplex` + minimal Stop payload on stdin | the per-turn hook rung floor (DRY — see caveats) |
| `warm` | the fast-path verb WARM in-process (import once, warm-up call, then timed) | the reference floor a warm daemon approaches |

Subprocess surfaces are **interleaved**: each round rotates the surface order so
no single surface consistently pays (or dodges) the coldest filesystem state
(defeats cache-warming order bias).

## Which numbers gate what

The **decision line** the script computes (sec 2a gate numbers):

- **cold fast-path floor** (median of `fast_path`) — the headline stateless
  number the daemon would replace.
- **cold full-walk floor** (median of `full_walk`) — the heavier CLI rung.
- **warm reference (measured)** vs the memo's **15–20 ms** band — the daemon's
  warm-call target. Note: the measured warm number reflects whatever the chosen
  verb costs warm in-process; if that verb re-walks per call it will exceed the
  15–20 ms band, which is a *projection/target*, not a claim about this verb.
- **per-turn hook cost** = `HOOKS_PER_TURN (3) × hook median` — a turn fires
  ~3 hooks (UserPromptSubmit capture + Stop multiplex + a PostToolUse fence).
- **spawn variance** = |bare − bare2| medians — the AV/spawn wildcard magnitude.
- **residual gap vs warm band** = cold fast-path median − 20 ms.

**The gate (sec 2a step 3):** DW1+ builds **iff** the residual gap — measured
here post-wave — still justifies 13 units of lifecycle machinery, judged against
the ≈15–20 ms daemon warm call. The script's `reading` field is advisory:
- cold floor within ~1.5× the 20 ms band → residual gap SMALL → leans SHELVE.
- cold floor well above the band → gap remains → the maintainer decides whether
  it earns the machinery, **comparing against the projected post-wave target,
  not the pre-wave baseline**.

The script never emits the ruling. It emits the numbers and cites sec 2a.

## Honest-reporting rules (baked into the harness)

1. **Filesystem-cache warmth is NOT controlled.** This box has already imported
   the tree (pytest, prior runs). `first_run_of_boot` is left `null` on purpose;
   `boot_state` is `warm-uncontrolled` unless `--cold-claim` is passed after a
   real reboot. We do not pretend to a cold-cache number we cannot honestly
   produce.
2. **The dirty tree is labelled, loudly.** `git HEAD` + dirty-line count go into
   the JSON and a banner prints when dirty. A measurement taken mid-swarm is
   visibly not a clean baseline — it is never silently trusted.
3. **The hook surface is DRY.** The payload's `cwd` has no `.hpc` and
   `HPC_JOURNAL_DIR` points at a nonexistent dir, so the Stop prefilter proves
   every guard is a no-op and returns 0 **without importing the heavy guard
   chain** — zero side effects, no journal writes. This measures the *no-op
   per-turn hook floor* (interpreter + `stop_multiplex` import + stdin read),
   which is the common case (most Stop events do no guard work). A
   **guard-active** Stop pays an additional `import hpc_agent`-class cost on top;
   estimate it as roughly `hook + (import − bare)`. The caveat travels in the
   JSON (`surfaces.hook.caveat`).
4. **Spawn/AV variance is surfaced, not hidden.** Measuring the bare spawn twice
   and reporting both medians makes the AV wildcard a first-class number rather
   than noise folded into a single figure.
5. **Env fingerprint travels with every report** — timestamp, python version,
   wheel version (`importlib.metadata`), box name, platform — so a number can
   never be cited without its provenance.

## Report schema

JSON `schema: hpc.measure_dispatch_floor.v1`. Top-level keys: `runs_per_surface`,
`env`, `git`, `boot_state`, `boot_state_note`, `first_run_of_boot`, `surfaces`
(per-surface `median_ms` / `min_ms` / `max_ms` / `mean_ms` / `n` / `samples_ms`
/ `desc`), `decision` (the gate numbers + advisory `reading` + `ruling_ref`).

## Tests

`tests/scripts/test_measure_dispatch_floor.py` — the statistics and report
assembly are unit-tested with injected timings (no real spawns in CI). One
`@pytest.mark.slow` smoke actually spawns the two cheapest surfaces (bare,
import) once each and asserts sane bounds (>0, <60 s).

## GATE RUN — 2026-07-17, wheel d71a690b (CI-green b1ea0d5d), uv-tool env

Measured on the installed wheel (BUILD_SHA present → baked hydration active;
the dev .venv is a source checkout and always walks, so it is NOT the gate env):

| surface | median |
|---|---|
| bare interpreter | 209 ms |
| import hpc_agent (lazy root) | 413 ms |
| cold fast-path (describe/find, baked) | **2207 ms** |
| cold full-walk (capabilities, deferred) | 8584 ms |
| fused Stop hook (dry) | 528 ms/turn |
| WARM in-process (daemon target) | **27 ms** |

**Residual gap: ~2180 ms per cold call; ~500 ms per fused-hook turn.**
Pre-wave baseline was ~7000 ms cold, so the bake delivered ~3.2×. But the
Windows spawn+import floor (~600 ms just for `import hpc_agent`) is
irreducible without a warm process, so the stateless path plateaus ~2.2 s —
still ~80× the 27 ms warm path. The R4 sec-2a step-3 call is the maintainer's.

## GATE RUN — 2026-07-17, wheel 06796de3 (CI-green 93461364), uv-tool env

The post-reduction re-measure the amended R4 ruling asked for ("REDUCE
STATELESS FURTHER" — the c936a345 wave: lazy `infra` re-exports, deferred
pydantic/plugins/`importlib.resources`). Same box, same uv-tool gate env,
`--runs 7` (`full_walk` advisory n=3 via the new `--full-runs` cap).
`boot_state: warm-uncontrolled`; the fast_path max (2712 ms) shows a load
spike round — read the median/min, not the max.

| surface | median | min |
|---|---|---|
| bare interpreter | 104 ms | 84 ms |
| import hpc_agent (lazy root) | 170 ms | 135 ms |
| cold fast-path (describe/find, baked) | **566 ms** | 460 ms |
| cold full-walk (capabilities, deferred) | 3750 ms | 3210 ms |
| dry Stop hook (single) | 201 ms | 179 ms |
| WARM in-process (daemon target) | **20 ms** | 14 ms |

**Cold fast-path 2207 → 566 ms (3.9×; cumulative from pre-bake ~7000 ms:
~12×).** The reduction also carried `full_walk` 8584 → 3750 ms (capabilities
inherits the lazy-infra cut) and `import hpc_agent` 413 → 170 ms. Hook
single-median 201 ms is within load noise of the prior run's ~176 ms — the
reduction never touched the hook chain. Composition of the remaining 566 ms:
~100 ms spawn+AV, ~70 ms lazy-root import, ~400 ms registry/parser/catalog
dispatch — the part only a warm process removes. Residual gap vs warm:
~546 ms per cold call, ~28× (was ~80×).

R4 sec-2a step-3 remains the maintainer's call. Two decision notes from the
2026-07-16/17 sessions: (a) this floor measures LOCAL dispatch only — the
daemon's other prize, SSH-handshake amortization across verb invocations
(one asyncssh pool vs per-process cold connects), is not in this number and
is argued in the maintainer's transport-brittleness thread; (b) the
per-fused-hook-turn cost (~600 ms at 3 hooks/turn) is now the larger
recurring stateless tax.

## GATE RUN — 2026-07-17, wheel e9ee1f4e (CI-green fb498ff7), uv-tool env

Wave-2 cold-path datum (the fb498ff7 wave: describe-cache BUILD_SHA
content-keying, leaf homedir resolver, host-first schema roots,
error-path-only ssh_agent). `--runs 9`. NOTE an earlier same-wheel run was
DISCARDED as load-poisoned per the honest-reporting rules — its own `bare`
medians (162/217 ms vs the quiet-box ~84 ms) proved ambient load, not code;
this run's spawn variance is 2.4 ms (quiet box, cleanest conditions of the
three gate runs — cross-run comparisons should lean on the bare-relative
deltas, not absolute medians alone).

| surface | median | min |
|---|---|---|
| bare interpreter | 84 ms | 75 ms |
| import hpc_agent (lazy root) | 126 ms | 121 ms |
| cold fast-path (describe/find, baked) | **321 ms** | 305 ms |
| cold full-walk (capabilities, deferred) | 2199 ms | 2196 ms |
| dry Stop hook (single) | 152 ms | 144 ms |
| WARM in-process (daemon target) | **13 ms** | 12 ms |

**Cold fast-path 566 → 321 ms (wave-2); cumulative pre-bake ~7000 → 321 ms
(~22×).** full_walk rode along 3750 → 2199 ms. Residual composition:
~84 ms spawn+AV (irreducible stateless), ~42 ms lazy-root import over bare,
~195 ms registry/parser/catalog dispatch over import. Hook single 152 ms
(~456 ms/turn at 3 hooks); the hook's only reachable stateless lever is the
`_PACKAGE_ROOT` Path→str ruling (~10-15 ms/fire — `re` is anchored by
`json`, see the 2026-07-17 hook-floor investigation), which is PARKED
awaiting the maintainer. Wave-2 also closed the describe-cache
same-version-reinstall staleness trap (content-keyed on BUILD_SHA;
source-checkout/dirty-wheel = cache disabled).

## GATE RUN — 2026-07-17, wheel 8a940f3b (CI-green 6af9a24e), uv-tool env — LOAD-AFFECTED

Post-mega-wave measure (the 6af9a24e train: pathlib-free root/hooks,
capabilities cache, reconcile backstop, verify-relay, consent hints,
transport hardening). **NOT a clean gate number — the box was under load
from concurrent build/measure activity: `bare` measured 115 ms vs the
84 ms of the wave-2 quiet-box run, and `fast_path` max spiked to 4734 ms
(a single load blip). Read bare-RELATIVE deltas, not absolutes.**

| surface | median | vs bare |
|---|---|---|
| bare interpreter | 115 ms | — |
| import hpc_agent | 138 ms | **+23 ms** (was +42 ms wave-2 — the pathlib cut) |
| cold fast-path (describe/find) | 402 ms | +287 ms (inflated by load; cf. wave-2 clean 321 ms) |
| cold full-walk (capabilities) | 2945 ms | — |
| dry Stop hook (single) | 212 ms | +97 ms over bare |
| WARM in-process | 16 ms | — |

**Read:** the pathlib-free work shows up where it should — import-over-bare
fell ~42→~23 ms. The absolute 402 ms cold median is load-inflated, NOT a
regression against wave-2's 321 ms (bare itself rose ~37 %); a clean re-run
on a quiet box is the number to cite for the record. Warm floor holds at
16 ms. The mega-wave's value was correctness + robustness (reconcile bug,
verify-relay, transport hardening, consent) and the capabilities-cache /
hook-floor latency cuts; the stateless cold floor was already near its
spawn+import plateau after wave-2.

"""The DEPLOYED combiner artifact: its identity, its probe, its absence.

``deploy_runtime`` ships :mod:`hpc_agent.execution.mapreduce.combiner` to the
cluster as ``.hpc/_hpc_combiner.py``, and three separate control planes then
*depend* on that file existing: the per-wave combine
(:func:`hpc_agent.infra.transport.run_combiner`), the fused multi-wave batch,
and the cross-wave ``--final`` reduce. Until this module, none of them ever
ASKED whether the file was there — they just ran ``python3
.hpc/_hpc_combiner.py`` and read whatever the shell said.

The 2026-07-30 incident is what that costs. The deployed combiner went missing;
the first thing that noticed was ``[combiner] ERROR: no _combiner/<run_id>/
wave_*.json`` at 20:52 — a message about *wave partials*, emitted by a
different code path, hours after the cause — and a human hand-launched the
final reduce over ssh at 22:00. The artifact's absence was never named.

Three ways a staged run ends up with no deployed combiner
---------------------------------------------------------

**D1 — the skip-staging re-entry.** ``skip_rsync_deploy`` (``skip_prelude_io``)
drops the whole ``_push_and_deploy`` arm, so ``deploy_runtime`` never runs; the
re-entry's only cluster read is ``_resolve_existing_code_tree``, which confirms
the code tree's SEAL marker and nothing about ``.hpc/``. The post-deploy
executor-existence preflight is explicitly skipped on that path too. A phase-2
submit against a base tree whose ``.hpc/_hpc_combiner.py`` is gone deploys
nothing and verifies nothing.

**D2 — the deploy cache's presence lie (the root cause this module closes).**
``deploy_runtime``'s content-hash cache (#242) skips any file whose sha AND
package version match the cluster-side manifest ``.hpc/.deploy_state.json``.
The manifest is a claim about what a PAST deploy wrote; it was never checked
against what is actually on disk now. So the moment the combiner disappears
while the manifest survives, every subsequent deploy re-affirms the cache hit
and never re-ships it — silently, and permanently. The manifest outlives the
file it attests by construction: it is rewritten on every deploy that changes
anything (fresh mtime), while a cache-hit file is never rewritten at all (frozen
mtime), which is exactly the discrimination a scratch reaper applies.

**D3 — the torn tree deploy.** ``_deploy_code_tree`` wraps materialize +
``deploy_runtime`` + seal in one ``except Exception`` that degrades to the base
tree with a log line. A tree materialized but not runtime-deployed is left
behind unsealed; the next probe rebuilds it, but any reader that resolved
``REPO_DIR`` to it in between finds no ``.hpc/_hpc_combiner.py``.

What this module provides
-------------------------

ONE definition, shared by the deploy leg and the combine leg, of:

* :data:`COMBINER_REL` — where the artifact lives, relative to a deploy root.
* :func:`local_combiner_sha` — the sha256 of the bytes ``deploy_runtime`` ships
  (byte-identical to the ``_DeployItem.sha`` the deploy cache keys on).
* :func:`combiner_probe_snippet` — a POSIX-sh fragment that prints the
  cluster-side presence+sha on ONE line, cheap enough to FOLD INTO an exec that
  already runs (the deploy prelude's ``mkdir``/``cat`` chain). No new
  round-trip class.
* :func:`combiner_guard_snippet` — the fragment the combine execs prefix onto
  their own command, so a missing artifact exits :data:`COMBINER_ABSENT_RC`
  with a machine-recognisable sentinel INSTEAD of running a combiner that
  isn't there.
* :func:`combiner_absent_in` / :func:`split_combiner_probe` — the readers.

Stdlib-only and import-light on purpose: the deploy leg (``infra.transport``)
and the combine leg (``ops.aggregate``) both import it, and neither may pull in
pydantic to ask whether a file exists.

NOT shipped to the cluster. ``combiner.py`` stays self-contained (it is the
thing being deployed); this module only reads its bytes.
"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "COMBINER_REL",
    "COMBINER_ABSENT_RC",
    "COMBINER_ABSENT_SENTINEL",
    "COMBINER_PROBE_PREFIX",
    "CombinerProbe",
    "combiner_absent_in",
    "combiner_guard_snippet",
    "combiner_probe_snippet",
    "combiner_source_path",
    "local_combiner_sha",
    "redeploy_command",
    "split_combiner_probe",
]

#: Where ``deploy_runtime`` places the combiner, relative to the deploy root
#: (``remote_path`` for the base tree, the tree path for a §10.S4 code tree).
#:
# MIRROR: hpc_agent/infra/transport/_deploy_items.py::_build_deploy_items
# (the ``add_file(pkg_dir/…/combiner.py, ".hpc/_hpc_combiner.py")`` mint site)
# pinned-by test_local_sha_matches_the_deploy_item_sha
COMBINER_REL: Final[str] = ".hpc/_hpc_combiner.py"

#: Exit code the guard uses when the artifact is absent. 78 is ``EX_CONFIG``
#: from ``sysexits.h`` — "configuration error", i.e. the machine is fine and
#: the deployment is wrong. Deliberately NOT 1 (which the combiner itself uses
#: for its own refusals) and NOT 127 (which is the bare-login-python class the
#: activation seam already owns), so the three stay distinguishable in a log.
COMBINER_ABSENT_RC: Final[int] = 78

#: Printed to stderr by the guard. Positive evidence of ABSENCE — the reader
#: never infers absence from a generic "No such file or directory", which the
#: shell also emits for a missing interpreter, a missing ``cd`` target, and a
#: missing sidecar.
COMBINER_ABSENT_SENTINEL: Final[str] = "__HPC_COMBINER_ABSENT__"

#: Leads the presence+sha probe line (:func:`combiner_probe_snippet`).
COMBINER_PROBE_PREFIX: Final[str] = "__HPC_COMBINER_SHA__"

#: What the probe prints for the sha when the file is not there at all.
_ABSENT_TOKEN: Final[str] = "absent"

#: What the probe prints when the file IS there but no sha tool on the login
#: node could hash it. Presence is still positive evidence; the sha is UNKNOWN
#: and must never be read as "matches" or as "differs".
_UNKNOWN_TOKEN: Final[str] = "unknown"


def combiner_source_path() -> Path:
    """Absolute path to the packaged combiner source ``deploy_runtime`` ships.

    The SAME file :func:`~hpc_agent.infra.transport._deploy_items._build_deploy_items`
    reads for its ``.hpc/_hpc_combiner.py`` deploy item — resolved here from
    this module's own location so the two cannot drift apart silently.
    """
    return Path(__file__).with_name("combiner.py")


def local_combiner_sha() -> str:
    """sha256 of the combiner bytes a deploy would place on the cluster.

    Byte-identical to the ``_DeployItem.sha`` the deploy cache records for
    ``.hpc/_hpc_combiner.py``, so a cluster-side sha that differs from this is
    positive evidence of a STALE deploy (not merely an unverified one).

    Not cached: the file is ~30 KB and every caller here is already paying for
    an SSH round-trip. A cache would only add a staleness question during
    development, where the file is edited between calls.
    """
    return hashlib.sha256(combiner_source_path().read_bytes()).hexdigest()


@dataclass(frozen=True)
class CombinerProbe:
    """What one cluster-side presence+sha read found.

    *sha* is ``None`` when the artifact is absent OR when it is present but no
    hashing tool was available. The two are NOT the same verdict, so they are
    kept apart by *present*: a present-but-unhashable artifact is
    :attr:`state` ``"present"`` with ``sha_known=False`` — the deploy leg must
    not re-ship on an UNKNOWN sha (that would defeat the cache on every host
    without ``sha256sum``), and must always re-ship on an ABSENT one.
    """

    present: bool
    sha: str | None
    expected_sha: str

    @property
    def sha_known(self) -> bool:
        """True when the cluster-side sha was actually read."""
        return self.sha is not None

    @property
    def matches(self) -> bool:
        """True only on positive evidence the deployed bytes are current."""
        return self.present and self.sha is not None and self.sha == self.expected_sha

    @property
    def stale(self) -> bool:
        """True only on positive evidence the deployed bytes are WRONG."""
        return self.present and self.sha is not None and self.sha != self.expected_sha

    @property
    def state(self) -> str:
        """``"absent"`` | ``"stale"`` | ``"present"`` — for messages and tests."""
        if not self.present:
            return "absent"
        if self.stale:
            return "stale"
        return "present"

    @property
    def needs_redeploy(self) -> bool:
        """Whether a deploy must re-ship the artifact regardless of its cache.

        Absent or provably stale. An unhashable-but-present artifact is NOT
        included: absence of a sha is absence of evidence, and forcing a
        re-ship on it would turn every ``sha256sum``-less login node into a
        permanent cache miss.
        """
        return not self.present or self.stale


def combiner_probe_snippet(*, root: str | None = None) -> str:
    """POSIX-sh fragment printing ``<prefix> <sha|absent|unknown>`` on one line.

    Designed to be FOLDED INTO an exec that already runs — the deploy
    prelude's ``mkdir``/``find``/``cat`` chain — so verifying the artifact
    costs zero additional round-trips. It writes exactly one line to stdout and
    nothing to stderr, and always exits 0, so it can be chained with ``;``
    ahead of a command whose own stdout the caller parses (see
    :func:`split_combiner_probe`, which peels the line back off).

    *root* is the deploy root the artifact is relative to; ``None`` means "the
    current working directory", for callers that already ``cd``'d there.

    Three hashing tools are tried in order because login nodes disagree:
    ``sha256sum`` (GNU coreutils, the common case), ``shasum -a 256`` (macOS /
    perl-based hosts), ``openssl dgst -sha256`` (last resort). Their output
    formats do NOT agree on field position — the coreutils pair print
    ``<hash>  <file>`` while openssl prints ``SHA256(<file>)= <hash>`` — so the
    extractor picks the first field that IS a 64-hex-digit token rather than
    counting columns. (Field-counting is how the first cut of this shipped a
    probe that returned the FILENAME as the sha, which the reader would then
    have compared against the real digest and reported "stale" on every host.)

    When no hashing tool exists the file is still reported PRESENT with an
    ``unknown`` sha — presence is the load-bearing half.

    The line is emitted with a LEADING newline. When the snippet is folded
    *after* a payload command, that payload's last line may have no trailing
    newline of its own (``json.dump`` does not write one, and a run sidecar is
    ``cat``-ed verbatim), which would otherwise glue the probe onto the end of
    the payload's final line — where a line-oriented reader cannot see it and a
    JSON parser chokes on it. The leading newline costs one byte and one blank
    line, which every consumer here already tolerates.
    :func:`split_combiner_probe` ALSO handles the glued form, so an older
    cluster-side snippet stays readable.
    """
    path = COMBINER_REL if root is None else f"{root.rstrip('/')}/{COMBINER_REL}"
    path_q = shlex.quote(path)
    # Single-quoted awk program; no shell interpolation inside it.
    awk_prog = "{for(i=1;i<=NF;i++) if ($i ~ /^[0-9a-f]{64}$/) {print $i; exit}}"
    return (
        f"if [ -f {path_q} ]; then "
        f"__hpc_cs=$( {{ sha256sum {path_q} 2>/dev/null "
        f"|| shasum -a 256 {path_q} 2>/dev/null "
        f"|| openssl dgst -sha256 {path_q} 2>/dev/null; }} "
        f"| awk '{awk_prog}' | head -n 1 ); "
        f"printf '\\n%s %s\\n' {shlex.quote(COMBINER_PROBE_PREFIX)} "
        f'"${{__hpc_cs:-{_UNKNOWN_TOKEN}}}"; '
        f"else printf '\\n%s %s\\n' {shlex.quote(COMBINER_PROBE_PREFIX)} "
        f"{shlex.quote(_ABSENT_TOKEN)}; fi; "
    )


def combiner_guard_snippet(*, root: str | None = None) -> str:
    """POSIX-sh fragment that REFUSES rather than run an absent combiner.

    Prefixed onto every exec that invokes ``python3 .hpc/_hpc_combiner.py``
    (per-wave, fused batch, ``--final``). Costs one ``test -f`` inside an ssh
    the caller was making anyway — no new round-trip class — and converts the
    silent late failure into an immediate, named one:
    :data:`COMBINER_ABSENT_SENTINEL` on stderr and exit
    :data:`COMBINER_ABSENT_RC`.

    Ends in `` && `` — NOT ``; `` — so it slots into an existing
    ``cd <root> && <activation>python3 …`` chain without breaking the ``&&``
    that chain already relies on. A ``; `` here would silently DOWNGRADE the
    guarantee it is meant to strengthen: a failed ``cd`` (bad ``remote_path``)
    would stop gating the combiner invocation, which would then run in the
    login shell's home directory. The ``if … fi`` compound exits 0 when the
    artifact is present, so the ``&&`` is transparent on the happy path.

    Place it AFTER the ``cd`` when *root* is ``None`` (the relative path
    resolves against the deploy root) and BEFORE any env activation — an absent
    artifact is not worth a ``conda activate``.
    """
    path = COMBINER_REL if root is None else f"{root.rstrip('/')}/{COMBINER_REL}"
    path_q = shlex.quote(path)
    return (
        f"if [ ! -f {path_q} ]; then "
        f"printf '%s %s\\n' {shlex.quote(COMBINER_ABSENT_SENTINEL)} {path_q} >&2; "
        f"exit {COMBINER_ABSENT_RC}; fi && "
    )


def split_combiner_probe(stdout: str) -> tuple[CombinerProbe | None, str]:
    """Peel the probe line off *stdout*; return ``(probe, remaining_stdout)``.

    Returns ``(None, stdout)`` unchanged when no probe line is present — an
    older cluster-side prelude, a severed channel, or a caller that did not
    fold the snippet in. The caller then has NO evidence either way and must
    fall back to its prior behaviour rather than infer absence: this is the
    same fail-open discipline ``_parse_remote_manifest`` applies to a missing
    manifest.

    The remaining stdout is returned verbatim (minus the probe line) so a
    downstream parser — ``json.loads`` on the deploy manifest, the batch
    sentinel scanner — sees exactly what it would have seen before.

    Matching is by SUBSTRING, not ``startswith``, and the text before the
    prefix on that line is KEPT. That handles the glued case where the payload
    the snippet was folded after ended without a trailing newline — a run
    sidecar is ``cat``-ed verbatim and ``json.dump`` writes none, so
    ``{...}__HPC_COMBINER_SHA__ absent`` arrives as ONE line. A
    ``startswith`` reader misses the probe there AND hands the glued text to
    ``json.loads``, which is exactly how this was first found. The emitter now
    prefixes a newline too; this stays tolerant so a cluster still running the
    older snippet is read correctly rather than silently mis-parsed.
    """
    if COMBINER_PROBE_PREFIX not in (stdout or ""):
        return None, stdout
    kept: list[str] = []
    probe: CombinerProbe | None = None
    expected = local_combiner_sha()
    for line in stdout.splitlines():
        idx = line.find(COMBINER_PROBE_PREFIX) if probe is None else -1
        if idx < 0:
            kept.append(line)
            continue
        head = line[:idx]
        if head.strip():
            kept.append(head)
        token = line[idx + len(COMBINER_PROBE_PREFIX) :].strip().split()
        value = token[0] if token else ""
        if value == _ABSENT_TOKEN:
            probe = CombinerProbe(present=False, sha=None, expected_sha=expected)
        elif value in ("", _UNKNOWN_TOKEN):
            probe = CombinerProbe(present=True, sha=None, expected_sha=expected)
        else:
            probe = CombinerProbe(present=True, sha=value, expected_sha=expected)
    return probe, "\n".join(kept)


def combiner_absent_in(stdout: str | None, stderr: str | None) -> bool:
    """True iff a combine exec's output carries the guard's absence sentinel.

    Both streams are scanned because the fused batch runner folds stderr into
    stdout (``2>&1``) while the per-wave and ``--final`` runners keep them
    apart. Sentinel-keyed, never text-keyed: a user's own combiner printing
    "no such file" must not be read as a deploy dropout.
    """
    return COMBINER_ABSENT_SENTINEL in ((stdout or "") + (stderr or ""))


def redeploy_command(
    *,
    experiment_dir: str | None = None,
    run_id: str | None = None,
) -> str:
    """The literal command that RESTORES the deployed combiner.

    This is the command the 2026-07-30 incident's human substituted with a hand
    ``scp`` at 22:00. It re-ships every ``deploy_runtime`` artifact to the run's
    own ``remote_path`` with the content-hash cache bypassed, and submits
    nothing — so it is safe to run against a run that is mid-flight, finished,
    or being aggregated.

    Unresolved values are emitted as ``<placeholder>`` tokens, which is exactly
    what :func:`hpc_agent.recovery.registry.remediation_for` substitutes at
    emit time.
    """
    exp = experiment_dir or "<experiment_dir>"
    rid = run_id or "<run_id>"
    return f"hpc-agent redeploy-runtime --experiment-dir {exp} --run-id {rid}"

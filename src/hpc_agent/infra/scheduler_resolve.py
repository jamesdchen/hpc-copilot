"""Deterministic scheduler detection for cluster onboarding.

Probes a login node and resolves which curated scheduler **family** it
runs — ``slurm`` / ``sge`` / ``pbspro`` / ``torque`` — purely by
measurement: which submit binary exists, and (for the ambiguous ``qsub``
case) which marker tools + version banner are present. No LLM, no live
job: the answer is ground truth from the cluster, and a wrong call fails
loud at first submit (the engine's job-id parse guard).

This module is intentionally *detection only*. A scheduler outside the
curated families is handled by pinning a ``scheduler_profile`` in
clusters.yaml (data) or adding a curated family (code) — it is not
auto-authored at runtime, because a synthesised profile has no fast,
reliable verifier and the curated families already cover the common
ground. ``seed_profile_for_probe`` returns the nearest curated golden
profile for a detected family (used by the deterministic resolver).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE, SchedulerProfile

if TYPE_CHECKING:
    import subprocess

# Families the engine can register a profile under. ``probe_cluster`` only
# ever resolves ``family`` to one of these (or None). Mirrors the frozen
# set the engine registers under so a probe can never name something the
# rest of the framework can't route.
_PROBE_FAMILIES = frozenset({"slurm", "sge", "pbspro", "torque"})

# A callable that runs one shell command on the cluster login node and
# returns its CompletedProcess (the same shape ``infra.remote.ssh_run``
# yields). Injected so tests can stub the cluster.
SshRun = Callable[[str], "subprocess.CompletedProcess[str]"]


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

# Submit binary -> family. ``sbatch`` is unambiguous (slurm); ``qsub`` is
# AMBIGUOUS — it ships with SGE *and* both PBS forks (PBSPro/OpenPBS and
# TORQUE), so it is disambiguated separately (see ``_disambiguate_qsub``).
# ``bsub`` -> lsf is recorded but lsf is not an engine family, so it never
# becomes ``ProbeResult.family``. Order matters only for the (rare) cluster
# that ships more than one; sbatch wins because a SLURM site sometimes also
# has a legacy qsub wrapper, not the reverse.
_BIN_FAMILY = (("sbatch", "slurm"), ("qsub", "sge"), ("bsub", "lsf"))

# Marker binaries that disambiguate a qsub host. ``command -v`` for each is
# run only when ``qsub`` is present; the results feed ``_disambiguate_qsub``.
_QSUB_MARKERS = ("pbsnodes", "qmgr", "qconf", "momctl")


def _pbs_fork_from_version(banner: str) -> str:
    """Split PBS-family into ``pbspro`` vs ``torque`` from a version banner.

    Deterministic, case-insensitive parse of a ``qstat --version`` /
    ``qsub --version`` banner:

    * ``PBSPro`` / ``OpenPBS`` / a ``pbs_version`` token / a version whose
      major is >= 13 (PBSPro/OpenPBS adopted year-based 13.x+ numbering)
      -> ``pbspro``.
    * a ``2.x``-``6.x`` version (TORQUE's range) -> ``torque``.

    The ambiguous-but-PBS case (no decisive marker) defaults to ``pbspro``,
    the larger deployment share. Callers reach this only once the host is
    already known to be PBS-family (a PBS marker binary was found).
    """
    text = (banner or "").lower()
    if "pbspro" in text or "openpbs" in text or "pbs_version" in text:
        return "pbspro"
    if "momctl" in text:  # TORQUE-only tool sometimes named in its banner
        return "torque"
    # Find the first dotted version number and branch on its major component.
    m = re.search(r"\b(\d+)\.\d+", text)
    if m:
        major = int(m.group(1))
        if major >= 13:
            return "pbspro"
        if 2 <= major <= 6:
            return "torque"
    # Ambiguous PBS host: default to the larger-share fork.
    return "pbspro"


def _disambiguate_qsub(markers: dict[str, bool], version_banner: str) -> str:
    """Resolve a ``qsub`` host to ``sge`` / ``pbspro`` / ``torque``.

    Deterministic decision tree (authoritative rules):

    1. ``pbsnodes`` or ``qmgr`` present -> PBS-family (NOT sge). Split the
       fork via the version banner (``_pbs_fork_from_version``); ``momctl``
       present forces ``torque`` even if the banner is silent.
    2. ``qconf`` present (and no PBS marker) -> ``sge``.
    3. Neither family marker present -> fall back to ``sge`` (the historical
       qsub default; it has a golden seed and preserves prior behaviour for
       a bare-qsub host that exposes none of the marker tools).
    """
    pbs_family = markers.get("pbsnodes") or markers.get("qmgr")
    if pbs_family:
        if markers.get("momctl"):
            return "torque"
        return _pbs_fork_from_version(version_banner)
    if markers.get("qconf"):
        return "sge"
    return "sge"


@dataclass(frozen=True)
class ProbeResult:
    """What the login node told us about its scheduler."""

    binaries: dict[str, str] = field(default_factory=dict)  # bin -> path
    family: str | None = None  # inferred known family, else None
    versions: dict[str, str] = field(default_factory=dict)  # bin -> version banner
    raw: dict[str, str] = field(default_factory=dict)  # cmd -> stdout (diagnostics)
    markers: dict[str, bool] = field(default_factory=dict)  # disambig marker -> present


def probe_cluster(ssh_run: SshRun) -> ProbeResult:
    """Detect scheduler binaries + versions on the login node.

    Pure I/O via *ssh_run*; the parsing is deterministic and tested. The
    resolved ``family`` is one of ``{slurm, sge, pbspro, torque}`` or None.

    ``qsub`` is ambiguous (SGE and both PBS forks ship it), so when it is
    present the probe additionally runs ``command -v`` for a set of marker
    tools (pbsnodes/qmgr/qconf/momctl) and reads a ``qstat --version`` /
    ``qsub --version`` banner, then disambiguates deterministically via
    ``_disambiguate_qsub``.
    """

    def _run(cmd: str) -> str:
        try:
            cp = ssh_run(cmd)
        except Exception:  # noqa: BLE001 — a probe failure is just "absent"
            return ""
        return (getattr(cp, "stdout", "") or "").strip()

    binaries: dict[str, str] = {}
    versions: dict[str, str] = {}
    raw: dict[str, str] = {}
    markers: dict[str, bool] = {}
    for bin_name, _fam in _BIN_FAMILY:
        path = _run(f"command -v {bin_name}")
        raw[f"command -v {bin_name}"] = path
        if path:
            binaries[bin_name] = path.splitlines()[0].strip()

    # Version banners (only for binaries that exist) — diagnostics + fork split.
    _version_cmd = {"sbatch": "sbatch --version", "qsub": "qsub -help", "bsub": "bsub -V"}
    for bin_name in binaries:
        cmd = _version_cmd.get(bin_name)
        if cmd:
            out = _run(cmd)
            raw[cmd] = out
            if out:
                versions[bin_name] = out.splitlines()[0].strip()

    # qsub disambiguation: probe marker tools + a richer version banner so we
    # can tell SGE from PBSPro from TORQUE.
    pbs_banner = ""
    if "qsub" in binaries:
        for marker in _QSUB_MARKERS:
            present = bool(_run(f"command -v {marker}"))
            raw[f"command -v {marker}"] = "/usr/bin/" + marker if present else ""
            markers[marker] = present
        for vcmd in ("qstat --version", "qsub --version"):
            out = _run(vcmd)
            raw[vcmd] = out
            if out and not pbs_banner:
                pbs_banner = out

    family: str | None = None
    if "sbatch" in binaries:
        family = "slurm"
    elif "qsub" in binaries:
        family = _disambiguate_qsub(markers, pbs_banner)
    if family is not None and family not in _PROBE_FAMILIES:  # pragma: no cover — defensive
        family = None
    return ProbeResult(
        binaries=binaries, family=family, versions=versions, raw=raw, markers=markers
    )


# ---------------------------------------------------------------------------
# Seed from nearest curated golden profile
# ---------------------------------------------------------------------------


def seed_profile_for_probe(probe: ProbeResult) -> SchedulerProfile:
    """Return the curated golden profile for the detected family.

    Raises :class:`~hpc_agent.errors.SpecInvalid` when the probe found no
    recognisable submit binary — there is nothing to map to, and guessing a
    family blind would be worse than failing loudly.
    """
    if probe.family == "slurm":
        return SLURM_PROFILE
    if probe.family == "sge":
        return SGE_PROFILE
    if probe.family == "pbspro":
        from hpc_agent.infra.backends.profile import PBSPRO_PROFILE

        return PBSPRO_PROFILE
    if probe.family == "torque":
        from hpc_agent.infra.backends.profile import TORQUE_PROFILE

        return TORQUE_PROFILE
    raise errors.SpecInvalid(
        "cluster probe found no sbatch/qsub-family scheduler "
        f"(binaries={sorted(probe.binaries)}); cannot map to a curated profile. "
        "If this is a known scheduler, pin a 'scheduler_profile' in clusters.yaml."
    )

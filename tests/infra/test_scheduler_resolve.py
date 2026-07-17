"""Unit tests for deterministic scheduler detection (probe + qsub
disambiguation + seed-from-curated-golden), driven through a stubbed cluster.

The probe fires ONE ssh round-trip (a sentinel-framed batch script); ``BatchSsh``
models the login node's response to that script. ``_serial_probe`` reimplements
the pre-batch 6–12-dial algorithm and is used purely as a byte-equivalence oracle:
for the same per-command remote outputs, the batched ``probe_cluster`` must derive
an identical ``ProbeResult`` while collapsing N dials to 1.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra import scheduler_resolve as sr
from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE


def _cp(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _one(cmd: str, *, present: set[str], version_banner: str) -> str:
    """Per-command remote stdout for a host with *present* binaries.

    The single source of truth for both the batched fake (``BatchSsh``) and the
    serial oracle (``_serial_probe``), so any ``ProbeResult`` difference between
    them is a real batching defect, not a fixture mismatch.
    """
    if cmd.startswith("command -v "):
        b = cmd.split()[-1]
        return f"/usr/bin/{b}" if b in present else ""
    if cmd in ("qstat --version", "qsub --version"):
        return version_banner
    if "-help" in cmd or "--version" in cmd or cmd.endswith(" -V"):
        # A submit binary's --version/-help/-V banner: only meaningful when the
        # binary exists. Matches the pre-batch fakes' generic banner.
        b = cmd.split()[0]
        return "scheduler 1.2.3" if b in present else ""
    return ""


class BatchSsh:
    """Fake ``ssh_run`` modelling the ONE-round-trip probe script.

    Given a *present* binary set + a qsub-family *version_banner*, it renders the
    section-framed, ack-suffixed stdout a real login node returns for the batch.
    Records every call so tests can assert the dial count (exactly 1)."""

    def __init__(self, *, present=(), version_banner=""):
        self.present = set(present)
        self.version_banner = version_banner
        self.calls: list[str] = []

    def __call__(self, script: str):
        self.calls.append(script)
        lines: list[str] = []
        for cmd in sr._PROBE_COMMANDS:
            lines.append(f"{sr._SECTION_PREFIX}{cmd}{sr._SECTION_SUFFIX}")
            out = _one(cmd, present=self.present, version_banner=self.version_banner)
            if out:
                lines.append(out)
        lines.append(f"{sr._PROBE_ACK_PREFIX}0")
        return _cp("\n".join(lines) + "\n")


def _serial_probe(present, version_banner="") -> tuple[sr.ProbeResult, int]:
    """Reference reimplementation of the PRE-batch 6–12-dial algorithm.

    Byte-for-byte the derivation the old ``probe_cluster`` ran, but counting each
    ``_run`` as one dial. Returns ``(ProbeResult, dial_count)`` — the oracle the
    batched implementation is compared against.
    """
    present = set(present)
    dials = 0

    def _run(cmd: str) -> str:
        nonlocal dials
        dials += 1
        return _one(cmd, present=present, version_banner=version_banner).strip()

    binaries: dict[str, str] = {}
    versions: dict[str, str] = {}
    raw: dict[str, str] = {}
    markers: dict[str, bool] = {}
    for bin_name, _fam in sr._BIN_FAMILY:
        path = _run(f"command -v {bin_name}")
        raw[f"command -v {bin_name}"] = path
        if path:
            binaries[bin_name] = path.splitlines()[0].strip()
    for bin_name in binaries:
        cmd = sr._VERSION_CMD.get(bin_name)
        if cmd:
            out = _run(cmd)
            raw[cmd] = out
            if out:
                versions[bin_name] = out.splitlines()[0].strip()
    pbs_banner = ""
    if "qsub" in binaries:
        for marker in sr._QSUB_MARKERS:
            hit = bool(_run(f"command -v {marker}"))
            raw[f"command -v {marker}"] = "/usr/bin/" + marker if hit else ""
            markers[marker] = hit
        for vcmd in sr._QSUB_VERSION_CMDS:
            out = _run(vcmd)
            raw[vcmd] = out
            if out and not pbs_banner:
                pbs_banner = out
    family: str | None = None
    if "sbatch" in binaries:
        family = "slurm"
    elif "qsub" in binaries:
        family = sr._disambiguate_qsub(markers, pbs_banner)
    return (
        sr.ProbeResult(
            binaries=binaries, family=family, versions=versions, raw=raw, markers=markers
        ),
        dials,
    )


# --- probe ------------------------------------------------------------------


def test_probe_detects_slurm():
    p = sr.probe_cluster(BatchSsh(present=("sbatch",)))
    assert p.family == "slurm"
    assert "sbatch" in p.binaries


def test_probe_detects_sge():
    p = sr.probe_cluster(BatchSsh(present=("qsub",)))
    assert p.family == "sge"


def test_probe_unknown_when_no_known_binary():
    p = sr.probe_cluster(BatchSsh(present=("bsub",)))
    assert p.family is None  # lsf is not an engine family


# --- single-dial collapse (AUDIT rank 5 / U9) -------------------------------


@pytest.mark.parametrize(
    "present, banner",
    [
        (("sbatch",), ""),  # slurm: serial = 4 dials
        (("qsub", "pbsnodes", "qmgr"), "pbs_version = 2020.1"),  # qsub host: 10 dials
        ((), ""),  # bare host: serial = 3 dials
    ],
)
def test_probe_collapses_to_one_dial(present, banner):
    # Serial oracle fires many cold dials...
    _ref, serial_dials = _serial_probe(present, banner)
    assert serial_dials > 1, "oracle must model the multi-dial baseline"
    # ...the batched probe fires exactly ONE.
    ssh = BatchSsh(present=present, version_banner=banner)
    sr.probe_cluster(ssh)
    assert len(ssh.calls) == 1


def test_probe_slurm_baseline_is_four_dials():
    # Pins the concrete "N before" number the collapse removes.
    _ref, dials = _serial_probe(("sbatch",))
    assert dials == 4  # command -v {sbatch,qsub,bsub} + sbatch --version


def test_probe_qsub_baseline_is_ten_dials():
    _ref, dials = _serial_probe(("qsub", "pbsnodes"), "Version 6.1.2")
    # 3 command -v + qsub -help + 4 markers + 2 qsub-family banners.
    assert dials == 10


# --- byte-equivalence: batched == serial for the same remote outputs --------


@pytest.mark.parametrize(
    "present, banner",
    [
        (("sbatch",), ""),
        (("qsub",), ""),
        (("bsub",), ""),
        ((), ""),
        (("qsub", "pbsnodes", "qmgr"), "pbs_version = 2020.1"),
        (("qsub", "pbsnodes"), "Version 6.1.2"),
        (("qsub", "qmgr", "momctl"), ""),
        (("qsub", "qconf"), ""),
        (("qsub", "qmgr"), "qstat 9.9"),
        (("sbatch", "qsub", "bsub", "qconf"), "whatever 3.2"),
    ],
)
def test_batched_probe_is_byte_equivalent_to_serial(present, banner):
    ref, _dials = _serial_probe(present, banner)
    got = sr.probe_cluster(BatchSsh(present=present, version_banner=banner))
    assert got == ref  # frozen dataclass eq compares every field
    assert got.binaries == ref.binaries
    assert got.versions == ref.versions
    assert got.raw == ref.raw
    assert got.markers == ref.markers
    assert got.family == ref.family


# --- severed / truncated channel -> UNKNOWN (never a default verdict) -------


def test_probe_raises_when_ack_absent():
    # An rc-0 read that is missing the positive-evidence ack = a truncated /
    # severed batch. It must RAISE, not settle a bogus "no scheduler" verdict.
    def _truncated(_script: str):
        return _cp(f"{sr._SECTION_PREFIX}command -v sbatch{sr._SECTION_SUFFIX}\n")

    with pytest.raises(errors.RemoteCommandFailed, match="ack"):
        sr.probe_cluster(_truncated)


def test_probe_raises_when_dial_itself_raises():
    def _boom(_script: str):
        raise OSError("connection reset")

    with pytest.raises(errors.RemoteCommandFailed, match="never returned"):
        sr.probe_cluster(_boom)


def test_probe_clean_empty_host_is_none_not_raise():
    # A CLEANLY-completed probe (ack present) that found no submit binary is
    # DATA (family=None), not an error — absence must not be conflated with a
    # severed channel.
    p = sr.probe_cluster(BatchSsh(present=()))
    assert p.family is None
    assert p.binaries == {}


# --- qsub disambiguation (sge / pbspro / torque) ----------------------------


def test_pbsfork_helper_recognizes_pbspro_banner():
    assert sr._pbs_fork_from_version("pbs_version = 2021.1.2") == "pbspro"
    assert sr._pbs_fork_from_version("PBSPro_19.2.4") == "pbspro"
    assert sr._pbs_fork_from_version("OpenPBS 20.0.0") == "pbspro"
    assert sr._pbs_fork_from_version("version: 14.1.0") == "pbspro"  # major >= 13


def test_pbsfork_helper_recognizes_torque_banner():
    assert sr._pbs_fork_from_version("version: 6.1.3") == "torque"
    assert sr._pbs_fork_from_version("pbs_version = 4.2.10") == "pbspro"  # token wins
    assert sr._pbs_fork_from_version("Version 2.5.13") == "torque"


def test_pbsfork_helper_defaults_ambiguous_to_pbspro():
    assert sr._pbs_fork_from_version("") == "pbspro"
    assert sr._pbs_fork_from_version("no version here") == "pbspro"


def test_probe_qsub_with_pbsnodes_pbspro_banner():
    ssh = BatchSsh(present=("qsub", "pbsnodes", "qmgr"), version_banner="pbs_version = 2020.1")
    p = sr.probe_cluster(ssh)
    assert p.family == "pbspro"
    assert p.markers["pbsnodes"] is True
    assert p.markers["qconf"] is False


def test_probe_qsub_with_pbsnodes_torque_banner():
    ssh = BatchSsh(present=("qsub", "pbsnodes"), version_banner="Version 6.1.2")
    p = sr.probe_cluster(ssh)
    assert p.family == "torque"


def test_probe_qsub_torque_via_momctl_marker():
    # momctl present forces torque even if the banner is silent/ambiguous.
    ssh = BatchSsh(present=("qsub", "qmgr", "momctl"), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "torque"


def test_probe_qsub_with_qconf_is_sge():
    ssh = BatchSsh(present=("qsub", "qconf"), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "sge"


def test_probe_qsub_ambiguous_pbs_defaults_pbspro():
    # PBS marker present but a banner with no decisive fork signal.
    ssh = BatchSsh(present=("qsub", "qmgr"), version_banner="qstat 9.9")
    p = sr.probe_cluster(ssh)
    assert p.family == "pbspro"  # 9.9 is neither >=13 nor 2-6 → default pbspro


def test_probe_bare_qsub_no_markers_falls_back_to_sge():
    # No PBS markers, no qconf: preserve the historical qsub→sge default.
    ssh = BatchSsh(present=("qsub",), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "sge"


def test_probe_slurm_unaffected_by_disambiguation():
    ssh = BatchSsh(present=("sbatch",))
    p = sr.probe_cluster(ssh)
    assert p.family == "slurm"
    assert p.markers == {}  # markers only recorded when qsub is present


# --- seed from curated golden -----------------------------------------------


def test_seed_slurm_and_sge():
    assert sr.seed_profile_for_probe(sr.ProbeResult(family="slurm")) is SLURM_PROFILE
    assert sr.seed_profile_for_probe(sr.ProbeResult(family="sge")) is SGE_PROFILE


def test_seed_pbspro_and_torque():
    from hpc_agent.infra.backends.profile import PBSPRO_PROFILE, TORQUE_PROFILE

    assert sr.seed_profile_for_probe(sr.ProbeResult(family="pbspro")) is PBSPRO_PROFILE
    assert sr.seed_profile_for_probe(sr.ProbeResult(family="torque")) is TORQUE_PROFILE


def test_seed_raises_without_family():
    with pytest.raises(errors.SpecInvalid, match="no sbatch/qsub"):
        sr.seed_profile_for_probe(sr.ProbeResult(family=None))

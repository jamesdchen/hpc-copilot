"""Unit tests for deterministic scheduler detection (probe + qsub
disambiguation + seed-from-curated-golden), driven through a stubbed cluster."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra import scheduler_resolve as sr
from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE


def _cp(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class FakeSsh:
    """Command-dispatching fake ``ssh_run`` keyed on which binaries exist."""

    def __init__(self, *, present=()):
        self.present = set(present)  # binaries that `command -v` finds
        self.calls: list[str] = []

    def __call__(self, cmd: str):
        self.calls.append(cmd)
        if cmd.startswith("command -v "):
            b = cmd.split()[-1]
            return _cp(f"/usr/bin/{b}" if b in self.present else "")
        if "--version" in cmd or "-help" in cmd or cmd.endswith("-V"):
            return _cp("scheduler 1.2.3")
        return _cp("")


# --- probe -----------------------------------------------------------------


def test_probe_detects_slurm():
    p = sr.probe_cluster(FakeSsh(present=("sbatch",)))
    assert p.family == "slurm"
    assert "sbatch" in p.binaries


def test_probe_detects_sge():
    p = sr.probe_cluster(FakeSsh(present=("qsub",)))
    assert p.family == "sge"


def test_probe_unknown_when_no_known_binary():
    p = sr.probe_cluster(FakeSsh(present=("bsub",)))
    assert p.family is None  # lsf is not an engine family


# --- qsub disambiguation (sge / pbspro / torque) ---------------------------


class DisambigSsh:
    """Fake ``ssh_run`` modelling ``command -v`` for an arbitrary marker set
    plus a configurable ``qstat --version`` / ``qsub --version`` banner, so the
    sge-vs-pbspro-vs-torque decision tree is exercised without a cluster."""

    def __init__(self, *, present=(), version_banner=""):
        self.present = set(present)
        self.version_banner = version_banner
        self.calls: list[str] = []

    def __call__(self, cmd: str):
        self.calls.append(cmd)
        if cmd.startswith("command -v "):
            b = cmd.split()[-1]
            return _cp(f"/usr/bin/{b}" if b in self.present else "")
        if cmd in ("qstat --version", "qsub --version"):
            return _cp(self.version_banner)
        if "-help" in cmd or "--version" in cmd:
            return _cp("usage: qsub ...")
        return _cp("")


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
    ssh = DisambigSsh(present=("qsub", "pbsnodes", "qmgr"), version_banner="pbs_version = 2020.1")
    p = sr.probe_cluster(ssh)
    assert p.family == "pbspro"
    assert p.markers["pbsnodes"] is True
    assert p.markers["qconf"] is False


def test_probe_qsub_with_pbsnodes_torque_banner():
    ssh = DisambigSsh(present=("qsub", "pbsnodes"), version_banner="Version 6.1.2")
    p = sr.probe_cluster(ssh)
    assert p.family == "torque"


def test_probe_qsub_torque_via_momctl_marker():
    # momctl present forces torque even if the banner is silent/ambiguous.
    ssh = DisambigSsh(present=("qsub", "qmgr", "momctl"), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "torque"


def test_probe_qsub_with_qconf_is_sge():
    ssh = DisambigSsh(present=("qsub", "qconf"), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "sge"


def test_probe_qsub_ambiguous_pbs_defaults_pbspro():
    # PBS marker present but a banner with no decisive fork signal.
    ssh = DisambigSsh(present=("qsub", "qmgr"), version_banner="qstat 9.9")
    p = sr.probe_cluster(ssh)
    assert p.family == "pbspro"  # 9.9 is neither >=13 nor 2-6 → default pbspro


def test_probe_bare_qsub_no_markers_falls_back_to_sge():
    # No PBS markers, no qconf: preserve the historical qsub→sge default.
    ssh = DisambigSsh(present=("qsub",), version_banner="")
    p = sr.probe_cluster(ssh)
    assert p.family == "sge"


def test_probe_slurm_unaffected_by_disambiguation():
    ssh = DisambigSsh(present=("sbatch",))
    p = sr.probe_cluster(ssh)
    assert p.family == "slurm"
    assert p.markers == {}  # markers only probed when qsub is present


# --- seed from curated golden ----------------------------------------------


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

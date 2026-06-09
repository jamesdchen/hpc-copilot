"""Scheduler-as-data: the :class:`SchedulerProfile`.

A profile is the *data* that distinguishes one scheduler from another —
the submit binary, the job-id parse anchor, the template extension, the
error-state vocabulary, and the runtime array-job script bodies. The
engine (:class:`hpc_agent.infra.backends._engine.ProfileBackend`)
interprets a profile; it carries no per-scheduler literals of its own.

Two profiles ship as golden constants: :data:`SLURM_PROFILE` and
:data:`SGE_PROFILE`. They reproduce the historical hard-coded
``SlurmBackend`` / ``SGEBackend`` behaviour exactly (the equivalence test
suite is the contract). A previously-unknown scheduler is handled by
*resolving* a profile at cluster-setup time — seeded from the nearest
golden profile when the family is recognised, otherwise LLM-authored —
then canary-validated and pinned into the experiment metadata so steady
state is pure deterministic Python.

``family`` selects the structural command-assembly shape the engine
uses (``"slurm"`` flag style vs ``"sge"`` flag style). Everything else
on the profile is swappable data, which is what makes a resolved profile
expressible as a plain dict (see :meth:`SchedulerProfile.from_dict` /
:meth:`SchedulerProfile.to_dict` — the pin wire format).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.backends._scripts import (
    PBSPRO_CPU,
    PBSPRO_GPU,
    PBSPRO_MPI,
    SGE_CPU,
    SGE_GPU,
    SGE_MPI,
    SLURM_CPU,
    SLURM_GPU,
    SLURM_MPI,
    TORQUE_CPU,
    TORQUE_GPU,
    TORQUE_MPI,
)

# Known structural families the engine can assemble commands for. A
# resolved profile MUST pick one of these — it tells the engine which
# flag grammar (sbatch-style vs qsub-style) to emit. A genuinely novel
# grammar needs a new branch in the engine (documented escape hatch);
# everything within a family is pure data on the profile.
#
# ``pbspro`` and ``torque`` are both qsub-family but diverge structurally
# (array flag ``-J`` vs ``-t``, index env ``PBS_ARRAY_INDEX`` vs
# ``PBS_ARRAYID``, ``select=`` vs ``nodes=:ppn=``, finished token ``F`` vs
# ``C``, history ``qstat -x`` vs ``qstat -f``), so they are distinct
# families rather than one — mirroring Nextflow's PbsPro/Pbs split.
KNOWN_FAMILIES = frozenset({"slurm", "sge", "pbspro", "torque"})


@dataclass(frozen=True)
class SchedulerProfile:
    """The data that defines one scheduler.

    Parameters
    ----------
    name:
        Registry label (``"slurm"`` / ``"sge"`` for the golden ones, or a
        cluster-chosen label for a resolved profile). ``get_backend_class``
        looks profiles up by this.
    family:
        Structural command grammar — one of :data:`KNOWN_FAMILIES`.
    submit_bin:
        The submit binary (``"sbatch"`` / ``"qsub"``).
    job_id_regex:
        Pattern whose first capture group is the job id in submit stdout.
        Anchored on a phrase so a digit-bearing warning banner can't poison
        the parse.
    template_ext:
        On-disk / on-remote extension for the rendered array script.
    supports_test_only_eta:
        Whether the backfill planner's ``--test-only`` ETA probe is
        available (SLURM yes, SGE no).
    error_states:
        Raw state tokens classified as ``error`` (consumed by the
        ``slurm``-family exact-match classifier; the ``sge`` family uses a
        substring rule and leaves this empty).
    scripts:
        ``kind -> script body`` for the runtime job (keys ``"cpu"`` /
        ``"gpu"`` for the array shape, ``"mpi"`` for a single multi-rank
        job, #293). Rendered verbatim by :func:`render_script`.
    """

    name: str
    family: str
    submit_bin: str
    job_id_regex: str
    template_ext: str
    supports_test_only_eta: bool
    error_states: frozenset[str] = frozenset()
    scripts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family not in KNOWN_FAMILIES:
            raise errors.SpecInvalid(
                f"SchedulerProfile {self.name!r}: unknown family {self.family!r}; "
                f"the engine can assemble commands for {sorted(KNOWN_FAMILIES)} only. "
                "A novel scheduler grammar needs a new engine family branch."
            )
        # Freeze the mutable containers so the frozen dataclass is honestly
        # immutable (a caller can't mutate ``scripts`` out from under the
        # registered backend class).
        object.__setattr__(self, "error_states", frozenset(self.error_states))
        object.__setattr__(self, "scripts", MappingProxyType(dict(self.scripts)))

    # -- pin wire format ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for pinning into experiment metadata."""
        return {
            "name": self.name,
            "family": self.family,
            "submit_bin": self.submit_bin,
            "job_id_regex": self.job_id_regex,
            "template_ext": self.template_ext,
            "supports_test_only_eta": self.supports_test_only_eta,
            "error_states": sorted(self.error_states),
            "scripts": dict(self.scripts),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SchedulerProfile:
        """Rebuild a profile from its pinned dict, validating shape loudly.

        Raises :class:`hpc_agent.errors.SpecInvalid` on a malformed pin so a
        corrupt ``scheduler_profile`` fails at load time rather than emitting
        a broken submit command later.
        """
        try:
            return cls(
                name=str(d["name"]),
                family=str(d["family"]),
                submit_bin=str(d["submit_bin"]),
                job_id_regex=str(d["job_id_regex"]),
                template_ext=str(d["template_ext"]),
                supports_test_only_eta=bool(d["supports_test_only_eta"]),
                error_states=frozenset(d.get("error_states", ()) or ()),
                scripts=dict(d.get("scripts", {}) or {}),
            )
        except KeyError as exc:
            raise errors.SpecInvalid(
                f"scheduler_profile pin missing required key: {exc.args[0]!r}"
            ) from exc


def render_script(profile: SchedulerProfile, *, kind: str) -> str:
    """Return the runtime job script body for *kind* (``cpu``/``gpu``/``mpi``).

    Phase 2 (Option C): the script is *rendered from the profile* rather
    than read from a static file shipped on disk. For the golden profiles
    this returns the exact bytes of the historical template (byte-for-byte
    golden test); a resolved profile carries its own bodies.
    """
    try:
        return profile.scripts[kind]
    except KeyError:
        raise errors.SpecInvalid(
            f"scheduler profile {profile.name!r} has no {kind!r} script "
            f"(available: {sorted(profile.scripts)})"
        ) from None


# ---------------------------------------------------------------------------
# Golden profiles — reproduce the historical SlurmBackend / SGEBackend.
# ---------------------------------------------------------------------------

SLURM_PROFILE = SchedulerProfile(
    name="slurm",
    family="slurm",
    submit_bin="sbatch",
    # sbatch prints ``Submitted batch job 12345``; anchor on the phrase so
    # a warning prefix with digits (``... 30% of nodes pre-empt; Submitted
    # batch job 12345``) can't poison the parse.
    job_id_regex=r"Submitted batch job\s+(\d+)",
    template_ext=".slurm",
    supports_test_only_eta=True,
    error_states=frozenset(
        {
            "FAILED",
            "NODE_FAIL",
            "BOOT_FAIL",
            "DEADLINE",
            "OUT_OF_MEMORY",
            "CANCELLED",
            "TIMEOUT",
            "PREEMPTED",
            "REVOKED",
        }
    ),
    scripts={"cpu": SLURM_CPU, "gpu": SLURM_GPU, "mpi": SLURM_MPI},
)

SGE_PROFILE = SchedulerProfile(
    name="sge",
    family="sge",
    submit_bin="qsub",
    # qsub prints ``Your job 12345 (...)`` or ``Your job-array 12345.1-10:1
    # (...)``; anchor on that phrase so a stray digit elsewhere can't win.
    job_id_regex=r"Your job(?:-array)?\s+(\d+)",
    template_ext=".sh",
    supports_test_only_eta=False,
    # SGE classifies by substring (``E`` -> error, ``h`` -> held), so the
    # exact-token set is intentionally empty for this family.
    error_states=frozenset(),
    scripts={"cpu": SGE_CPU, "gpu": SGE_GPU, "mpi": SGE_MPI},
)

# PBS family. Job ids are ``<seq>.<server>`` (arrays ``<seq>[].<server>``) —
# anchor on the ``.server`` suffix (SGE's ``Your job`` phrase and SLURM's
# bare ``\d+`` both fail on PBS). Finished-state success/failure is NOT in
# the live token (``F``/``C`` cover both) — it is read from ``Exit_status``
# via the history query (``query_pbs``), so ``error_states`` stays empty;
# the engine's pbs classify branch only buckets the live qstat tokens.
PBSPRO_PROFILE = SchedulerProfile(
    name="pbspro",
    family="pbspro",
    submit_bin="qsub",
    job_id_regex=r"(\d+)(?:\[\d*\])?\.[A-Za-z0-9_.-]+",
    template_ext=".pbs",
    supports_test_only_eta=False,
    error_states=frozenset(),
    scripts={"cpu": PBSPRO_CPU, "gpu": PBSPRO_GPU, "mpi": PBSPRO_MPI},
)

TORQUE_PROFILE = SchedulerProfile(
    name="torque",
    family="torque",
    submit_bin="qsub",
    job_id_regex=r"(\d+)(?:\[\d*\])?\.[A-Za-z0-9_.-]+",
    template_ext=".pbs",
    supports_test_only_eta=False,
    error_states=frozenset(),
    scripts={"cpu": TORQUE_CPU, "gpu": TORQUE_GPU, "mpi": TORQUE_MPI},
)

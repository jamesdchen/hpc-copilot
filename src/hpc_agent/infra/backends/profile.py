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
class FamilyDialect:
    """Per-family scheduler capability profile (the G9 upstream leg).

    Captures the grammar / exit-code semantics that the engine's command
    builders and query verdicts must branch on, so a family the primary dev
    loop does not exercise (``pbspro`` / ``torque``) cannot silently inherit
    a *sibling* family's rule by fallthrough — the whole G9
    "scheduler-dialect-monoculture" class. Keyed by ``family`` in
    :data:`FAMILY_DIALECTS`; the engine reads each capability off the dialect
    rather than hardcoding one family's behaviour into another's branch.

    Every ``family`` in :data:`KNOWN_FAMILIES` has exactly one dialect (pinned
    by a per-family contract fixture matrix in the tests), so adding a family
    without its capabilities fails a test rather than inheriting SGE's.

    Attributes
    ----------
    supports_comma_array_ranges:
        The array flag accepts a comma index LIST (``4,8,13-15``). ``True`` for
        SLURM ``--array`` and TORQUE ``-t``; ``False`` for SGE ``qsub -t`` and
        PBS Pro ``qsub -J`` (which accept a single ``n[-m[:s]]`` range only), so
        a non-contiguous resubmit on a ``False`` family is split into one array
        per contiguous run before submission (#6).
    cap_style:
        How an in-array concurrency cap is emitted (#32). ``"range_suffix"``
        appends ``%N`` to the array range (SLURM ``--array=1-100%20``, TORQUE
        ``-t 1-100%20``); ``"tc_flag"`` emits a separate ``-tc N`` flag and
        leaves the range bare (SGE/UGE); ``"max_run_subjobs"`` emits a separate
        ``-l max_run_subjobs=N`` attribute and leaves the range bare (PBS Pro,
        whose ``-J`` rejects a ``%N`` suffix — ``qsub: illegal -J value``).
    explicit_id_liveness_query:
        The liveness / state query names EXPLICIT job ids (SLURM ``squeue -j``,
        PBS ``qstat -t <ids>``), so a non-zero rc means the queried ids have
        left the queue — expected, not a failure — and sentinel-ack PRESENCE
        alone proves the query ran (#5). ``False`` for SGE, whose
        ``qstat -u $USER`` queries the whole user queue and exits 0 on an empty
        queue, so a non-zero rc IS the qstat binary failing and rc==0 is
        required to trust the (possibly empty) answer.
    """

    family: str
    supports_comma_array_ranges: bool
    cap_style: str
    explicit_id_liveness_query: bool


# The per-family capability matrix. One entry per :data:`KNOWN_FAMILIES`
# member (a test pins the keyset equality). ``pbspro`` and ``torque`` diverge
# on BOTH the comma-list grammar and the cap syntax, which is exactly why the
# monoculture bugs (#5/#6/#32) fired: the PBS branch had copied TORQUE's/SGE's
# rule onto PBS Pro.
FAMILY_DIALECTS: Mapping[str, FamilyDialect] = MappingProxyType(
    {
        "slurm": FamilyDialect(
            family="slurm",
            supports_comma_array_ranges=True,
            cap_style="range_suffix",
            explicit_id_liveness_query=True,
        ),
        "torque": FamilyDialect(
            family="torque",
            supports_comma_array_ranges=True,
            cap_style="range_suffix",
            explicit_id_liveness_query=True,
        ),
        "sge": FamilyDialect(
            family="sge",
            supports_comma_array_ranges=False,
            cap_style="tc_flag",
            explicit_id_liveness_query=False,
        ),
        "pbspro": FamilyDialect(
            family="pbspro",
            supports_comma_array_ranges=False,
            cap_style="max_run_subjobs",
            explicit_id_liveness_query=True,
        ),
    }
)


def dialect_for(family: str) -> FamilyDialect:
    """Return the :class:`FamilyDialect` for *family* (loud on an unknown one)."""
    try:
        return FAMILY_DIALECTS[family]
    except KeyError:
        raise errors.SpecInvalid(
            f"no scheduler dialect for family {family!r}; "
            f"known families are {sorted(FAMILY_DIALECTS)}"
        ) from None


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

# PBS family. Job ids are ``<seq>.<server>`` (arrays ``<seq>[].<server>``).
# The regex must do two jobs its SLURM/SGE siblings' phrase anchors already do
# — plus one PBS-specific one:
#   * PRESERVE the array ``[]`` in the captured id (#F36). PBS Pro/TORQUE
#     address an array as ``<seq>[]``; ``qstat -t`` / ``qdel`` on the bare
#     ``<seq>`` resolve a non-existent id ('Unknown Job Id'), so the captured
#     id KEEPS the bracket group (``12345[]``). ``parse_alive_output`` /
#     ``parse_scheduler_states`` normalise BOTH the stored id and the row id to
#     the bare sequence for MATCHING, but the persisted/dispatched id retains
#     the bracket so the round-trip addresses the real array.
#   * LINE-ANCHOR + prefer-last (#F39). qsub prints the id alone on its own
#     line, but many sites echo informational banners with dotted numbers
#     (``est. wait 1.5 hours``, ``PBS Pro 2022.1``) BEFORE it — and the submit
#     path's own ``bash -lc`` login shell can trigger profile.d echoes. Without
#     an anchor the old shape-only pattern matched ``1`` in ``1.5`` and
#     journaled a phantom id. ``(?ms)`` makes ``^…$`` match a whole physical
#     line; the leading greedy ``.*`` makes ``search`` bind the LAST id-shaped
#     line (mandatory, not optional — SGE/SLURM anchor on a phrase, PBS has no
#     phrase, so prefer-last is the PBS equivalent). A banner-only stdout with
#     no id line now yields NO match → ``submit_one`` raises loudly rather than
#     tracking a phantom.
# Finished-state success/failure is NOT in the live token (``F``/``C`` cover
# both) — it is read from ``Exit_status`` via the history query (``query_pbs``),
# so ``error_states`` stays empty; the engine's pbs classify branch only
# buckets the live qstat tokens.
_PBS_JOB_ID_REGEX = r"(?ms).*^(\d+(?:\[\d*\])?)\.[A-Za-z0-9_.-]+\s*$"

PBSPRO_PROFILE = SchedulerProfile(
    name="pbspro",
    family="pbspro",
    submit_bin="qsub",
    job_id_regex=_PBS_JOB_ID_REGEX,
    template_ext=".pbs",
    supports_test_only_eta=False,
    error_states=frozenset(),
    scripts={"cpu": PBSPRO_CPU, "gpu": PBSPRO_GPU, "mpi": PBSPRO_MPI},
)

TORQUE_PROFILE = SchedulerProfile(
    name="torque",
    family="torque",
    submit_bin="qsub",
    job_id_regex=_PBS_JOB_ID_REGEX,
    template_ext=".pbs",
    supports_test_only_eta=False,
    error_states=frozenset(),
    scripts={"cpu": TORQUE_CPU, "gpu": TORQUE_GPU, "mpi": TORQUE_MPI},
)

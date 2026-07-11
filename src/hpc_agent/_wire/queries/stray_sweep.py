"""Pydantic models for the ``stray-sweep`` login-node hygiene probe (query).

run-12 finding 20 LAYER 2: the connection storm's REMOTE residue. Orphaned
remote halves of killed ssh commands (a bash+python pair per poll) accumulate
until the login node's per-user process quota is exhausted and sshd itself
cannot fork a shell — an unrecoverable state (the ``kill`` builtin needs a
shell to run IN). This verb is the observability + bounded-cleanup half:

* every framework remote command now self-identifies with an ``HPC_AGENT_OP``
  argv marker (:func:`hpc_agent.infra.remote.build_remote_command`);
* this probe runs ONE fork-minimal ``ps`` over ssh, counts the user's total
  processes, and identifies MARKED processes older than the max legitimate
  deadline as strays — the 47-stray count that would have been visible days
  before the quota wedged;
* only with an explicit ``reap`` flag does it ``kill`` exactly those PIDs —
  never an unmarked user process.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StraySweepSpec(BaseModel):
    """Input spec for the ``stray-sweep`` verb."""

    model_config = ConfigDict(extra="forbid", title="stray-sweep input spec")

    ssh_target: str = Field(
        min_length=1,
        description=(
            "SSH destination of the login node to sweep — user@host or an "
            "OpenSSH alias, exactly as the run record's ssh_target."
        ),
    )
    reap: bool = Field(
        default=False,
        description=(
            "When True, kill the strays this sweep found (MARKED framework "
            "processes older than max_age_sec) — and ONLY those exact PIDs, "
            "never an unmarked user process. Default False makes the verb pure "
            "detection (the never-act-without-the-flag posture)."
        ),
    )
    max_age_sec: int = Field(
        default=3900,
        ge=1,
        description=(
            "Age (seconds) beyond which a MARKED process is a stray. Defaults "
            "just above the remote self-destruct default (3600s deadline + 60s "
            "margin + slack): a marked process older than this outlived even the "
            "generous no-client-timeout bound, so it is an orphan the timeout "
            "wrapper failed to reap (or a pre-wrapper process)."
        ),
    )
    warn_threshold: int = Field(
        default=40,
        ge=1,
        description=(
            "Total process count above which needs_attention flips — the "
            "early-warning the quota-wedge lacked (the login-node fork limit is "
            "typically a few hundred; 40 leaves ample headroom to act)."
        ),
    )


class StrayProcess(BaseModel):
    """One MARKED framework process observed on the login node."""

    model_config = ConfigDict(extra="forbid", title="stray-sweep marked process")

    pid: int = Field(description="Process id on the login node.")
    etimes: int = Field(description="Elapsed time in whole seconds since the process started.")
    op: str | None = Field(
        default=None,
        description="The HPC_AGENT_OP label parsed from the marker (verb/op tag), or null.",
    )
    args: str = Field(description="The process's argv line (ps -o args), for the human.")


class StraySweepResult(BaseModel):
    """Shape of the ``data`` field on a ``stray-sweep`` envelope."""

    model_config = ConfigDict(extra="forbid", title="stray-sweep output data")

    ssh_target: str = Field(description="The login node that was swept.")
    total_process_count: int = Field(
        description="Total processes the user owns on the login node (fork-quota pressure)."
    )
    marked_count: int = Field(
        description="How many of those carry the HPC_AGENT_OP framework marker."
    )
    strays: list[StrayProcess] = Field(
        default_factory=list,
        description=(
            "MARKED processes older than max_age_sec — orphaned remote halves "
            "the timeout wrapper did not reap (or pre-wrapper residue)."
        ),
    )
    reaped_pids: list[int] = Field(
        default_factory=list,
        description=(
            "PIDs actually killed this sweep (empty unless spec.reap was True and "
            "strays were found). Exactly the stray PIDs — never an unmarked process."
        ),
    )
    reaped: bool = Field(
        default=False, description="Whether a reap was requested AND executed this sweep."
    )
    needs_attention: bool = Field(
        default=False,
        description=(
            "True when the total process count exceeds warn_threshold OR any "
            "stray was found — the early-warning the fork-exhaustion incident lacked."
        ),
    )
    summary: str = Field(
        default="", description="One-line human digest of the sweep (counts + verdict)."
    )

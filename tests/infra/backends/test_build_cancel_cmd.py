"""Range-aware ``build_cancel_cmd`` on the backend seam (M-KILL, SPEC §2 Δ4).

The cancel seam speaks the SAME array-index vocabulary as the submit side: a
``task_range`` (``"4,8,13-15"``) scopes the cancel to those array indices —
SGE ``qdel <id> -t <range>``, SLURM ``scancel <id>_[<range>]``. ``None`` cancels
the whole array (byte-identical to the pre-range command). These pin the exact
emitted strings and prove the whole-run path is untouched.

Every non-empty output is run under a NON-interactive LOGIN shell
(``bash -lc <inner>``) so the scheduler binary (qdel/scancel) resolves on
``PATH`` over ssh_run's non-login transport — the same wrap the query builders
carry, minus the sentinel-ack (cancel's success is confirmed by the follow-up
alive-check, not its own exit code). The tests unwrap that login shell via
:func:`_login_inner` and pin the INNER command bytes; a dedicated test pins the
``bash -lc`` wrap for every family.
"""

from __future__ import annotations

import shlex

from hpc_agent.infra.backends import get_backend_class

SGE = get_backend_class("sge")
SLURM = get_backend_class("slurm")
PBSPRO = get_backend_class("pbspro")


def _login_inner(out: str) -> str:
    """Unwrap a ``bash -lc <inner>`` login-shell cancel command; return <inner>.

    Every non-empty ``build_cancel_cmd`` output runs its cancel under a
    NON-interactive LOGIN shell so the scheduler binary (qdel/scancel) resolves
    on ``PATH``: Hoffman2/UGE et al. install it onto PATH only via the login
    profile chain, and ssh_run's transport uses a non-login ``bash -c`` (a bare
    ``qdel`` is rc 127 there). This mirrors the query builders' idiom (``bash
    -lc {shlex.quote(inner)}``) minus the sentinel-ack — cancel carries none, its
    success being confirmed by the follow-up alive-check. ``shlex.split`` reverses
    ``shlex.quote``, so the third token is the exact inner the login shell runs.
    """
    parts = shlex.split(out)
    assert parts[:2] == ["bash", "-lc"], f"not a login-shell command: {out!r}"
    assert len(parts) == 3, f"expected `bash -lc <inner>`, got {out!r}"
    return parts[2]


# --- login-shell wrap: every family, every shape (the new invariant) ---------


def test_every_family_cancel_is_login_shell_wrapped() -> None:
    """Every non-empty cancel output starts ``bash -lc `` (PATH resolution).

    The whole-run and range-scoped cancels of every family are login-wrapped so a
    bare ``qdel``/``scancel`` never hits ssh_run's non-login shell (rc 127 on
    Hoffman2/UGE). The empty-id short-circuit stays a bare ``true`` no-op.
    """
    for cls in (SLURM, SGE, PBSPRO):
        assert cls.build_cancel_cmd(["1", "2"]).startswith("bash -lc ")
        assert cls.build_cancel_cmd(["1", "2"], "4,8,13-15").startswith("bash -lc ")
        assert cls.build_cancel_cmd([]) == "true"  # no-op stays bare


# --- range-scoped cancel (the new affordance) -------------------------------


def test_sge_range_cancel_uses_qdel_dash_t() -> None:
    # SGE addresses array subtasks with ``-t <range>`` (the submit ``-t`` dialect).
    # A single contiguous segment stays one ``qdel -t`` call.
    assert _login_inner(SGE.build_cancel_cmd(["12345"], "13-15")) == "qdel 12345 -t 13-15"


# --- SGE non-contiguous decomposition (the reported defect) ------------------
#
# SGE/UGE ``qdel -t`` accepts a SINGLE ``n[-m[:s]]`` range only — a whole-set
# ``qdel <id> -t 4,8,13-15`` cancels at most the leading task and leaves the
# rest running. The cancel MUST decompose the non-contiguous undone set into one
# ``qdel -t`` per contiguous comma segment, covering EXACTLY the set.


def test_sge_noncontiguous_decomposes_into_one_qdel_per_run() -> None:
    # {4,8,13,14,15} == "4,8,13-15" -> three ranges: 4 / 8 / 13-15. The whole
    # ``;``-joined chain rides inside ONE login shell.
    inner = _login_inner(SGE.build_cancel_cmd(["12345"], "4,8,13-15"))
    assert inner == "qdel 12345 -t 4 ; qdel 12345 -t 8 ; qdel 12345 -t 13-15"
    # Sequenced with ``;`` (never ``&&``): an already-gone leading task must not
    # abort the cancel of the remaining segments (never a subset).
    assert "&&" not in inner
    # Exactly the undone set — no task outside {4,8,13,14,15} is ever named.
    segs = [part.split("-t ")[1].strip() for part in inner.split(" ; ")]
    assert segs == ["4", "8", "13-15"]


def test_sge_single_task_is_one_range() -> None:
    # Boundary {4} -> a single ``qdel -t 4``.
    assert _login_inner(SGE.build_cancel_cmd(["12345"], "4")) == "qdel 12345 -t 4"


def test_sge_contiguous_set_is_one_range() -> None:
    # {4,5,6} arrives compacted as "4-6" (compact_task_ids output) -> ONE range,
    # never split into three single-index qdels.
    assert _login_inner(SGE.build_cancel_cmd(["12345"], "4-6")) == "qdel 12345 -t 4-6"


def test_sge_noncontiguous_fans_across_multiple_ids() -> None:
    # Every job id is named in every per-segment qdel.
    inner = _login_inner(SGE.build_cancel_cmd(["10", "20"], "4,13-15"))
    assert inner == "qdel 10 20 -t 4 ; qdel 10 20 -t 13-15"


def test_sge_whole_array_is_not_named_by_a_range_cancel() -> None:
    # A range cancel must NEVER degrade to the whole-array ``qdel <id>`` (that
    # would cancel a superset — running/done tasks outside the undone set).
    inner = _login_inner(SGE.build_cancel_cmd(["12345"], "4,8,13-15"))
    assert "-t" in inner
    for part in inner.split(" ; "):
        assert part.strip().startswith("qdel 12345 -t ")


def test_slurm_range_cancel_uses_bracket_subscript() -> None:
    # SLURM addresses array subtasks with ``<id>_[<indices>]``.
    assert _login_inner(SLURM.build_cancel_cmd(["999"], "4,8,13-15")) == "scancel 999_[4,8,13-15]"


def test_slurm_range_cancel_fans_across_multiple_ids() -> None:
    # Each job id gets its own ``_[<range>]`` subscript.
    assert _login_inner(SLURM.build_cancel_cmd(["10", "20"], "1-3")) == "scancel 10_[1-3] 20_[1-3]"


def test_slurm_range_cancel_threads_federated_cluster() -> None:
    # #F37 -M cluster survives the range branch.
    assert (
        _login_inner(SLURM.build_cancel_cmd(["999"], "4,8", cluster="gpu"))
        == "scancel -M gpu 999_[4,8]"
    )


def test_sge_range_single_index() -> None:
    assert _login_inner(SGE.build_cancel_cmd(["777"], "42")) == "qdel 777 -t 42"


# --- whole-run path (pins hold on the login-shell inner, range=None default) --


def test_whole_run_cancel_unchanged_slurm() -> None:
    assert _login_inner(SLURM.build_cancel_cmd(["100", "200", "300"])) == "scancel 100 200 300"
    assert (
        _login_inner(SLURM.build_cancel_cmd(["100", "200", "300"], None)) == "scancel 100 200 300"
    )


def test_whole_run_cancel_unchanged_sge() -> None:
    assert _login_inner(SGE.build_cancel_cmd(["100", "200"])) == "qdel 100 200"
    assert _login_inner(SGE.build_cancel_cmd(["100", "200"], None)) == "qdel 100 200"


def test_empty_id_list_short_circuits_regardless_of_range() -> None:
    # No bare scancel/qdel ever — even with a range set. The no-op stays a bare
    # ``true`` (no login shell needed for a no-op).
    assert SLURM.build_cancel_cmd([], "1-3") == "true"
    assert SGE.build_cancel_cmd([], "1-3") == "true"


def test_pbs_whole_run_still_qdel() -> None:
    # The PBS family shares the qdel branch; the whole-run pin is untouched.
    assert _login_inner(PBSPRO.build_cancel_cmd(["12345", "12346"])) == "qdel 12345 12346"

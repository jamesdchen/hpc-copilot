"""Range-aware ``build_cancel_cmd`` on the backend seam (M-KILL, SPEC §2 Δ4).

The cancel seam speaks the SAME array-index vocabulary as the submit side: a
``task_range`` (``"4,8,13-15"``) scopes the cancel to those array indices —
SGE ``qdel <id> -t <range>``, SLURM ``scancel <id>_[<range>]``. ``None`` cancels
the whole array (byte-identical to the pre-range command). These pin the exact
emitted strings and prove the whole-run path is untouched.
"""

from __future__ import annotations

from hpc_agent.infra.backends import get_backend_class

SGE = get_backend_class("sge")
SLURM = get_backend_class("slurm")
PBSPRO = get_backend_class("pbspro")


# --- range-scoped cancel (the new affordance) -------------------------------


def test_sge_range_cancel_uses_qdel_dash_t() -> None:
    # SGE addresses array subtasks with ``-t <range>`` (the submit ``-t`` dialect).
    assert SGE.build_cancel_cmd(["12345"], "4,8,13-15") == "qdel 12345 -t 4,8,13-15"


def test_slurm_range_cancel_uses_bracket_subscript() -> None:
    # SLURM addresses array subtasks with ``<id>_[<indices>]``.
    assert SLURM.build_cancel_cmd(["999"], "4,8,13-15") == "scancel 999_[4,8,13-15]"


def test_slurm_range_cancel_fans_across_multiple_ids() -> None:
    # Each job id gets its own ``_[<range>]`` subscript.
    assert SLURM.build_cancel_cmd(["10", "20"], "1-3") == "scancel 10_[1-3] 20_[1-3]"


def test_slurm_range_cancel_threads_federated_cluster() -> None:
    # #F37 -M cluster survives the range branch.
    assert SLURM.build_cancel_cmd(["999"], "4,8", cluster="gpu") == "scancel -M gpu 999_[4,8]"


def test_sge_range_single_index() -> None:
    assert SGE.build_cancel_cmd(["777"], "42") == "qdel 777 -t 42"


# --- whole-run path is byte-identical (pins hold, range=None default) --------


def test_whole_run_cancel_unchanged_slurm() -> None:
    assert SLURM.build_cancel_cmd(["100", "200", "300"]) == "scancel 100 200 300"
    assert SLURM.build_cancel_cmd(["100", "200", "300"], None) == "scancel 100 200 300"


def test_whole_run_cancel_unchanged_sge() -> None:
    assert SGE.build_cancel_cmd(["100", "200"]) == "qdel 100 200"
    assert SGE.build_cancel_cmd(["100", "200"], None) == "qdel 100 200"


def test_empty_id_list_short_circuits_regardless_of_range() -> None:
    # No bare scancel/qdel ever — even with a range set.
    assert SLURM.build_cancel_cmd([], "1-3") == "true"
    assert SGE.build_cancel_cmd([], "1-3") == "true"


def test_pbs_whole_run_still_qdel() -> None:
    # The PBS family shares the qdel branch; the whole-run pin is untouched.
    assert PBSPRO.build_cancel_cmd(["12345", "12346"]) == "qdel 12345 12346"

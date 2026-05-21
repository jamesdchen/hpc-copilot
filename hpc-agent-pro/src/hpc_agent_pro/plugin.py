"""hpc-agent-pro plugin object.

Discovered by the host ``hpc-agent`` package through the
``hpc_agent.plugins`` entry-point group (see
``hpc_agent._internal.plugins``). The host resolves the entry point via
``ep.load()`` and reads two optional attributes off the result with
``getattr``:

* ``primitive_modules`` — dotted module paths the host imports (after
  the core primitive modules) so their ``@primitive`` decorators
  register.
* ``register_cli(subparsers)`` — callable handed argparse's
  ``_SubParsersAction`` so the plugin can add CLI subcommands.

This module *is* the plugin object: the entry point points straight at
the module, and ``getattr(module, "primitive_modules")`` /
``getattr(module, "register_cli")`` resolve to the names defined here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse


# ─── primitive modules ─────────────────────────────────────────────────────
#
# Every module in this package that runs an ``@primitive(...)`` decorator
# at import time. The host imports each one after the core modules so the
# decorators self-register into the shared registry.
#
# ORDERING mirrors the relative order of the moved entries in the public
# package's ``hpc_agent._internal.primitive._PRIMITIVE_MODULES`` tuple:
# atoms / leaf modules precede any composite that references them. None
# of the moved primitives declare ``composes=``, so the order is not
# load-critical here, but keeping it faithful avoids surprises if a
# ``composes=`` edge is added later.
primitive_modules: tuple[str, ...] = (
    # read-runtime-prior — public ``state.runtime_prior`` slot.
    "hpc_agent_pro.commands.read_runtime_prior",
    # forecast leaves.
    "hpc_agent_pro.forecast.best_submit_window",
    "hpc_agent_pro.forecast.queue_wait_baseline",
    # plan-submit composite.
    "hpc_agent_pro.planning.planner",
    # inspect-cluster — public ``infra.inspect`` slot.
    "hpc_agent_pro.commands.inspect_cluster",
    # calibration / forecast atoms.
    "hpc_agent_pro.atoms.house_edge",
    "hpc_agent_pro.atoms.predict_start_time",
    "hpc_agent_pro.atoms.recommend_wait_alternative",
    "hpc_agent_pro.atoms.walltime_drift",
    # validate — scheduler --test-only probe wrapper.
    "hpc_agent_pro.planning.validate",
)


# ─── CLI helpers (mirrored from hpc_agent.agent_cli) ───────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _add_profile_cluster_cmdsha(
    parser: argparse.ArgumentParser,
    *,
    cmd_sha_help: str | None = None,
) -> None:
    """Add the ``--profile`` / ``--cluster`` / ``--cmd-sha`` trio."""
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument(
        "--cmd-sha",
        default=None,
        help=cmd_sha_help or "If set, filter runtime priors to samples with this cmd_sha.",
    )


# ─── subcommand dispatchers ────────────────────────────────────────────────
#
# Each ``cmd_*`` adapter reuses the public package's ``agent_cli`` output
# helpers (``_ok``, ``_load_spec``, ``_validate_against_schema``,
# ``_require_ssh_agent``, ``EXIT_OK``) and the public ``errors`` module so
# the envelope contract is identical to the in-tree subcommands.


def cmd_predict_start_time(args: argparse.Namespace) -> int:
    from hpc_agent import errors
    from hpc_agent.agent_cli import EXIT_OK, _load_spec, _ok

    from hpc_agent_pro._schema_models.queries.predict_start_time import PredictStartTimeSpec
    from hpc_agent_pro.atoms.predict_start_time import predict_start_time_primitive

    intent = _load_spec(args.spec, schema_name="predict_start_time")
    if not intent:
        raise errors.SpecInvalid("--spec is required for `predict-start-time`")
    try:
        spec = PredictStartTimeSpec.model_validate(intent)
    except Exception as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    experiment_dir = Path(args.experiment_dir).resolve()
    result = predict_start_time_primitive(experiment_dir, spec=spec)
    _ok(result.model_dump(mode="json"), name="predict-start-time")
    return EXIT_OK


def cmd_inspect_cluster(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok, _require_ssh_agent

    from hpc_agent_pro.commands.inspect_cluster import inspect_cluster

    if (rc := _require_ssh_agent()) is not None:
        return rc
    snap = inspect_cluster(
        args.cluster,
        sacct_window_hours=args.sacct_window_hours,
        stress_alloc_mem_pct=args.stress_alloc_mem_pct,
        stress_cpu_load_frac=args.stress_cpu_load_frac,
        use_cache=not args.no_cache,
    )
    payload = snap.to_dict()
    partial = list(payload.get("errors") or [])
    _ok(payload, name="inspect-cluster", partial_errors=partial or None)
    return EXIT_OK


def cmd_runtime_prior(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok

    from hpc_agent_pro.commands.read_runtime_prior import roll_up_quantiles

    out = roll_up_quantiles(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        cmd_sha=args.cmd_sha,
    )
    _ok(out, name="read-runtime-prior")
    return EXIT_OK


def cmd_plan_submit(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok, _require_ssh_agent

    from hpc_agent_pro.planning.planner import plan_submit

    if (rc := _require_ssh_agent()) is not None:
        return rc
    candidates: list[str] | None = None
    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    out = plan_submit(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        candidates=candidates,
        cmd_sha=args.cmd_sha,
        adversarial=not bool(getattr(args, "no_adversarial", False)),
        walltime_safety_mult=float(getattr(args, "walltime_safety_mult", 1.30)),
        target_backfill_window_sec=getattr(args, "target_backfill_window_sec", None),
        current_max_array_size=getattr(args, "current_max_array_size", None),
        est_per_task_sec=getattr(args, "est_per_task_sec", None),
    )
    _ok(out, name="score-submit-plan")
    return EXIT_OK


def cmd_walltime_drift(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok

    from hpc_agent_pro.atoms.walltime_drift import walltime_drift

    _ok(
        walltime_drift(
            experiment_dir=args.experiment_dir,
            profile=args.profile,
            cluster=args.cluster,
            cmd_sha=args.cmd_sha,
            base_safety_mult=float(args.base_safety_mult),
        ),
        name="walltime-drift",
    )
    return EXIT_OK


def cmd_house_edge(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok

    from hpc_agent_pro.atoms.house_edge import house_edge

    _ok(
        house_edge(
            experiment_dir=args.experiment_dir,
            profile=args.profile,
            cluster=args.cluster,
            cmd_sha=args.cmd_sha,
        ),
        name="house-edge",
    )
    return EXIT_OK


def cmd_predict_queue_wait(args: argparse.Namespace) -> int:
    from typing import Any as _Any

    from hpc_agent.agent_cli import EXIT_OK, _ok, _validate_against_schema

    from hpc_agent_pro._schema_models.queries.predict_queue_wait import PredictQueueWaitSpec
    from hpc_agent_pro.forecast.queue_wait_baseline import predict_queue_wait

    payload: dict[str, _Any] = {
        "profile": args.profile,
        "cluster": args.cluster,
        "backend": args.backend,
        "n_replications": int(args.n_replications),
    }
    if args.at_iso is not None:
        payload["at_iso"] = args.at_iso
    if args.seed is not None:
        payload["seed"] = int(args.seed)
    _validate_against_schema(payload, "predict_queue_wait")
    spec = PredictQueueWaitSpec.model_validate(payload)
    out = predict_queue_wait(args.experiment_dir, spec=spec)
    _ok(out.to_dict(), name="predict-queue-wait")
    return EXIT_OK


def cmd_best_submit_window(args: argparse.Namespace) -> int:
    from hpc_agent.agent_cli import EXIT_OK, _ok, _validate_against_schema

    from hpc_agent_pro._schema_models.queries.best_submit_window import BestSubmitWindowSpec
    from hpc_agent_pro.forecast.best_submit_window import best_submit_windows

    raw = {
        "profile": args.profile,
        "cluster": args.cluster,
        "within_hours": int(args.within_hours),
        "top_k": int(args.top_k),
    }
    _validate_against_schema(raw, "best_submit_window")
    spec = BestSubmitWindowSpec.model_validate(raw)
    candidates = best_submit_windows(args.experiment_dir, spec=spec)
    _ok(
        {
            "profile": spec.profile,
            "cluster": spec.cluster,
            "within_hours": spec.within_hours,
            "top_k": spec.top_k,
            "candidates": [c.to_dict() for c in candidates],
        },
        name="best-submit-window",
    )
    return EXIT_OK


def register_cli(subparsers: Any) -> None:
    """Add the moved primitives' CLI subcommands to *subparsers*.

    *subparsers* is argparse's ``_SubParsersAction`` from the host CLI.
    Each ``add_parser`` block is a faithful copy of the corresponding
    block in ``hpc_agent.agent_cli`` so the plugin subcommands present
    an identical interface to the ones the public CLI used to ship.

    Coverage note: ``inspect-cluster``, ``read-runtime-prior`` (CLI name
    ``runtime-prior``), ``plan-submit``, ``predict-start-time``,
    ``predict-queue-wait``, ``best-submit-window``, ``walltime-drift``
    and ``house-edge`` are wired here. The ``validate`` and
    ``recommend-wait-alternative`` primitives have no standalone CLI
    subcommand in the public ``agent_cli`` (no ``add_parser`` block,
    no ``cli=`` on the decorator), so none is added here either —
    matching the public surface.
    """
    sub = subparsers

    # predict-start-time
    p_pst = sub.add_parser(
        "predict-start-time",
        help=(
            "Floor + LightGBM-residual forecast for when a hypothetical job "
            "would start. Sweeps candidate submit-at-T offsets and returns "
            "the lowest-total-time-to-start option."
        ),
    )
    p_pst.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="Path to predict_start_time.input.json conforming to the schema.",
    )
    p_pst.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path("."),
        help="Path to the experiment directory; defaults to cwd.",
    )
    p_pst.set_defaults(func=cmd_predict_start_time)

    # inspect-cluster
    p_ic = sub.add_parser(
        "inspect-cluster",
        help=(
            "Snapshot a cluster's per-node state (alloc mem, CPU load, "
            "co-tenants, drain). Read-only; output is the planner's input."
        ),
    )
    p_ic.add_argument("--cluster", required=True, help="Cluster name from clusters.yaml.")
    p_ic.add_argument(
        "--sacct-window-hours",
        type=int,
        default=24,
        help="Look-back window for co-tenant attribution (default 24h).",
    )
    p_ic.add_argument(
        "--stress-alloc-mem-pct",
        type=float,
        default=0.80,
        help="AllocMem fraction above which a node is flagged is_stressed.",
    )
    p_ic.add_argument(
        "--stress-cpu-load-frac",
        type=float,
        default=0.80,
        help="CPULoad/CPUTot fraction above which a node is flagged is_stressed.",
    )
    p_ic.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the 60s in-process cache and re-poll the cluster.",
    )
    p_ic.set_defaults(func=cmd_inspect_cluster)

    # plan-submit
    p_ps = sub.add_parser(
        "plan-submit",
        help=(
            "Score candidate constraints for a submit. Combines inspect-cluster "
            "and runtime priors. Output is JSON the slash command hands to "
            "Claude for cost-model judgment."
        ),
    )
    _add_experiment_dir(p_ps)
    _add_profile_cluster_cmdsha(p_ps)
    p_ps.add_argument(
        "--candidates",
        default=None,
        help=(
            "Optional comma-separated list of constraint expressions to evaluate "
            "(e.g. 'a100,a40|a100,a40|a100|v100'). Defaults to single-GPU + "
            "all-GPU-types from clusters.yaml."
        ),
    )
    p_ps.add_argument(
        "--no-adversarial",
        action="store_true",
        help=(
            "Disable the default backfill-attack mode. By default, plan-submit "
            "right-sizes the walltime ask from runtime priors and probes a "
            "(walltime x constraint) lattice via `sbatch --test-only` to find "
            "the variant SLURM predicts will start earliest."
        ),
    )
    p_ps.add_argument(
        "--walltime-safety-mult",
        type=float,
        default=1.30,
        help=(
            "Multiplier applied to the runtime prior's p95 to derive the "
            "right-sized walltime ask. Default 1.30 (30%% pad)."
        ),
    )
    p_ps.add_argument(
        "--target-backfill-window-sec",
        type=int,
        default=None,
        help=(
            "Adversarial knob: a typical backfill gap size on this cluster "
            "(e.g., 1800 for 30 minutes). Triggers array-reshape and "
            "walltime-split recommendations sized to fit that window."
        ),
    )
    p_ps.add_argument(
        "--current-max-array-size",
        type=int,
        default=None,
        help="Adversarial array-reshape input: the cluster's configured max array size.",
    )
    p_ps.add_argument(
        "--est-per-task-sec",
        type=int,
        default=None,
        help="Adversarial knob: estimated per-task runtime (typically the p95 from runtime-prior).",
    )
    p_ps.set_defaults(func=cmd_plan_submit)

    # runtime-prior  (read-runtime-prior primitive)
    p_rp = sub.add_parser(
        "runtime-prior",
        help="Quantile rollup of runtime samples for a (profile, cluster).",
    )
    _add_experiment_dir(p_rp)
    _add_profile_cluster_cmdsha(
        p_rp,
        cmd_sha_help="Filter samples to one cmd_sha (recommended after .hpc/tasks.py changes).",
    )
    p_rp.set_defaults(func=cmd_runtime_prior)

    # walltime-drift
    p_wd = sub.add_parser(
        "walltime-drift",
        help=(
            "Closed-loop calibration: measure cliff-kill rate from past "
            "samples and recommend an adjusted safety_mult per cluster."
        ),
    )
    _add_experiment_dir(p_wd)
    _add_profile_cluster_cmdsha(p_wd)
    p_wd.add_argument("--base-safety-mult", type=float, default=1.30)
    p_wd.set_defaults(func=cmd_walltime_drift)

    # house-edge
    p_he = sub.add_parser(
        "house-edge",
        help=(
            "Compare planner's --test-only predictions against observed "
            "Submit->Start deltas. Validates that the lattice probe is "
            "finding real backfill windows and surfaces miscalibration."
        ),
    )
    _add_experiment_dir(p_he)
    _add_profile_cluster_cmdsha(p_he)
    p_he.set_defaults(func=cmd_house_edge)

    # predict-queue-wait
    p_pqw = sub.add_parser(
        "predict-queue-wait",
        help=(
            "Forecast queue-wait seconds for a hypothetical submit. "
            "Backend 'auto' picks DES when a snapshot + user-profiles "
            "are available; falls back to the diurnal MA baseline."
        ),
    )
    _add_experiment_dir(p_pqw)
    p_pqw.add_argument("--profile", required=True)
    p_pqw.add_argument("--cluster", required=True)
    p_pqw.add_argument("--at-iso", default=None, help="reference timestamp (default: now)")
    p_pqw.add_argument("--backend", choices=["auto", "diurnal_ma", "des"], default="auto")
    p_pqw.add_argument(
        "--n-replications",
        type=int,
        default=64,
        help="DES replications (only used on the DES path)",
    )
    p_pqw.add_argument("--seed", type=int, default=None, help="seed for deterministic DES sampling")
    p_pqw.set_defaults(func=cmd_predict_queue_wait)

    # best-submit-window
    p_bsw = sub.add_parser(
        "best-submit-window",
        help=(
            "Sweep the diurnal queue-wait predictor over the next "
            "--within-hours hours and surface the top_k lowest-wait "
            "submit candidates."
        ),
    )
    _add_experiment_dir(p_bsw)
    p_bsw.add_argument("--profile", required=True)
    p_bsw.add_argument("--cluster", required=True)
    p_bsw.add_argument("--within-hours", type=int, default=24)
    p_bsw.add_argument("--top-k", type=int, default=5)
    p_bsw.set_defaults(func=cmd_best_submit_window)

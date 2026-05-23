"""Campaign verb-group argparse adapters.

Each ``cmd_campaign_*`` is a thin shim around the corresponding
``hpc_agent.atoms.campaign_*`` primitive. Helpers come from
:mod:`hpc_agent.cli._helpers` (the adapter SDK) — the older
lazy-import-from-agent_cli pattern was a workaround for an import cycle
that ``agent_cli`` → ``cli/`` ↔ ``cli/_helpers`` cleanly resolves.
"""

from __future__ import annotations

import argparse
from typing import Any

from hpc_agent.cli._helpers import EXIT_OK, _err, _ok, _validate_against_schema


def cmd_campaign_status(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_status."""
    from hpc_agent.atoms.campaign_status import campaign_status

    _ok(
        campaign_status(experiment_dir=args.experiment_dir, campaign_id=args.campaign_id),
        name="campaign-status",
    )
    return EXIT_OK


def cmd_campaign_list(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_list."""
    from hpc_agent.atoms.campaign_list import campaign_list

    _ok(campaign_list(experiment_dir=args.experiment_dir), name="campaign-list")
    return EXIT_OK


def cmd_campaign_init(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_init."""
    from hpc_agent.atoms.campaign_init import campaign_init

    _ok(
        campaign_init(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            goal=args.goal,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
            plateau_mode=args.plateau_mode,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
            strategy_name=args.strategy_name,
            strategy_params_json=args.strategy_params_json,
        ),
        name="campaign-init",
    )
    return EXIT_OK


def cmd_campaign_replay(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_replay."""
    from hpc_agent.atoms.campaign_replay import campaign_replay

    _ok(
        campaign_replay(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            last_n=args.last_n,
        ),
        name="campaign-replay",
    )
    return EXIT_OK


def cmd_campaign_converged(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_converged."""
    from hpc_agent.atoms.campaign_converged import campaign_converged

    _ok(
        campaign_converged(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
            plateau_mode=args.plateau_mode,
        ),
        name="campaign-converged",
    )
    return EXIT_OK


def cmd_campaign_budget(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_budget."""
    from hpc_agent.atoms.campaign_budget import campaign_budget

    _ok(
        campaign_budget(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
        ),
        name="campaign-budget",
    )
    return EXIT_OK


def cmd_campaign_advance(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_advance."""
    from hpc_agent.atoms.campaign_advance import campaign_advance

    _ok(
        campaign_advance(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
            plateau_mode=args.plateau_mode,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
        ),
        name="campaign-advance",
    )
    return EXIT_OK


def cmd_campaign_health(args: argparse.Namespace) -> int:
    """Aggregate run-history into a campaign-health payload (D2a).

    Thin CLI wrapper. The ``@primitive(name="campaign-health", ...)``
    decorator lives on ``hpc_agent.atoms.campaign_health.campaign_health``
    (the module-level implementation), matching the ``backed_by.python``
    pointer in ``docs/primitives/campaign-health.md``.
    """
    from hpc_agent import errors
    from hpc_agent.atoms.campaign_health import campaign_health

    payload: dict[str, Any] = {}
    if args.campaign_id is not None:
        payload["campaign_id"] = args.campaign_id
    if args.since_iso is not None:
        payload["since_iso"] = args.since_iso
    if args.profile is not None:
        payload["profile"] = args.profile
    if args.cluster is not None:
        payload["cluster"] = args.cluster
    _validate_against_schema(payload, "campaign_health")
    from hpc_agent._schema_models.queries.campaign_health import CampaignHealthSpec

    spec = CampaignHealthSpec.model_validate(payload)
    try:
        data = campaign_health(args.experiment_dir, spec=spec)
    except errors.HpcError:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort error envelope
        return _err(
            error_code="internal",
            message=f"campaign_health failed: {exc}",
            category="internal",
            retry_safe=False,
        )
    _ok(data, name="campaign-health")
    return EXIT_OK

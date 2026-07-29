"""``queue-run`` — one item arrives on the run queue. Ungated, by design.

Phase 1 of ``docs/plans/run-queue-placement-2026-07-28.md``: the intake half
of the organ the fleet was missing. Experiments "come in" from any session at
any time, and workflows are stateless relays that die and replay from cache,
so arrival must land in a durable kernel store rather than in workflow memory
(§1). This verb is that store's ONE writer: it stamps one record onto
``<experiment>/.hpc/queue/intake.jsonl`` (:mod:`hpc_agent.state.queue_intake`)
and echoes what the ledger now holds.

**R2 — enqueueing spends nothing** (§1: "New verb ``queue-run`` (mutate,
**ungated**): enqueueing spends nothing. Gates bind where they always bind —
at the cluster boundary of the run the item becomes."). So there is no consent
check, no greenlight probe, no journal write, and no SSH on this path. That is
not an omission to be fixed later: the human's y binds to a spec that NAMES a
cluster (§3, placement lives inside the y), and an item on the ledger has not
been placed. Gating arrival would take a verdict against a question the queue
cannot yet ask, and would make the cheap act (writing down what someone wants)
as expensive as the costly one (occupying a login node).

What IS refused here is the boundary input a later phase could not recover
from. A primitive owns its invariants (``docs/internals/adding-a-primitive.md``),
and the invariant this verb owns is that every record it writes is placeable in
principle:

* an explicit ``cluster`` pin is checked against the live ``clusters.yaml``
  through the real loader (:func:`hpc_agent.infra.clusters.load_clusters_config`,
  the same resolution order every transport-consuming verb sees). R5 makes a
  pin SUPREME over policy — placement will not second-guess it — so a pin
  naming a cluster that does not exist is an item nothing can ever place. It is
  refused loudly, with the near-miss suggestion, at the moment the operator can
  still fix the typo. Silently enqueueing it would convert a typo into a
  permanently held item whose disclosed reason ("no such cluster") arrives
  hours later in a brief;
* a blank pin is refused rather than silently demoted to "unpinned" — the two
  requests mean opposite things (an operator's choice vs. policy's choice), and
  guessing which was meant is exactly what R4/R5 forbid.

Everything else about the item is opaque: the resolve spec is recorded
verbatim and never interpreted (the resolver owns that invariant), a
``spec_ref`` is not dereferenced (a pointer that goes stale is a fact for the
projection to surface, not a reason to refuse arrival), and the resource asks
are typed by the wire model rather than judged here.

Idempotency is the client's to assert: the ``request_id`` on the spec is
passed straight through as :func:`hpc_agent.infra.io.append_jsonl_line`'s
``dedup_key`` (R8, §10.S2), so a relayed workflow turn — the engine replaying
a completed call verbatim from cache — writes no second line and gets the
ORIGINAL item back with ``replayed=True``. The probe runs inside the ledger's
flock, so it is race-free against a concurrent duplicate too.

This module is the ``queue-run`` leaf of the ``ops/queue`` subject; the
substrate it reaches (``state/queue_intake``) deliberately lives outside the
subject so ``queue-advance`` and ``campaign-advance`` can share it.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.queue_run import QueueRunResult, QueueRunSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.queue_intake import (
    STATE_QUEUED,
    append_intake_item,
    find_intake_item,
    intake_path,
    item_cmd_sha,
    item_run_id,
    items_in_states,
    read_intake_items,
)

__all__ = ["queue_run"]

_PRIMITIVE = "queue-run"


def _validated_cluster_pin(pin: str | None) -> str | None:
    """Return the normalized explicit pin, or ``None`` when the item is unpinned.

    Raises :class:`errors.SpecInvalid` for a blank pin (a malformed spec value),
    :class:`errors.ClusterUnknown` for one absent from the active
    ``clusters.yaml`` (the house error for that exact fact), and
    :class:`errors.ConfigInvalid` when the config cannot be read at all — an
    unverifiable pin is not a verified pin, and R5 means nothing downstream will
    re-examine it.

    The loader import is lazy: an unpinned enqueue (the common case) must not
    pay for the yaml parse, and R2's "spends nothing" is a claim about cost as
    well as about consent.
    """
    if pin is None:
        return None
    key = pin.strip()
    if not key:
        raise errors.SpecInvalid(
            "queue-run: cluster pin is blank. Omit the field to let placement choose "
            "and disclose the cluster (the default), or name a clusters.yaml key to "
            "pin it — a blank string is neither, and guessing which was meant would "
            "put an undisclosed placement decision on the ledger."
        )

    from hpc_agent.infra.clusters import load_clusters_config

    try:
        clusters = load_clusters_config()
    except Exception as exc:  # noqa: BLE001 — an unreadable config is a loud refusal
        raise errors.ConfigInvalid(
            f"queue-run: cannot verify cluster pin {key!r} — clusters.yaml failed to "
            f"load ({exc}). A pin always wins over placement policy, so an unverified "
            "pin would be enqueued as an unchallengeable placement."
        ) from exc
    if not isinstance(clusters, dict):
        raise errors.ConfigInvalid(
            f"queue-run: cannot verify cluster pin {key!r} — clusters.yaml did not "
            f"parse to a mapping of cluster keys (got {type(clusters).__name__})."
        )
    if key in clusters:
        return key

    close = difflib.get_close_matches(key, sorted(str(k) for k in clusters), n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(repr(c) for c in close)}?" if close else ""
    # ClusterUnknown, not SpecInvalid: "key absent from the active clusters.yaml"
    # is the house error for exactly this fact (ops/clusters/describe.py,
    # ops/dir_digest.py, ops/host_retarget.py), and its class remediation —
    # "run `hpc-agent clusters list`" — is the one an operator actually needs.
    # SpecInvalid would carry the generic rebuild-your-spec remediation, which
    # sends the reader to .hpc/tasks.py for a clusters.yaml typo.
    raise errors.ClusterUnknown(
        f"queue-run: cluster pin {key!r} is absent from the active clusters.yaml.{hint} "
        "An explicit pin always wins over placement policy (R5), so nothing downstream "
        "would ever re-choose for this item: enqueueing it would create work no cluster "
        "can claim. Run `hpc-agent clusters list` for the configured keys, or omit "
        "`cluster` to let queue-advance choose and disclose one."
    )


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/queue/intake.jsonl"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.ClusterUnknown,
        errors.ConfigInvalid,
        errors.JournalCorrupt,
    ],
    # Idempotent through the CLIENT's request_id, which is passed to
    # append_jsonl_line as dedup_key: the in-flock probe finds the earlier record
    # and NO second line is written, so a replayed relay returns the original item
    # with replayed=True instead of double-enqueuing it (R8, §10.S2). This is the
    # deliberate opposite of tag-session's honest-duplication posture — a devx
    # ledger may duplicate, a queue may not: a duplicated item is a duplicated
    # submission, and the client is the only party that knows which two calls are
    # the same call.
    idempotent=True,
    idempotency_key="request_id",
    cli=CliShape(
        help=(
            "Enqueue ONE run request onto the per-experiment run queue "
            "(.hpc/queue/intake.jsonl): the resolve spec (inline or by "
            "reference), optional run name, optional explicit cluster pin, "
            "optional campaign base, and typed resource asks (gpu / cores / "
            "walltime / est core-hours). UNGATED - enqueueing spends nothing "
            "and touches no cluster; gates bind later at the cluster boundary "
            "of the run the item becomes, where the brief discloses the "
            "placement the y covers. The item lands in state 'queued'; "
            "queue-advance decides where it goes and queue-status projects "
            "what became of it. An explicit pin is verified against the live "
            "clusters.yaml and a typo is refused here rather than becoming a "
            "silently unplaceable item. The client-minted request_id is the "
            "dedup key, so a replayed relay returns the original item and "
            "writes nothing. Pure local flock-append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=QueueRunSpec,
        schema_ref=SchemaRef(input="queue_run"),
    ),
    agent_facing=True,
)
def queue_run(*, experiment_dir: Path, spec: QueueRunSpec) -> QueueRunResult:
    """Append the item's arrival facts and echo what the ledger now holds."""
    experiment_dir = Path(experiment_dir)
    cluster_pin = _validated_cluster_pin(spec.cluster)

    # The arrival facts, and nothing else (R1). The pin is stored as
    # ``cluster_pin``, NOT as ``cluster``: a placement transition overlays
    # ``cluster``, and writing the pin there would make an operator's REQUEST
    # indistinguishable from queue-advance's disclosed DECISION after one fold.
    #
    # ``run_id`` / ``cmd_sha`` are arrival facts too, not lifecycle: they say
    # WHICH run this item will be (the id is a pure function of run_name and
    # cmd_sha), never that a run exists. Passed through verbatim and never
    # derived here — this verb resolves nothing (R2: arrival spends nothing,
    # including the cost of a resolve), so the only way an item carries its
    # identity is that the caller resolved first, which is exactly what the
    # §10.S3 refill path does.
    record: dict[str, Any] = {
        "spec": spec.spec,
        "spec_ref": spec.spec_ref,
        "run_name": spec.run_name,
        "run_id": spec.run_id,
        "cmd_sha": spec.cmd_sha,
        "cluster_pin": cluster_pin,
        "campaign_base": spec.campaign_base,
        "resources": spec.resources.model_dump(mode="json"),
    }
    replayed = append_intake_item(experiment_dir, record=record, request_id=spec.request_id)

    # Read the ledger back rather than echoing what we meant to write: on a
    # replay the truth is the ORIGINAL record (which may already have been
    # placed), and on a fresh append the fold is what every projection will
    # see. One read also yields the queued depth queue-advance will consider.
    items = read_intake_items(experiment_dir)
    item = find_intake_item(items, spec.request_id)
    if item is None:
        raise errors.JournalCorrupt(
            f"queue-run: appended item_id={spec.request_id!r} to "
            f"{intake_path(experiment_dir)} but the ledger does not read it back. "
            "The intake ledger is the queue's source of truth; echoing a record no "
            "reader can see would report an enqueue that never happened."
        )

    return QueueRunResult(
        path=str(intake_path(experiment_dir)),
        item_id=spec.request_id,
        request_id=spec.request_id,
        # Guaranteed to be in {queued, placed}: the fold drops any record whose
        # state is outside the vocabulary R1 fixes.
        state=str(item.get("state") or STATE_QUEUED),  # type: ignore[arg-type]
        replayed=replayed is not None,
        # Read off the FOLD, never echoed from the spec. On a replay the ledger's
        # identity is the authority: a caller re-sending a request_id with a
        # different resolution must see what the original item committed to
        # rather than its own input handed back. Null for an item that arrived
        # unresolved and has learned nothing since.
        run_id=item_run_id(item),
        cmd_sha=item_cmd_sha(item),
        enqueued_at=str(item.get("enqueued_at") or ""),
        queued_count=len(items_in_states(items, [STATE_QUEUED])),
        record=item,
    )

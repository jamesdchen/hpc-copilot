"""Data-movement orchestration around a sweep (#232).

Orchestration *over* the existing transfer tools (``infra/transport.py``:
rsync/scp/tar), not a new transfer mechanism. The profile-independent core
— a content manifest + verify-against-manifest — lives in :mod:`.manifest`;
the per-profile stage-in / stage-out bracket shape (one shared dataset vs
per-task shards vs stage-out-heavy) waits on the data profile.
"""

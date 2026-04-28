"""Smoke test for the manifest-recovery recipe documented in ``slash_commands/commands/status.md``.

The recipe — "if the local manifest is lost, pull it back from the cluster" —
is driven entirely from the slash-command prompt; there is no dedicated CLI.
What we can pin here is that the underlying primitive (``rsync_pull``) behaves
predictably:

* Happy path: rsync returns 0 and the file appears locally.
* Unhappy path: rsync returns non-zero and the caller can detect/handle it
  gracefully without the test harness raising.

We do NOT exercise the real network — ``subprocess.run`` is monkeypatched so
no external ``rsync`` binary is invoked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hpc_mapreduce.infra.remote import rsync_pull


class _FakeRsync:
    """Callable replacement for ``subprocess.run`` that simulates rsync.

    When ``source_manifest`` exists on the fake "remote", copy it into the
    local destination and return rc=0.  Otherwise return rc=23 with an
    rsync-flavoured stderr, mirroring real rsync's behaviour for missing src.
    """

    def __init__(self, remote_subdir: Path) -> None:
        self.remote_subdir = remote_subdir
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))

        # Last two positional args to rsync are src, dst.  Src looks like
        # ``user@host:/remote/path/sub/`` — we only care that it was built.
        dst = argv[-1]
        local_dst = Path(dst.rstrip("/"))
        local_dst.mkdir(parents=True, exist_ok=True)

        manifest_src = self.remote_subdir / "_hpc_dispatch.json"
        if not manifest_src.is_file():
            return subprocess.CompletedProcess(
                argv,
                returncode=23,  # rsync code for partial/missing transfer
                stdout="",
                stderr=(
                    f'rsync: link_stat "{manifest_src}" failed: No such file or directory (2)\n'
                ),
            )

        shutil.copy2(manifest_src, local_dst / manifest_src.name)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")


class TestRsyncPullHappyPath:
    def test_rsync_pull_copies_manifest_locally(
        self,
        tmp_path: Path,
        monkeypatch: Any,
    ) -> None:
        # Fake "cluster" layout.
        remote_root = tmp_path / "cluster" / "project"
        remote_subdir = remote_root / "run"
        remote_subdir.mkdir(parents=True)
        (remote_subdir / "_hpc_dispatch.json").write_text('{"schema_version": 2}')

        local_dir = tmp_path / "local"

        fake = _FakeRsync(remote_subdir=remote_subdir)
        monkeypatch.setattr(subprocess, "run", fake)

        result = rsync_pull(
            host="fake.cluster",
            user="tester",
            remote_path=str(remote_root),
            remote_subdir="run",
            local_dir=local_dir,
        )

        assert result.returncode == 0, result.stderr
        local_manifest = local_dir / "_hpc_dispatch.json"
        assert local_manifest.is_file(), (
            f"rsync_pull did not produce the local manifest: {list(local_dir.iterdir())}"
        )
        assert local_manifest.read_text() == '{"schema_version": 2}'
        # And the mock saw exactly one rsync invocation.
        assert len(fake.calls) == 1
        assert fake.calls[0][0] == "rsync"


class TestRsyncPullMissingSource:
    def test_missing_manifest_returns_nonzero_and_caller_handles_gracefully(
        self,
        tmp_path: Path,
        monkeypatch: Any,
    ) -> None:
        # Fake "cluster" exists but has no manifest inside the requested subdir.
        remote_root = tmp_path / "cluster" / "project"
        remote_subdir = remote_root / "run"
        remote_subdir.mkdir(parents=True)
        # intentionally: no _hpc_dispatch.json

        local_dir = tmp_path / "local"

        fake = _FakeRsync(remote_subdir=remote_subdir)
        monkeypatch.setattr(subprocess, "run", fake)

        # The primitive itself must not raise — it just returns non-zero
        # and leaves error handling to the caller (the /status prompt).
        error_seen: str | None = None
        try:
            result = rsync_pull(
                host="fake.cluster",
                user="tester",
                remote_path=str(remote_root),
                remote_subdir="run",
                local_dir=local_dir,
            )
        except Exception as exc:  # pragma: no cover — must not happen
            raise AssertionError(
                f"rsync_pull must not raise on missing remote file: {exc!r}"
            ) from exc

        assert result.returncode != 0
        error_seen = result.stderr
        assert error_seen is not None
        assert "No such file or directory" in error_seen

        # The local manifest was NOT materialised.
        assert not (local_dir / "_hpc_dispatch.json").exists()

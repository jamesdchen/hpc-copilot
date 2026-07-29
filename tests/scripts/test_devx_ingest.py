"""``devx_ingest`` — the repo-side collector joins transcripts to tag ledgers.

What matters here is the JOIN: the munged project directory name is lossy
(``/`` → ``-`` collides with real hyphens), so the collector must recover
each project's cwd from the ``cwd`` field INSIDE its transcripts, then read
that directory's ``.hpc/devx/session_tags.jsonl``. Plus the ingestion
posture shared with the product-side reader: torn/foreign lines skip, and
untagged projects stay out of the default report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from devx_ingest import collect  # noqa: E402


def _project(projects: Path, name: str, cwd: Path | None, session_id: str = "sess-1") -> Path:
    proj = projects / name
    proj.mkdir(parents=True)
    lines = [json.dumps({"type": "queue-operation", "sessionId": session_id})]
    if cwd is not None:
        lines.append(json.dumps({"type": "user", "cwd": str(cwd), "sessionId": session_id}))
    (proj / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return proj


def _ledger(experiment: Path, records: list[dict]) -> Path:
    path = experiment / ".hpc" / "devx" / "session_tags.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_joins_cwd_recovered_from_transcript_to_the_ledger(tmp_path: Path) -> None:
    experiment = tmp_path / "my-lab-repo"  # real hyphens: name-munging alone can't recover this
    experiment.mkdir()
    _ledger(experiment, [{"ts": "t", "tags": ["friction"]}])
    projects = tmp_path / "projects"
    _project(projects, "-tmp-my-lab-repo", experiment)

    report = collect(projects, include_untagged=False)

    assert len(report) == 1
    entry = report[0]
    assert entry["cwd"] == str(experiment)
    assert [r["tags"] for r in entry["tags"]] == [["friction"]]
    assert [s["session_id"] for s in entry["sessions"]] == ["sess-1"]


def test_untagged_projects_excluded_by_default_included_with_all(tmp_path: Path) -> None:
    experiment = tmp_path / "exp"
    experiment.mkdir()  # no ledger
    projects = tmp_path / "projects"
    _project(projects, "-tmp-exp", experiment)

    assert collect(projects, include_untagged=False) == []
    report = collect(projects, include_untagged=True)
    assert len(report) == 1 and report[0]["tags"] == []


def test_torn_ledger_lines_and_cwdless_projects_degrade(tmp_path: Path) -> None:
    experiment = tmp_path / "exp"
    experiment.mkdir()
    ledger = _ledger(experiment, [{"ts": "t", "tags": ["good"]}])
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"torn": \n')

    projects = tmp_path / "projects"
    _project(projects, "-tmp-exp", experiment)
    _project(projects, "-tmp-orphan", cwd=None, session_id="sess-2")  # no cwd in any record

    report = collect(projects, include_untagged=True)

    by_dir = {Path(e["project_dir"]).name: e for e in report}
    assert [r["tags"] for r in by_dir["-tmp-exp"]["tags"]] == [["good"]]
    orphan = by_dir["-tmp-orphan"]
    assert orphan["cwd"] is None and orphan["tags"] == []  # listed, never fatal

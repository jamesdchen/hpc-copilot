"""Contract + behavioral tests for the generated-artifact merge driver.

Pins ``scripts/merge_generated.py`` and the root ``.gitattributes`` in three
ways:

1. **Manifest lockstep** — the ``merge=generated`` and ``!merge`` lines in the
   root ``.gitattributes`` equal the script's ``FULLY_GENERATED_PATTERNS`` /
   ``SCHEMA_MERGE_UNSET`` constants exactly (both directions), and the
   partially-generated exclusions carry no driver. Root-only: a second
   ``.gitattributes`` (``packs/quant/.gitattributes``) must NOT grow
   ``merge=generated``.
2. **Reality pin (drift guard)** — every ``merge=generated`` entry is an output
   path of a regen script; specifically the effective ``merge=generated`` schema
   set equals exactly what ``build_schemas.py`` emits, so a NEW hand-authored
   composite schema (which the glob would sweep in) turns the pin RED instead of
   being silently keep-ours'd (the silent-data-loss class this unit exists to
   kill).
3. **Fires-AND-passes** — in an isolated tmp git repo (global/system config
   neutralized, path deliberately containing a SPACE to mirror this clone's
   ``CC Allowed`` dir), a both-sides-changed ``merge=generated`` file CONFLICTS
   before ``ensure`` (the loud undeployed state) and keeps OURS cleanly after
   ``ensure`` (the deployed state), with the regen notice naming the path.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
MERGE_GENERATED = SCRIPTS / "merge_generated.py"
ROOT_GITATTRIBUTES = REPO_ROOT / ".gitattributes"
SCHEMAS_DIR = REPO_ROOT / "src" / "hpc_agent" / "schemas"


def _load(path: Path, name: str) -> ModuleType:
    # scripts/ is not a package; load by file location (the repo's convention,
    # e.g. tests/cli/test_cli_dispatcher_inline_parity.py).
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mg = _load(MERGE_GENERATED, "_merge_generated_under_test")


def _parse_attr_lines(text: str) -> list[tuple[str, list[str]]]:
    """Return (pattern, [attr-tokens]) for each non-comment .gitattributes line."""
    out: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out.append((parts[0], parts[1:]))
    return out


def _patterns_with_attr(text: str, attr: str) -> set[str]:
    return {pat for pat, attrs in _parse_attr_lines(text) if attr in attrs}


# --------------------------------------------------------------------------- #
# 1. Manifest lockstep
# --------------------------------------------------------------------------- #


class TestManifestLockstep:
    def test_merge_generated_lines_equal_patterns(self):
        text = ROOT_GITATTRIBUTES.read_text(encoding="utf-8")
        assert _patterns_with_attr(text, "merge=generated") == set(mg.FULLY_GENERATED_PATTERNS)

    def test_merge_unset_lines_equal_constant(self):
        text = ROOT_GITATTRIBUTES.read_text(encoding="utf-8")
        assert _patterns_with_attr(text, "!merge") == set(mg.SCHEMA_MERGE_UNSET)

    def test_partially_generated_excluded_carry_no_driver(self):
        # None of the partially-generated files may resolve to the driver, and
        # none may appear in the fully-generated pattern set.
        for pattern in mg.PARTIALLY_GENERATED_EXCLUDED:
            assert pattern not in mg.FULLY_GENERATED_PATTERNS
        # Representative real files: README + one primitive doc.
        for rel in ("docs/primitives/README.md", "docs/primitives/interview.md"):
            resolved = _check_attr_merge(REPO_ROOT, rel)
            assert resolved != "generated", f"{rel} unexpectedly carries the driver"

    def test_no_other_gitattributes_carries_merge_generated(self):
        for path in REPO_ROOT.rglob(".gitattributes"):
            if path.resolve() == ROOT_GITATTRIBUTES.resolve():
                continue
            text = path.read_text(encoding="utf-8")
            assert "merge=generated" not in text, (
                f"{path} carries merge=generated — only the root file may"
            )


# --------------------------------------------------------------------------- #
# 2. Reality pin (drift guard): manifest entries are regen-script outputs
# --------------------------------------------------------------------------- #


class TestManifestMatchesRegenOutputs:
    def test_singleton_entries_are_regen_outputs(self):
        bake = _load(SCRIPTS / "bake_operations_json.py", "_bake_ops_under_test")
        vmm = _load(SCRIPTS / "build_verb_module_map.py", "_vmm_under_test")
        idx = _load(SCRIPTS / "build_operations_index.py", "_ops_index_under_test")

        assert (
            bake.OUTPUT_PATH.resolve().relative_to(REPO_ROOT).as_posix()
            == "src/hpc_agent/operations.json"
        )
        assert (
            vmm._TARGET.resolve().relative_to(REPO_ROOT).as_posix()
            == "src/hpc_agent/cli/_verb_module_map.py"
        )
        # docs/generated/** must cover build_operations_index's output.
        out_rel = idx.OUT.resolve().relative_to(REPO_ROOT).as_posix()
        assert out_rel.startswith("docs/generated/")
        assert "docs/generated/**" in mg.FULLY_GENERATED_PATTERNS

    def test_schema_glob_matches_build_schemas_emitted_exactly(self):
        """The effective merge=generated schema set == build_schemas emitted set.

        This is the drift guard: the ``src/hpc_agent/schemas/*.json`` glob would
        otherwise silently sweep in hand-authored composite schemas that no
        regen script restores. SCHEMA_MERGE_UNSET must be EXACTLY the top-level
        schemas that build_schemas does not emit.
        """
        bs = _load(SCRIPTS / "build_schemas.py", "_build_schemas_under_test")
        emitted: set[str] = set()
        targets = [(s, f, sd) for s, f, sd in bs.SCHEMA_REGISTRY]
        targets += [(e[0], e[1], e[2]) for e in bs.DERIVED_REGISTRY]
        for _src, fname, schemas_dir in targets:
            path = (schemas_dir / fname).resolve()
            if path.parent == SCHEMAS_DIR.resolve():  # top-level only
                emitted.add(path.name)

        present = {p.name for p in SCHEMAS_DIR.glob("*.json")}
        unset_names = {Path(p).name for p in mg.SCHEMA_MERGE_UNSET}

        # The exclusion list is exactly the non-emitted top-level schemas.
        assert unset_names == (present - emitted)
        # Everything the glob keeps as merge=generated is a build_schemas output.
        assert (present - unset_names) == emitted


# --------------------------------------------------------------------------- #
# 3. Fires-AND-passes behavioral pair (+ ensure/check unit tests)
# --------------------------------------------------------------------------- #


def _neutral_env(tmp_path: Path) -> dict[str, str]:
    """A git env with global/system config neutralized for reproducibility."""
    gcfg = tmp_path / "global.gitconfig"
    scfg = tmp_path / "system.gitconfig"
    gcfg.write_text("", encoding="utf-8")
    scfg.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = str(gcfg)
    env["GIT_CONFIG_SYSTEM"] = str(scfg)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # Neutralize a possibly-set attributes file so only the repo one applies.
    env.pop("GIT_ATTR_NOSYSTEM", None)
    return env


def _git(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _check_attr_merge(cwd: Path, rel: str) -> str:
    result = subprocess.run(
        ["git", "check-attr", "merge", "--", rel],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # Output: "<path>: merge: <value>"
    return result.stdout.rsplit(":", 1)[-1].strip()


@pytest.fixture
def probe_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A tmp git repo whose path contains a SPACE (mirrors 'CC Allowed').

    Sets up a ``probe.json`` marked ``merge=generated`` with divergent edits on
    ``main`` (ours) and ``feat`` (theirs), and the driver script fixture-copied
    into ``<repo>/scripts/`` so the relative driver command can exec there
    (precedent: tests/infra/test_audit_fixes.py copies the script into the
    fixture repo).
    """
    env = _neutral_env(tmp_path)
    repo = tmp_path / "cc allowed repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(MERGE_GENERATED, repo / "scripts" / MERGE_GENERATED.name)

    assert _git(["init", "-b", "main", "."], repo, env).returncode == 0
    _git(["config", "user.name", "t"], repo, env)
    _git(["config", "user.email", "t@t"], repo, env)

    (repo / ".gitattributes").write_text("probe.json merge=generated\n", encoding="utf-8")
    (repo / "probe.json").write_text('{"v": 0}\n', encoding="utf-8")
    _git(["add", "-A"], repo, env)
    assert _git(["commit", "-m", "base"], repo, env).returncode == 0

    _git(["checkout", "-b", "feat"], repo, env)
    (repo / "probe.json").write_text('{"v": 2, "theirs": true}\n', encoding="utf-8")
    _git(["commit", "-am", "theirs"], repo, env)

    _git(["checkout", "main"], repo, env)
    (repo / "probe.json").write_text('{"v": 1, "ours": true}\n', encoding="utf-8")
    _git(["commit", "-am", "ours"], repo, env)
    return repo, env


def _ensure(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/merge_generated.py", "ensure"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class TestBehavioralFiresAndPasses:
    def test_undeployed_conflicts(self, probe_repo):
        repo, env = probe_repo
        # Driver not configured: the declared-but-undefined attribute degrades
        # to a normal text merge and CONFLICTS (loud, never silent).
        result = _git(["merge", "feat", "-m", "m"], repo, env)
        assert result.returncode != 0, "undeployed driver should conflict loudly"
        _git(["merge", "--abort"], repo, env)

    def test_deployed_keeps_ours(self, probe_repo):
        repo, env = probe_repo
        assert _ensure(repo, env).returncode == 0
        result = _git(["merge", "feat", "-m", "m"], repo, env)
        assert result.returncode == 0, f"deployed merge should succeed; stderr={result.stderr}"
        # Kept OURS byte-for-byte.
        assert (repo / "probe.json").read_text(encoding="utf-8") == '{"v": 1, "ours": true}\n'
        # The regen notice names the path.
        assert "probe.json" in result.stderr
        assert "regen" in result.stderr.lower()

    def test_ensure_is_idempotent(self, probe_repo):
        repo, env = probe_repo
        assert _ensure(repo, env).returncode == 0
        first = _git(["config", "--get", "merge.generated.driver"], repo, env).stdout
        assert _ensure(repo, env).returncode == 0
        second = _git(["config", "--get", "merge.generated.driver"], repo, env).stdout
        assert first == second and first.strip() != ""

    def test_check_reflects_deployment(self, probe_repo):
        repo, env = probe_repo

        def _check() -> int:
            return subprocess.run(
                [sys.executable, "scripts/merge_generated.py", "check"],
                cwd=str(repo),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).returncode

        assert _check() == 1  # virgin repo: driver missing
        assert _ensure(repo, env).returncode == 0
        assert _check() == 0  # installed


class TestUnitBehaviors:
    def test_driver_keeps_ours_and_exits_zero(self, capsys):
        # driver %O %A %B %P — main() reads argv[5] (== P) for the notice and
        # never touches %A, so git keeps ours.
        rc = mg.main(["driver", "O", "A", "B", "some/generated/file.json"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "some/generated/file.json" in err
        assert "merge=generated" in err

    def test_notice_names_both_regen_all_and_the_six_scripts(self):
        notice = mg._regen_notice("x.json")
        assert "scripts/regen_all.py" in notice
        for script in mg._SIX_REGEN_SCRIPTS:
            assert script in notice
        assert "merge or rebase" in notice.lower()

    def test_driver_command_is_sh_quoted(self):
        # The clone path contains a space; the interpreter must be sh-quoted so
        # git's `sh -c` does not split it, and the script path stays relative.
        import shlex

        cmd = mg._driver_command()
        assert cmd.endswith("scripts/merge_generated.py driver %O %A %B %P")
        # First shell token round-trips to sys.executable exactly (quoting held).
        assert shlex.split(cmd)[0] == sys.executable

    def test_unknown_subcommand_is_loud(self, capsys):
        assert mg.main(["frobnicate"]) == 2
        assert "unknown subcommand" in capsys.readouterr().err

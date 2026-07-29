"""Contract: a workflow plan's relayed commands are validated by EXECUTION.

Its sibling (`test_workflow_plan_delegation.py`) is a regex sweep: it asks
*which verb* a plan names and answers from the registry. That question is
necessary and insufficient — it cannot see a FLAG, and it never runs anything.
Two bugs shipped straight through it (both found 2026-07-29):

* **the flag bug.** `campaign-run.js`'s shared `specRun` helper appended
  `--experiment-dir` to EVERY relayed verb. `wait-detached`'s
  :class:`~hpc_agent.cli._dispatch.CliShape` sets ``experiment_dir_arg=False``,
  so argparse answered ``unrecognized arguments`` with rc=2 and the plan parked
  every real detached block as ``wait_failed``. `campaign-recon.js` carried the
  same bug on `net-triage`. The delegation sweep saw a legal query verb and
  passed;
* **the envelope bug.** The CLI's stdout is an ENVELOPE
  (``hpc_agent/cli/_helpers.py``: ``{ok, idempotent, data}`` on success,
  ``{ok: false, error_code, ...}`` on refusal), and the plan's parser returned
  the whole envelope while the loop read ``tick.action`` / ``tick.brief`` off
  the envelope ROOT — ``undefined`` for every field, so the park branches could
  never fire. Nothing textual was wrong; the SHAPE was.

So this module validates the two things a regex cannot, against the real
producers on the Python side:

1. every plan declares its relay table (``const RELAYS``) as strict JSON, and
   each row is MATERIALIZED into an argv and handed to the REAL argparse tree
   (:func:`hpc_agent.cli.parser.build_parser`) — parse only, no execution, no
   cluster. An unsupported flag, a missing required flag, or an unknown verb
   fails here;
2. every plan that reads CLI output does it in ONE helper that unwraps the
   envelope's payload member and branches on its ok member — with both key
   names DERIVED from an envelope Python actually emits, never copied. Where a
   JS runtime is available the helper is additionally EXECUTED against those
   real envelope bytes, so "it mentions `.data`" is not mistaken for "it
   returns the payload".

The relay table is not documentation: each plan RENDERS its commands from it
(``relayCommand``), so what this test validates and what the plan runs are the
same rows. Which is a claim, and claims get pinned: rule 1 above materializes
argv as ``--{flag}``, but the plan renders through ``FLAG_RENDER``, so a
renderer that spells a declared flag differently ships green past a table that
parses perfectly — the flag bug's exact signature, one layer down. So a third
rule binds the renderer to the table:

3. every ``FLAG_RENDER`` entry renders the flag the table NAMES — pinned
   statically (the spelling appears in the renderer block) and, under node,
   by CALLING each renderer and reading what it returned.

And the plan's own load-time gate, ``validatePlan()``, is executed rather than
admired: a gate no test ever runs is a gate that can be wrong for free.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.registry.primitive import get_registry, register_primitives
from tests._node import run_node
from tests._workflow_plans import WORKFLOWS_DIR, plan_paths, portable_prefix

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from hpc_agent._kernel.registry.primitive import PrimitiveMeta

_WORKFLOWS_DIR = WORKFLOWS_DIR

#: The rendezvous verb, locked in every plan section (rule 2). Asked of the
#: relay TABLE here — the delegation sweep asks it of the rendered text, which
#: no longer names a literal verb now that every command is table-rendered.
_RENDEZVOUS = "append-decision"

#: ``const RELAYS = { ... }`` — the plan's relay table, authored as strict JSON
#: (see each plan's comment) so it is read with :func:`json.loads` rather than
#: guessed at. Terminated by the closing brace at column 0, the same top-level
#: block shape ``test_workflow_plan_delegation`` parses.
_RELAYS_RE = re.compile(r"^const\s+RELAYS\s*=\s*(\{.*?^\})", re.S | re.M)

#: ``const parseEnvelope = (output) => { ... }`` — the single envelope reader.
_ENVELOPE_FN_RE = re.compile(r"^(const\s+parseEnvelope\s*=.*?^\})", re.S | re.M)

#: ``const FLAG_RENDER = { ... }`` — one renderer per declared flag. NOT JSON
#: (the values are arrow functions), so it is only ever grepped here and
#: EXECUTED below; nothing reconstructs it in Python.
_FLAG_RENDER_RE = re.compile(r"^const\s+FLAG_RENDER\s*=\s*\{.*?^\}", re.S | re.M)

#: Dummy values for a materialized argv. argparse only TYPE-converts at parse
#: time (``--spec`` is a ``pathlib.Path``, ``--experiment-dir`` a path string),
#: so nothing here has to exist and nothing is executed.
_DUMMY = "/nonexistent/contract-probe"


# --------------------------------------------------------------------------
# The shared checkers (module-level by design: the real-plan sweep and the
# synthetic fire paths must exercise the SAME code).
# --------------------------------------------------------------------------


def relay_table(text: str) -> dict[str, Any]:
    """The plan's declared relay table, or ``{}`` when it declares none."""
    match = _RELAYS_RE.search(text)
    if match is None:
        return {}
    return json.loads(match.group(1))


def _verb_parser(parser: argparse.ArgumentParser, verb: str) -> argparse.ArgumentParser | None:
    """The subparser argparse built for *verb*, or ``None`` if there is none.

    Reaches through ``_SubParsersAction`` because that is the only way to ask
    argparse what a subcommand declares — and asking the real tree is the whole
    point: a second table of "flags this verb takes" would be the copy that
    drifted.
    """
    for action in parser._actions:  # noqa: SLF001 — argparse exposes no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices.get(verb)
    return None


def check_relays(table: Mapping[str, Any], parser: argparse.ArgumentParser) -> list[str]:
    """Return the parse failures in one plan's relay table.

    Empty list == every declared command parses against the real CLI. Each row
    is materialized with dummy values and run through ``parse_args``; argparse
    exits 2 on an unrecognized or missing flag, which is captured and reported
    with argparse's own message.
    """
    problems: list[str] = []
    for key, row in table.items():
        if not isinstance(row, dict):
            problems.append(f"RELAYS.{key}: not an object")
            continue
        verb = row.get("verb")
        flags = row.get("flags")
        if not isinstance(verb, str) or not verb:
            problems.append(f"RELAYS.{key}: no verb")
            continue
        if not isinstance(flags, list) or any(not isinstance(f, str) for f in flags):
            problems.append(f"RELAYS.{key}: flags must be a list of strings")
            continue
        sub = _verb_parser(parser, verb)
        if sub is None:
            problems.append(f"RELAYS.{key}: `hpc-agent {verb}` is not a CLI verb")
            continue
        # A store_true flag takes no value; ask the REAL action, don't assume.
        options = {opt: action for action in sub._actions for opt in action.option_strings}
        argv = [verb]
        for flag in flags:
            action = options.get(f"--{flag}")
            takes_value = action is None or action.nargs != 0
            argv += [f"--{flag}", _DUMMY] if takes_value else [f"--{flag}"]
        stderr = io.StringIO()
        try:
            # The verb's OWN subparser, so a refusal reports that verb's compact
            # usage line rather than the top-level parser's ~180-choice dump.
            # Reaching the subparser at all went through the real tree above, so
            # this is still the shipped parser, not a reconstruction.
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                sub.parse_args(argv[1:])
        except SystemExit:
            detail = " ".join(stderr.getvalue().split()) or "(argparse exited without a message)"
            problems.append(f"RELAYS.{key}: `hpc-agent {' '.join(argv)}` does not parse — {detail}")
    return problems


def check_envelope_reader(text: str, *, payload_key: str, ok_key: str) -> list[str]:
    """Return the envelope-shape violations in one plan's source.

    A plan that never parses CLI output (a pure relay-verbatim sweep) is
    conforming vacuously — the rule binds the moment it parses one.
    """
    problems: list[str] = []
    parses = text.count("JSON.parse(")
    if parses == 0:
        return problems
    reader = _ENVELOPE_FN_RE.search(text)
    if reader is None:
        problems.append(
            "parses CLI output but defines no `parseEnvelope` helper — every envelope "
            "must be read through the one helper this contract can pin"
        )
        return problems
    body = reader.group(1)
    if parses != 1 or "JSON.parse(" not in body:
        problems.append(
            f"has {parses} JSON.parse call(s); exactly one is allowed and it must live "
            "in `parseEnvelope` — a second parse site is an envelope nobody unwrapped"
        )
    reads_payload = any(
        access in body for access in (f".{payload_key}", f"['{payload_key}']", f'["{payload_key}"]')
    )
    if not reads_payload:
        problems.append(
            f"`parseEnvelope` never reads the envelope's {payload_key!r} member: the CLI "
            f"wraps every result in {{{ok_key}, ..., {payload_key}}}, so a field read off "
            "the envelope root is undefined for EVERY field"
        )
    if f"{ok_key}" not in body:
        problems.append(
            f"`parseEnvelope` never branches on the envelope's {ok_key!r} member: a "
            "refusal envelope carries no payload and must not read as an empty result"
        )
    return problems


# --------------------------------------------------------------------------
# The real envelope, from the real producer
# --------------------------------------------------------------------------

_PROBE = {"contract_probe": "workflow-plan-commands"}


def _capture(emit: object) -> dict[str, Any]:
    """Run *emit* and return the single JSON envelope it printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        emit()  # type: ignore[operator]
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one envelope line, got {lines!r}"
    return json.loads(lines[0])


@pytest.fixture(scope="module")
def ok_envelope() -> dict[str, Any]:
    """A REAL success envelope, produced by the function every verb emits through."""
    from hpc_agent.cli._helpers import _ok

    return _capture(lambda: _ok(dict(_PROBE), idempotent=False))


@pytest.fixture(scope="module")
def err_envelope() -> dict[str, Any]:
    """A REAL refusal envelope, produced by the same module's error path."""
    from hpc_agent.cli._helpers import _err

    return _capture(
        lambda: _err(
            error_code="spec_invalid",
            message="contract probe",
            category="spec_invalid",
            retry_safe=False,
        )
    )


@pytest.fixture(scope="module")
def envelope_keys(ok_envelope: dict[str, Any], err_envelope: dict[str, Any]) -> tuple[str, str]:
    """``(payload_key, ok_key)``, DERIVED from the live envelopes.

    The payload key is the one whose value IS the probe payload; the ok key is
    the one whose value is ``True`` on the success envelope and ``False`` on the
    refusal (``idempotent=False`` above is what makes that unambiguous). Nothing
    here is a copied literal, so a Python-side rename fails this test loudly
    instead of silently un-pinning the JS check.
    """
    payload = [k for k, v in ok_envelope.items() if v == _PROBE]
    assert len(payload) == 1, f"cannot identify the payload key in {ok_envelope!r}"
    ok = [
        k
        for k, v in ok_envelope.items()
        if v is True and k != payload[0] and err_envelope.get(k) is False
    ]
    assert len(ok) == 1, f"cannot identify the ok key in {ok_envelope!r}/{err_envelope!r}"
    return payload[0], ok[0]


@pytest.fixture(scope="module")
def cli_parser() -> argparse.ArgumentParser:
    """The REAL top-level argparse tree — every verb, every flag, as shipped."""
    from hpc_agent.cli.parser import build_parser

    register_primitives()
    return build_parser()


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    register_primitives()
    return get_registry()


def _plan_paths() -> list[Path]:
    return plan_paths()


def _flag_render_block(text: str) -> str | None:
    """The plan's ``FLAG_RENDER`` source block, or ``None`` if it declares none."""
    match = _FLAG_RENDER_RE.search(text)
    return None if match is None else match.group(0)


# --------------------------------------------------------------------------
# The real plans
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_plan_declares_a_relay_table(path: Path) -> None:
    """Every plan declares its commands as data. A plan that hand-rolls an
    `hpc-agent` string instead is invisible to the parse gate below, which is
    exactly how the flag bug shipped."""
    table = relay_table(path.read_text(encoding="utf-8"))
    assert table, (
        f"{path.name}: no `const RELAYS = {{...}}` table — every relayed command must be "
        "rendered from a declared row so this contract validates what actually runs"
    )


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_every_relayed_command_parses_against_the_real_cli(
    path: Path, cli_parser: argparse.ArgumentParser
) -> None:
    """THE EXECUTION GATE. Each declared command is materialized with dummy
    values and parsed by the shipped argparse tree — no cluster, no side
    effects, just the parse step the CLI itself runs first."""
    problems = check_relays(relay_table(path.read_text(encoding="utf-8")), cli_parser)
    assert not problems, f"{path.name}: " + "; ".join(problems)


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_relay_table_verbs_resolve_and_exclude_the_rendezvous(
    path: Path, registry: dict[str, PrimitiveMeta]
) -> None:
    """The rule-2 lock, asked of the TABLE. Now that commands are rendered from
    it, the table — not the rendered text — is where a verb name lives, so this
    is where the rendezvous lock has to bind."""
    table = relay_table(path.read_text(encoding="utf-8"))
    verbs = [row["verb"] for row in table.values()]
    assert _RENDEZVOUS not in verbs, (
        f"{path.name}: relays `hpc-agent {_RENDEZVOUS}` — the read-and-sign rendezvous is "
        "locked in every plan, with no rule-5 exception (docs/design/agent-delegation.md rule 2)"
    )
    unknown = [verb for verb in verbs if verb not in registry]
    assert not unknown, (
        f"{path.name}: relay table names {unknown} which resolve to no @primitive in the "
        "live registry — a plan relays registry verbs, never a Tier-3 or invented one"
    )


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_plan_reads_the_envelope_payload_layer(path: Path, envelope_keys: tuple[str, str]) -> None:
    """THE SHAPE GATE (static arm). A plan that parses CLI output must unwrap
    the payload member and branch on the ok member — both key names derived
    from the envelope Python emits, not copied here."""
    payload_key, ok_key = envelope_keys
    problems = check_envelope_reader(
        path.read_text(encoding="utf-8"), payload_key=payload_key, ok_key=ok_key
    )
    assert not problems, f"{path.name}: " + "; ".join(problems)


# --------------------------------------------------------------------------
# The shape gate, executed
# --------------------------------------------------------------------------

_HARNESS_TAIL = (
    "\nconst out = parseEnvelope(process.argv[2])\n"
    "process.stdout.write(JSON.stringify(out === undefined ? null : out))\n"
)


def _run_reader(tmp_path: Path, reader: str, stdout_text: str) -> Any:
    """Execute a plan's `parseEnvelope` over *stdout_text* and return its result."""
    proc = run_node(tmp_path / "reader.mjs", reader + _HARNESS_TAIL, stdout_text)
    assert proc.returncode == 0, f"parseEnvelope threw: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_envelope_reader_returns_the_payload_not_the_envelope(
    path: Path,
    tmp_path: Path,
    ok_envelope: dict[str, Any],
    err_envelope: dict[str, Any],
    envelope_keys: tuple[str, str],
) -> None:
    """THE SHAPE GATE (executed arm). The plan's own helper is run against the
    bytes Python actually emitted. A helper that returns the ENVELOPE — the
    shipped bug — fails here even though it mentions every right word."""
    payload_key, ok_key = envelope_keys
    text = path.read_text(encoding="utf-8")
    match = _ENVELOPE_FN_RE.search(text)
    if match is None:
        pytest.skip(f"{path.name} parses no CLI output")
    reader = match.group(1)

    ok_line = json.dumps(ok_envelope, sort_keys=True)
    result = _run_reader(tmp_path, reader, f"some log noise\n{ok_line}\n")
    assert result is not None, f"{path.name}: parseEnvelope returned null for a real ok envelope"
    # The helper returns its OWN record, which MIRRORS the envelope's member
    # names on purpose ({ok, data, error} — see each plan's comment). Indexing
    # it by the Python-derived key names below relies on that mirroring, so say
    # it out loud and check it rather than letting a rename read as a null.
    assert {ok_key, payload_key} <= set(result), (
        f"{path.name}: parseEnvelope returned {sorted(result)}, which does not carry the "
        f"envelope's {ok_key!r}/{payload_key!r} member names — the reader's record is "
        "supposed to mirror them so the loop reads one vocabulary end to end"
    )
    assert result[ok_key] is True, f"{path.name}: real ok envelope did not read as ok"
    assert result[payload_key] == ok_envelope[payload_key], (
        f"{path.name}: parseEnvelope returned {result[payload_key]!r}, not the envelope's "
        f"{payload_key!r} member {ok_envelope[payload_key]!r} — it is handing the loop the "
        "envelope root, where every result field reads undefined"
    )

    err_line = json.dumps(err_envelope, sort_keys=True)
    refused = _run_reader(tmp_path, reader, f"{err_line}\n")
    assert refused is not None, f"{path.name}: parseEnvelope returned null for a real refusal"
    assert refused[ok_key] is False, (
        f"{path.name}: a refusal envelope did not read as not-ok — a refusal carries no "
        "payload and must never read as an empty result"
    )


# --------------------------------------------------------------------------
# The RENDERER gate — the table is validated, but the renderer is what runs
# --------------------------------------------------------------------------
#
# Rule 1 above materializes each row's argv as ``--{flag}`` and hands it to
# argparse. The plan does not: it renders through ``FLAG_RENDER[flag](inputs)``.
# Those are two spellings of the same flag, and only one of them ships. Change a
# renderer to emit ``--experimentdir`` and the parse gate stays green while every
# relayed command is rc=2 — Bug A again, one layer below where it was fixed.
#
# So the renderer is pinned TO the table: whatever ``FLAG_RENDER`` returns for a
# flag must begin with that flag as the table spells it.


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_every_declared_flag_is_spelled_in_the_renderer_block(path: Path) -> None:
    """THE RENDERER GATE (static arm). Runs everywhere, catches the spelling."""
    text = path.read_text(encoding="utf-8")
    block = _flag_render_block(text)
    flags = {flag for row in relay_table(text).values() for flag in row.get("flags", [])}
    if not flags:
        pytest.skip(f"{path.name} declares no flags")
    assert block is not None, (
        f"{path.name}: declares flags {sorted(flags)} but no `FLAG_RENDER = {{...}}` block — "
        "the parse gate would then be validating a table nothing renders from"
    )
    missing = sorted(flag for flag in flags if f"--{flag}" not in block)
    assert not missing, (
        f"{path.name}: FLAG_RENDER never spells {missing} the way RELAYS declares them — a "
        "renderer that emits a different flag parses nowhere (`--experimentdir` is rc=2), "
        "and the parse gate above cannot see it because it renders argv itself"
    )


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_every_flag_renderer_emits_the_flag_the_table_names(path: Path, tmp_path: Path) -> None:
    """THE RENDERER GATE (executed arm). Each renderer is CALLED and its output
    read: a spelling that merely appears somewhere in the block is not the same
    as the string the plan hands to the shell."""
    text = path.read_text(encoding="utf-8")
    if _flag_render_block(text) is None:
        pytest.skip(f"{path.name} declares no FLAG_RENDER")
    # A Proxy stands in for `inputs`: renderers destructure whatever they need
    # ({ repo }, …) and this answers every property without the test having to
    # know — and hence without a second copy of each renderer's input contract.
    tail = textwrap.dedent(
        """
        const probe = new Proxy({}, {
          get: (_target, key) => (typeof key === 'symbol' ? undefined : `<${String(key)}>`),
        })
        const out = {}
        for (const [flag, render] of Object.entries(FLAG_RENDER)) out[flag] = render(probe)
        process.stdout.write(JSON.stringify(out))
        """
    )
    proc = run_node(
        tmp_path / f"{path.stem}-render.mjs",
        portable_prefix(text, name=path.name) + tail,
    )
    assert proc.returncode == 0, f"{path.name}: rendering the flags threw: {proc.stderr.strip()}"
    rendered: dict[str, str] = json.loads(proc.stdout)

    declared = {flag for row in relay_table(text).values() for flag in row.get("flags", [])}
    unrendered = sorted(declared - set(rendered))
    assert not unrendered, (
        f"{path.name}: RELAYS declares {unrendered} with no FLAG_RENDER entry — "
        "`relayCommand` would call undefined and the plan would die at its first relay"
    )
    wrong = {
        flag: out
        for flag, out in rendered.items()
        if not (out == f"--{flag}" or out.startswith(f"--{flag} "))
    }
    assert not wrong, (
        f"{path.name}: FLAG_RENDER renders {wrong} — each entry must emit the flag RELAYS "
        "names (`--<flag>` alone or `--<flag> <value>`), because the argv rule 1 parses is "
        "built from the TABLE while the command that ships is built from these renderers"
    )


# --------------------------------------------------------------------------
# The plan's own load-time gate, EXECUTED
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_validate_plan_admits_the_shipped_plan_and_refuses_a_broken_relay_row(
    path: Path, tmp_path: Path
) -> None:
    """``validatePlan()`` runs before anything dispatches and is the plan's only
    self-check — and until now no test ever ran it, so it could have been wrong
    for free (a gate that cannot fire is inertia, not design).

    Both arms in one: the shipped table loads clean, and the two malformed rows
    the gate names in prose — a row with no verb, a flag with no renderer — are
    both reported when appended.
    """
    prefix = portable_prefix(path.read_text(encoding="utf-8"), name=path.name)
    clean = run_node(
        tmp_path / f"{path.stem}-validate-ok.mjs",
        prefix + "\nvalidatePlan()\nprocess.stdout.write('admitted')\n",
    )
    assert clean.returncode == 0 and clean.stdout == "admitted", (
        f"{path.name}: validatePlan() refuses the SHIPPED plan — "
        f"rc={clean.returncode} {clean.stderr.strip()}"
    )

    broken = run_node(
        tmp_path / f"{path.stem}-validate-bad.mjs",
        prefix
        + textwrap.dedent(
            """
            RELAYS.contractProbeNoVerb = { flags: [] }
            RELAYS.contractProbeBadFlag = { verb: 'queue-status', flags: ['contract-probe-flag'] }
            try {
              validatePlan()
              process.stdout.write('ADMITTED A BROKEN TABLE')
            } catch (err) {
              process.stdout.write(err.message)
            }
            """
        ),
    )
    assert broken.returncode == 0, f"{path.name}: probe script died: {broken.stderr.strip()}"
    message = broken.stdout
    assert "contractProbeNoVerb" in message and "no verb" in message, (
        f"{path.name}: validatePlan() admitted a RELAYS row with no verb — `relayCommand` "
        f"would render `hpc-agent undefined ...`. It said: {message!r}"
    )
    assert "contractProbeBadFlag" in message and "contract-probe-flag" in message, (
        f"{path.name}: validatePlan() admitted a flag with no FLAG_RENDER entry — "
        f"`relayCommand` would call undefined at relay time. It said: {message!r}"
    )


# --------------------------------------------------------------------------
# Fire paths — the guards are not decorative
# --------------------------------------------------------------------------


def test_a_missing_js_runtime_fails_the_executed_arms_when_ci_requires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executed arms are the ones a conforming-LOOKING plan cannot pass, so
    "node was absent, everything skipped, suite green" is the failure this
    switch exists to remove. Both halves pinned: a contributor without node
    still gets a skip, CI (which installs node and sets the env) gets a hard
    failure if the runtime ever goes missing."""
    import tests._node as node_mod

    monkeypatch.setattr(node_mod.shutil, "which", lambda _name: None)

    monkeypatch.delenv(node_mod.REQUIRE_NODE_ENV, raising=False)
    assert node_mod.node_is_required() is False
    with pytest.raises(pytest.skip.Exception):
        node_mod.require_node()

    monkeypatch.setenv(node_mod.REQUIRE_NODE_ENV, "1")
    assert node_mod.node_is_required() is True
    with pytest.raises(pytest.fail.Exception, match="no `node` is on PATH"):
        node_mod.require_node()


def test_parse_gate_fires_on_the_shipped_flag_bug(cli_parser: argparse.ArgumentParser) -> None:
    """Bug A, as it shipped: `wait-detached` relayed with `--experiment-dir`.
    The verb's CliShape sets experiment_dir_arg=False, so the real parser
    refuses it — rc=2 in production, a failing row here."""
    problems = check_relays(
        {"waitDetached": {"verb": "wait-detached", "flags": ["spec", "experiment-dir"]}},
        cli_parser,
    )
    assert len(problems) == 1, f"expected exactly the flag refusal, got {problems}"
    assert "wait-detached" in problems[0] and "experiment-dir" in problems[0]


def test_parse_gate_fires_on_a_dropped_required_flag(
    cli_parser: argparse.ArgumentParser,
) -> None:
    """The other half of the same gate: `block-drive --spec` is REQUIRED, so a
    row that forgets it is refused too. Without this arm the gate would only
    catch flags that are too many, never flags that are too few."""
    problems = check_relays(
        {"blockDrive": {"verb": "block-drive", "flags": ["experiment-dir"]}}, cli_parser
    )
    assert len(problems) == 1, f"expected exactly the missing-flag refusal, got {problems}"
    assert "block-drive" in problems[0]


def test_parse_gate_admits_the_fixed_rows(cli_parser: argparse.ArgumentParser) -> None:
    """The control arm: the post-fix table passes clean. A gate that refused
    everything would satisfy both fire paths while enforcing nothing."""
    assert (
        check_relays(
            {
                "waitDetached": {"verb": "wait-detached", "flags": ["spec"]},
                "blockDrive": {"verb": "block-drive", "flags": ["spec", "experiment-dir"]},
                "queueStatus": {"verb": "queue-status", "flags": ["spec", "experiment-dir"]},
            },
            cli_parser,
        )
        == []
    )


#: The envelope reader exactly as it shipped: it returns the PARSED ENVELOPE,
#: so `tick.action` was undefined and no park branch could ever fire (Bug B).
_SHIPPED_READER = textwrap.dedent(
    """\
    const parseEnvelope = (output) => {
      for (const line of output.split('\\n').reverse()) {
        const t = line.trim()
        if (t.startsWith('{') && t.endsWith('}')) {
          try {
            return JSON.parse(t)
          } catch {
          }
        }
      }
      return null
    }
    """
)


def test_shape_gate_fires_on_the_shipped_envelope_bug(
    envelope_keys: tuple[str, str],
) -> None:
    """Bug B, statically: the shipped reader unwraps nothing and branches on
    nothing, so both halves of the shape rule fail."""
    payload_key, ok_key = envelope_keys
    problems = check_envelope_reader(_SHIPPED_READER, payload_key=payload_key, ok_key=ok_key)
    assert len(problems) == 2, f"expected the unwrap + ok-branch refusals, got {problems}"
    assert any(payload_key in p for p in problems)
    assert any(ok_key in p for p in problems)


def test_shape_gate_fires_on_the_shipped_envelope_bug_when_executed(
    tmp_path: Path, ok_envelope: dict[str, Any], envelope_keys: tuple[str, str]
) -> None:
    """Bug B, executed: run the shipped reader over the REAL envelope bytes and
    watch it hand back the envelope root. This is the arm that no amount of
    right-looking text can satisfy."""
    payload_key, _ok_key = envelope_keys
    result = _run_reader(tmp_path, _SHIPPED_READER, json.dumps(ok_envelope, sort_keys=True))
    assert result == ok_envelope, "the shipped reader is supposed to return the whole envelope"
    assert result[payload_key] != result, "sanity: the payload is a strict sub-object"
    # And the consequence the plan actually suffered: every result field is
    # absent from what the loop was handed.
    for field in ok_envelope[payload_key]:
        assert field not in result, (
            f"{field!r} would have been readable off the envelope root — the bug's "
            "premise (result fields live one layer down) no longer holds"
        )


def test_shape_gate_admits_a_conforming_reader(
    envelope_keys: tuple[str, str],
) -> None:
    """The control arm for the shape gate."""
    payload_key, ok_key = envelope_keys
    conforming = textwrap.dedent(
        f"""\
        const parseEnvelope = (output) => {{
          for (const line of output.split('\\n').reverse()) {{
            const t = line.trim()
            if (!t.startsWith('{{') || !t.endsWith('}}')) continue
            let env
            try {{ env = JSON.parse(t) }} catch {{ continue }}
            if (env.{ok_key} === false) return {{ {ok_key}: false, {payload_key}: null }}
            if (env.{ok_key} === true) return {{ {ok_key}: true, {payload_key}: env.{payload_key} }}
          }}
          return null
        }}
        """
    )
    assert check_envelope_reader(conforming, payload_key=payload_key, ok_key=ok_key) == []

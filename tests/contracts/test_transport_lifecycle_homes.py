"""B16 contract: transport-lifecycle vocabulary stays in its declared homes.

The B16 ruling (philosophy audit; upstream-fixes sweep-2 banked spec 6,
schedule pulled forward with RULING 5's G4 shrink, 2026-07-12): hand-rolled
connection-lifecycle MECHANISMS (idle management, keepalive policy,
multiplexing) live library-native in a small set of declared library-boundary
homes; the ban-risk breaker and connection-RATE courtesy are cluster-social
POLICY and live elsewhere. G4's fired symptoms (#8, #35, r12-f16, r12-f24)
were all minted by lifecycle mechanism growing OUTSIDE those homes.

This pin is a lockstep set-equality over MANAGEMENT/ASSEMBLY tokens — not
mere word mentions (prose, cause names, and probe READS of multiplexing
support are fine anywhere; the multiplexing check is deliberately scoped to
option ASSEMBLY per the spec). A new module that starts assembling keepalive
options, severing on idle timers, or building ControlMaster options fails
this test and forces a deliberate decision: route it through the declared
home, or amend the home set here WITH the engineering-principles row.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"

# Token classes → (pattern, declared homes). Homes are repo-relative under
# src/hpc_agent/, forward slashes.
_LIFECYCLE_TOKEN_HOMES: dict[str, tuple[re.Pattern[str], frozenset[str]]] = {
    # Choosing/setting keepalive values — the death-detector policy G4 moved
    # library-native (asyncssh kwargs in ssh_engine; -o ServerAlive* assembly
    # in ssh_options).
    "keepalive-management": (
        re.compile(
            r"keepalive_interval|keepalive_count_max|ServerAliveInterval|ServerAliveCountMax"
        ),
        frozenset({"infra/ssh_engine.py", "infra/ssh_options.py"}),
    ),
    # Assembling multiplexing options (scoped to ASSEMBLY: probes that READ
    # ControlMaster support, prose, and help text are exempt by construction —
    # the pattern matches option-building forms only).
    "multiplex-assembly": (
        re.compile(
            r"ControlMaster[=\s]*(?:auto|yes|no)|-oControlMaster|ControlPath=|ControlPersist="
        ),
        frozenset({"infra/ssh_options.py", "infra/transport/__init__.py"}),
    ),
    # Idle timers that recycle/sever connections — post-G4 exactly one home:
    # the zero-inflight slot-courtesy recycle in ssh_engine (policy, declared).
    "idle-management": (
        re.compile(r"idle_timeout|IDLE_TIMEOUT|_sweep_idle|idle_sec|IDLE_SEC|max_idle"),
        frozenset({"infra/ssh_engine.py"}),
    ),
}


def _files_matching(pattern: re.Pattern[str]) -> frozenset[str]:
    return frozenset(
        str(p.relative_to(_SRC)).replace("\\", "/")
        for p in _SRC.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
    )


def test_transport_lifecycle_vocabulary_stays_in_declared_homes() -> None:
    problems: list[str] = []
    for name, (pattern, homes) in _LIFECYCLE_TOKEN_HOMES.items():
        actual = _files_matching(pattern)
        escaped = actual - homes
        if escaped:
            problems.append(
                f"{name}: lifecycle vocabulary appeared OUTSIDE its declared homes "
                f"in {sorted(escaped)} — route it through the declared home "
                f"({sorted(homes)}) or amend this pin deliberately (B16)."
            )
        vacated = homes - actual
        if vacated:
            problems.append(
                f"{name}: declared home(s) {sorted(vacated)} no longer carry the "
                "vocabulary — shrink this pin so it cannot rot vacuously."
            )
    assert not problems, "\n".join(problems)


def test_the_pin_is_not_vacuous() -> None:
    # Every token class must match at least one real file — an all-empty scan
    # means the patterns rotted, not that the codebase went lifecycle-free.
    for name, (pattern, _homes) in _LIFECYCLE_TOKEN_HOMES.items():
        assert _files_matching(pattern), f"{name}: pattern matches nothing — the pin has rotted"

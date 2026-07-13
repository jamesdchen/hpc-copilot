"""The conformance report — the verdict line the kit stamps (D-K6).

The report NEVER grades on a curve. A run stamps exactly one of:

* ``conforming: harness contract v1 (kit hpc-agent X.Y.Z)`` — all three
  capabilities' kit modules PASSED (plus, in waves B/C, canonicalization and
  negotiation);
* ``partial: <capability list> (kit hpc-agent X.Y.Z)`` — only the named
  capabilities certified.

Either way, every SKIPPED capability is listed WITH its contract-named degraded
tier verbatim — a skip is honest about what degraded, never rounded up
(boundary-drift flag "skips stay honest").

Stdlib-only and pytest-free so the render is unit-testable directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hpc_agent.conformance.adapter import CAPABILITIES, DEGRADED_TIERS
from hpc_agent.ops.harness_capabilities import HARNESS_CONTRACT_VERSION

__all__ = [
    "CONTRACT_MAJOR",
    "CONTRACT_VERSION",
    "ConformanceReport",
    "kit_version",
    "render_report",
]

# The MAJOR the verdict line names ("harness contract v1"). Within major 1
# changes are additive-only (D-K6 deprecation posture).
CONTRACT_MAJOR = "v1"

# The full SemVer of the contract the kit certifies against. K10 re-pointed this
# at the ONE constant beside the verb (``ops/harness_capabilities.py``) — D-K6:
# the constant's single home is the verb, the doc's version line and this stamp
# are pinned equal to it by ``tests/contracts/test_harness_contract.py``. The
# verdict LINE names only the major (``harness contract v1``); this full version
# is what the kit stamps alongside ``hpc_agent.__version__`` and what the
# three-way agreement pin holds.
CONTRACT_VERSION = HARNESS_CONTRACT_VERSION


def kit_version() -> str:
    """The shipping ``hpc-agent`` version — the kit version by construction.

    D-K1: the kit rides the wheel, so its version is pinned to the package
    version. The report stamps this so a verdict names the exact kit that
    produced it.
    """
    from hpc_agent import __version__

    return __version__


@dataclass
class ConformanceReport:
    """The tallied outcome of one kit run, ready to render.

    * *declared* — capabilities the adapter implements (:func:`declared_capabilities`).
    * *skipped* — capabilities skipped (undeclared → degraded tier).
    * *failed* — declared capabilities whose kit modules FAILED.
    A capability is PASSED iff declared and not failed.
    """

    adapter_name: str
    declared: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    contract_major: str = CONTRACT_MAJOR
    _kit_version: str = field(default="")

    @property
    def passed(self) -> frozenset[str]:
        return frozenset(self.declared - self.failed)

    @property
    def conforming(self) -> bool:
        """All three contract capabilities passed and none failed/skipped."""
        return self.passed == CAPABILITIES and not self.failed and not self.skipped

    def kit_version_str(self) -> str:
        return self._kit_version or kit_version()

    def to_lines(self) -> list[str]:
        return render_report(
            adapter_name=self.adapter_name,
            passed=self.passed,
            skipped=self.skipped,
            failed=self.failed,
            conforming=self.conforming,
            contract_major=self.contract_major,
            kit_version=self.kit_version_str(),
        )


def render_report(
    *,
    adapter_name: str,
    passed: frozenset[str],
    skipped: frozenset[str],
    failed: frozenset[str] = frozenset(),
    conforming: bool,
    contract_major: str = CONTRACT_MAJOR,
    kit_version: str,
) -> list[str]:
    """Render the verdict block: one headline + one line per skipped capability.

    The headline is the ``conforming: ...`` line only when *conforming*;
    otherwise ``partial: <sorted passed caps>``. Each skip line names the
    contract's degraded tier verbatim.
    """
    suffix = f"(kit hpc-agent {kit_version})"
    lines: list[str] = [f"[conformance] harness: {adapter_name}"]
    if conforming:
        lines.append(f"conforming: harness contract {contract_major} {suffix}")
    else:
        caps = ", ".join(sorted(passed)) if passed else "none"
        lines.append(f"partial: {caps} {suffix}")
    for capability in sorted(skipped):
        tier = DEGRADED_TIERS.get(capability, "unnamed degraded tier")
        lines.append(f"  skipped: {capability} — degraded tier: {tier}")
    for capability in sorted(failed):
        lines.append(f"  failed: {capability}")
    return lines

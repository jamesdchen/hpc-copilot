"""Quant pack CHECK script — runs caller-side, emits a pack receipt.

The domain check runs ENTIRELY outside core (domain-packs.md DP2 — core never
imports or executes a pack file), then records its mechanical verdict as a
sha-bound CODE receipt via ``hpc-agent pack-record-receipt``. The pack's own CI
(or the experiment env) runs this; core only ever weighs the resulting receipt,
which reads stale the instant any checked byte drifts.

The check here is STRUCTURAL, not a research judgment: the ACTIVE audit template
must carry every expected ``hpc-audit-section`` slug as an order-preserving
presence. The slug list is the 5-slug domain inventory (data-selection ->
target-construction -> feature-construction -> baseline -> metrics). No research
content is asserted; the verdict is a mechanical boolean the receipt records.

PARAMETERIZED (v0.2.0, the two-layer split): the template being checked is an
INPUT, not hard-wired. The domain check verifies whichever ACTIVE program
template a repo runs; with no ``--template`` it self-checks this domain pack's
own skeleton (a portable default). The receipt is recorded under the QUANT
pack's bind and fills the ``quant-audit`` DOMAIN slot — the slot names the
domain clearance; the concrete program identity rides the checked template's sha
echo on the receipt.

Usage:
    python packs/quant/check/check_quant.py [--experiment-dir DIR] [--template REL]
      --experiment-dir  experiment repo root (default: cwd)
      --template        experiment-relative path to the ACTIVE audit template
                        (default: this pack's own quant_skeleton.py)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_PACK = "quant"
_SLOT = "quant-audit"
# No lab default: the DOMAIN check assumes no program template. With no
# ``--template`` given it self-checks this pack's own skeleton (portable); its
# experiment-relative path is derived mechanically in ``main``.
_DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "quant_skeleton.py"

# The 5-slug domain inventory (order-preserving). Post-signature swap to the
# 12-slug inventory happens together with the seat swap (see README.md).
_EXPECTED_SECTIONS = (
    "data-selection",
    "target-construction",
    "feature-construction",
    "baseline",
    "metrics",
)


def check_sections(template: Path) -> bool:
    """Every expected section slug appears, in order (a structural presence check)."""
    text = template.read_text(encoding="utf-8")
    cursor = 0
    for slug in _EXPECTED_SECTIONS:
        marker = f"hpc-audit-section: {slug}"
        idx = text.find(marker, cursor)
        if idx < 0:
            return False
        cursor = idx + len(marker)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quant domain structural check.")
    parser.add_argument("--experiment-dir", default=".", help="experiment repo root")
    parser.add_argument(
        "--template",
        default=None,
        help="experiment-relative path to the active audit template",
    )
    # Back-compat: accept a bare positional experiment dir (the v0.1.0 call form).
    parser.add_argument("experiment_dir_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    experiment_dir = Path(args.experiment_dir_pos or args.experiment_dir)
    if args.template is not None:
        template_rel = args.template
        template_path = experiment_dir / template_rel
    else:
        template_path = _DEFAULT_TEMPLATE
        template_rel = Path(os.path.relpath(template_path, experiment_dir.resolve())).as_posix()
    passed = check_sections(template_path)
    spec = {
        "pack": _PACK,
        "slot": _SLOT,
        "checked": [template_rel],
        "passed": passed,
        "evidence": {
            "checker": "check_sections",
            "template": template_rel,
            "sections_in_order": passed,
        },
    }
    spec_path = experiment_dir / ".hpc" / "quant_receipt_spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.call(  # noqa: S603 — fixed argv, illustrative caller-side call
        [
            "hpc-agent",
            "pack-record-receipt",
            "--experiment-dir",
            str(experiment_dir),
            "--spec",
            str(spec_path),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

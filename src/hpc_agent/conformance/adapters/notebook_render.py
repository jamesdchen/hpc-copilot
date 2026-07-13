"""The notebook-render reference adapter (K8) — the SECOND, PARTIAL harness.

The jupytext plugin (``examples/plugins/hpc-agent-notebook-render``) as a
conforming harness, certified FIRST among externals (``docs/design/conformance-kit.md``
D-K5). Its conformance surface is capability 1 ONLY, via its ``ingest-signoffs``
path: a human types a sign-off into a rendered ``.ipynb`` sign-off cell — OUT of
band from the LLM, exactly the kit's ``write_utterance`` semantics ("as if a human
typed it") — and the ingest verb lands the text on the utterance log through the
documented write API (:func:`hpc_agent.state.utterances.append_utterance`, filters
included).

* :meth:`NotebookRenderAdapter.write_utterance` materialises a source/template
  ``.py`` pair, renders the audit notebook (``notebook-render``), types *text* into
  the human-required section's sign-off cell, and runs ``notebook-ingest-signoffs``
  with ``write_utterance_log=True`` — the F1 flag is HUMAN-INVOKED-ONLY and the
  adapter models exactly that human invocation (it IS the human-input channel under
  test). Text opening with a harness-injection tag is refused by the ingest's own
  ``is_harness_injected`` filter — one definition, the honest provenance behaviour.
* :meth:`NotebookRenderAdapter.detect_capabilities` detects capability 1 BY
  BEHAVIOR (the non-Claude-Code honest-detection rule, D-K3): the write path proving
  the reader accepts what it wrote. It never claims relay-enforcement or a
  per-harness backgrounding detection.

It declares NOTHING else: relay enforcement and backgrounding are genuinely
absent (the notebook harness has never claimed them), so the kit SKIPS those
modules with the contract-named degraded tier and the report reads
``partial: utterance-log``. Partial is honest, not a failure.

The render stack (``hpc_agent_notebook_render`` + jupytext/nbformat/nbdime) is a
LAZY import — installed by the conformance CI job's notebook leg, never a core
dependency. Loadable via
``--harness-adapter hpc_agent.conformance.adapters.notebook_render:build``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent.conformance.adapter import CAP_UTTERANCE_LOG

__all__ = ["NotebookRenderAdapter", "build"]

# A percent-format source/template pair with one auto-cleared section (``header``,
# byte-shared) and one HUMAN-REQUIRED section (``analysis``, a value change) — so a
# rendered notebook carries exactly one sign-off cell for the adapter to type into.
_TEMPLATE = """# %%
# hpc-audit-section: header
import os

# %%
# hpc-audit-section: analysis
value = 0
"""

_SOURCE = """# %%
# hpc-audit-section: header
import os

# %%
# hpc-audit-section: analysis
value = 42
"""

_AUDIT_ID = "kit-notebook-audit"
_SIGNOFF_SECTION = "analysis"


class NotebookRenderAdapter:
    """The jupytext plugin behind the kit's adapter seam — capability 1 only."""

    name = "notebook-render"

    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* as a human sign-off through the render + ingest path.

        Renders the audit notebook, types *text* below the sign-off sentinel of the
        human-required section, and runs ``notebook-ingest-signoffs`` with
        ``write_utterance_log=True`` (the HUMAN-INVOKED-ONLY flag the adapter models
        — it is the harness's human channel for the test). The text lands on the
        utterance log via the core write API, or is refused by the ingest's
        injection filter — exactly the harness's real behaviour.
        """
        experiment_dir = Path(experiment_dir)
        (
            _annotate,
            notebook_render,
            notebook_ingest_signoffs,
            NotebookRenderSpec,
            NotebookIngestSignoffsSpec,
        ) = _load_plugin()

        (experiment_dir / "source.py").write_text(_SOURCE, encoding="utf-8")
        (experiment_dir / "template.py").write_text(_TEMPLATE, encoding="utf-8")

        render = notebook_render(
            experiment_dir=experiment_dir,
            spec=NotebookRenderSpec(
                audit_id=_AUDIT_ID,
                source="source.py",
                template="template.py",
                output_path="_kit_render.ipynb",
            ),
        )
        nb_path = Path(render.output_path)
        _type_into_signoff(_annotate, nb_path, _SIGNOFF_SECTION, text)

        notebook_ingest_signoffs(
            experiment_dir=experiment_dir,
            spec=NotebookIngestSignoffsSpec(
                audit_id=_AUDIT_ID,
                source="source.py",
                template="template.py",
                notebook_path=str(nb_path.relative_to(experiment_dir)),
                write_utterance_log=True,
            ),
        )

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """Detect capability 1 BY BEHAVIOR — the non-Claude-Code honest rule.

        Writes a probe utterance through this adapter's own channel and confirms the
        canonical reader (:func:`hpc_agent.state.utterances.read_utterances`) accepts
        it; a landed probe detects ``utterance-log`` and nothing else. The harness
        never claims relay-enforcement, and backgrounding detection is a core-side
        constant excluded from the per-harness seam set — so the detected set is
        exactly what this adapter behaves.
        """
        from hpc_agent.state.utterances import read_utterances

        experiment_dir = Path(experiment_dir)
        probe = "notebook-render detect-by-behavior probe: analysis value change to 42"
        self.write_utterance(experiment_dir, probe)
        if any(record["text"] == probe for record in read_utterances(experiment_dir)):
            return frozenset({CAP_UTTERANCE_LOG})
        return frozenset()


def _type_into_signoff(annotate: Any, nb_path: Path, slug: str, text: str) -> None:
    """Type *text* below the sentinel of *slug*'s sign-off cell (as a human would)."""
    import nbformat

    signoff_re = annotate.SIGNOFF_MARKER_RE
    sentinel = annotate.SIGNOFF_SENTINEL
    notebook = nbformat.read(str(nb_path), as_version=4)
    for cell in notebook.cells:
        match = signoff_re.search(cell["source"])
        if match and match.group("slug") == slug:
            head = cell["source"].split(sentinel)[0]
            cell["source"] = head + sentinel + "\n" + text
    nbformat.write(notebook, str(nb_path))


def _load_plugin() -> tuple[Any, Any, Any, Any, Any]:
    """Lazily import the render stack — installed only in the CI notebook leg.

    Returns ``(_annotate module, notebook_render, notebook_ingest_signoffs,
    NotebookRenderSpec, NotebookIngestSignoffsSpec)``. Kept lazy so importing this
    adapter (and the kit) never requires the plugin or jupytext/nbformat/nbdime.
    """
    from hpc_agent_notebook_render import _annotate
    from hpc_agent_notebook_render._models import (
        NotebookIngestSignoffsSpec,
        NotebookRenderSpec,
    )
    from hpc_agent_notebook_render.ingest import notebook_ingest_signoffs
    from hpc_agent_notebook_render.render import notebook_render

    return (
        _annotate,
        notebook_render,
        notebook_ingest_signoffs,
        NotebookRenderSpec,
        NotebookIngestSignoffsSpec,
    )


def build() -> NotebookRenderAdapter:
    """Zero-arg factory for ``--harness-adapter …adapters.notebook_render:build``."""
    return NotebookRenderAdapter()

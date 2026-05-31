"""Behavioral evaluation harness for the hpc-agent surface.

See ``README.md`` in this directory. The package exposes:

* :mod:`tests.eval.recursive_compare` — the structural, float-tolerant grader.
* :mod:`tests.eval.cases` — the declarative eval corpus (NL request → expected spec).
* :mod:`tests.eval.resolve` — the offline deterministic resolver + the LLM driver seam.
"""

from __future__ import annotations

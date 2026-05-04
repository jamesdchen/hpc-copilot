"""Make `tests/` a Python package so test files can do
``from tests.conftest import make_sidecar_json, write_hpc_tasks``.

Without this file, the import works under `python -m pytest` (the cwd
ends up on sys.path so `tests/conftest.py` is locatable) but fails
under the bare `pytest` binary on CI (which uses different sys.path
construction). Adding `__init__.py` makes the import resolve under
both invocations.

The conftest fixtures themselves are still auto-discovered by pytest
in the normal way; this file just exposes the explicit module
import path for the handful of test modules that opted into
sharing helpers via direct import rather than fixtures.
"""

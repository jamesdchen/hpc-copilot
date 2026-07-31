"""Console + dialog hygiene for every PYTHON DESCENDANT of the test suite.

The suite's conftest wraps ``subprocess.Popen`` and sets the process error
mode — but a test-spawned WORKER (a separate interpreter running a generated
``worker.py``) never imports conftest, so its own console-app children each
allocated a fresh visible console and its severed-pipe launches raised modal
hard-error boxes (the 2026-07-30 popup storm's third species: grandchildren).
``sitecustomize`` is auto-imported by every Python interpreter that finds it
on ``sys.path``, so appending this directory to ``PYTHONPATH`` in conftest
covers the whole descendant tree, however deep. POSIX: no-op.

Kept dependency-free and silent: a failure here must never break a worker.
"""

import sys

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    try:
        import ctypes
        import subprocess

        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)

        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
        _ORIG_INIT = subprocess.Popen.__init__

        def _init_no_window(self, *args, **kwargs):  # noqa: ANN001, ANN202
            flags = kwargs.get("creationflags", 0)
            if not flags & _DETACHED:
                kwargs["creationflags"] = flags | _NO_WINDOW
            _ORIG_INIT(self, *args, **kwargs)

        subprocess.Popen.__init__ = _init_no_window  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001 - hygiene must never break a worker
        pass

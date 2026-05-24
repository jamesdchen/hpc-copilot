"""``python -m hpc_agent`` entry point. Delegates to ``cli.dispatch.main``."""

import sys

from hpc_agent.cli.dispatch import main

if __name__ == "__main__":
    sys.exit(main())

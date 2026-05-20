"""``python -m hpc_agent`` entry point. Delegates to ``agent_cli.main``."""

import sys

from hpc_agent.agent_cli import main

if __name__ == "__main__":
    sys.exit(main())

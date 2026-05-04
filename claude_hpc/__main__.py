"""``python -m claude_hpc`` entry point. Delegates to ``agent_cli.main``."""

import sys

from claude_hpc.agent_cli import main

if __name__ == "__main__":
    sys.exit(main())

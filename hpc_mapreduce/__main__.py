"""``python -m hpc_mapreduce`` entry point. Delegates to ``agent_cli.main``."""

import sys

from hpc_mapreduce.agent_cli import main

if __name__ == "__main__":
    sys.exit(main())

"""``python -m hpc_mapreduce`` entry point. Delegates to ``cli.main``."""

import sys

from hpc_mapreduce.cli import main

if __name__ == "__main__":
    sys.exit(main())

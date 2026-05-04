"""Primitive atoms whose decorated entry point is at the primitives layer.

C′-v2 step 5: cmd_* dispatchers in ``agent_cli.py`` that have no inner
Python helper get a real primitive-layer module here. The argparse
adapter in ``agent_cli`` becomes a thin wrapper that calls into this
package.
"""

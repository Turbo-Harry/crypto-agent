"""Compatibility import for Agent contracts now owned by ``interfaces``.

New cross-layer callers must import :mod:`interfaces.agent` directly.  The
decision package keeps this re-export so existing internal callers and stored
replay code remain source-compatible.
"""

from interfaces.agent import *  # noqa: F401,F403

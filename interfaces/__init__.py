"""Stable cross-module contracts.

Only dependency-free types and ``Protocol`` definitions belong here.  Runtime
modules may implement these contracts, while callers depend on the contracts
instead of reaching into another module's private state.
"""

from interfaces.trading import TradingRuntimePort

__all__ = ["TradingRuntimePort"]

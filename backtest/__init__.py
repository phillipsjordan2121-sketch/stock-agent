"""
Phase 0 backtest package for the Stock Agent technical-core strategy.

FULLY ISOLATED from the live daily pipeline:
  - Never imports, reads, or writes portfolio.json / trades.json /
    rejected.json / learning_context.txt.
  - Only reads Polygon market history and writes into backtest/results/.
  - Run on-demand (workflow_dispatch), never on the daily cron.

Nothing here can affect the running paper-trading agent.
"""
__all__ = ["config", "data", "indicators", "engine"]

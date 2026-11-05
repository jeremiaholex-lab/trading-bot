"""
dashboard

Read-only data layer for the trading-bot web dashboard. Importing this
package never places an order; it only reads broker, journal, log and
state-file data.
"""

from .data_fetcher import (
    get_account,
    get_analytics,
    get_attention,
    get_config,
    get_liveness,
    get_logs,
    get_macro_snapshot,
    get_optimizer_state,
    get_positions,
    get_recent_trades,
    get_scan_snapshot,
    get_trade_summary,
    get_workflow,
    fetch_overview,
    orchestrator_status,
    peek_scan,
)

__all__ = [
    "get_account",
    "get_analytics",
    "get_attention",
    "get_config",
    "get_liveness",
    "get_logs",
    "get_macro_snapshot",
    "get_optimizer_state",
    "get_positions",
    "get_recent_trades",
    "get_scan_snapshot",
    "get_trade_summary",
    "get_workflow",
    "fetch_overview",
    "orchestrator_status",
    "peek_scan",
]

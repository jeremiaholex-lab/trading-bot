"""
dashboard/data_fetcher.py

Read-only extraction of live trading-bot state for the web dashboard.

Design rules:
  * Nothing here can place an order, cancel an order, or mutate bot state.
  * Every public function is individually try/except'd by the caller, so one
    failing data source degrades a single panel instead of blanking the page.
  * Values are cached for a short TTL so a fast-polling UI does not hammer
    Alpaca or re-download 9 symbols of bars on every request.

Sources:
  Orchestrator.broker        -> account, positions, market clock
  Orchestrator.risk_mgr      -> start-of-day equity, circuit breaker
  Orchestrator.market_data   -> OHLCV bars
  Orchestrator.self_improver -> tuned params + walk-forward performance
  trades.db                  -> realized PnL, MAE/MFE, holdings
  bot_heartbeat.json         -> liveness, scans, warnings
  logs/bot.log               -> event stream
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from indicators.technicals import TechnicalIndicators
from strategy.adaptive_strategy import AdaptiveStrategy

logger = logging.getLogger("DashboardData")

# --------------------------------------------------------------------------
# Test-artifact detection
# --------------------------------------------------------------------------
# The repository's test-suite records synthetic trades into the same
# trades.db the live bot uses (symbols AAA/BBB/CCC, SYM0..SYM4, A..G).
# Those rows are not real trades. Reporting them as live performance would
# be actively misleading -- 221 fake "open positions" at $150 a share.
_TEST_SYMBOL_RE = re.compile(r"^(?:[A-G]|AAA|BBB|CCC|SYM\d+)$")

TEST_ROW_NOTE = (
    "Rows from the test suite (synthetic symbols such as AAA, SYM0, A-G) are "
    "excluded from every performance figure on this dashboard."
)


def is_test_symbol(symbol: str) -> bool:
    return bool(_TEST_SYMBOL_RE.match((symbol or "").strip().upper()))


# --------------------------------------------------------------------------
# Lazy singletons
# --------------------------------------------------------------------------

_lock = threading.RLock()
_orchestrator = None
_orchestrator_error: Optional[str] = None

_cache: Dict[str, tuple[float, Any]] = {}
_key_locks: Dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _cached(key: str, ttl: float, producer):
    """Returns a cached value when fresher than `ttl`, else recomputes.

    A per-key lock means an expensive producer (the market scan takes
    seconds) only ever runs once even when several request threads ask for
    it at the same moment.
    """
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    with _key_locks_guard:
        lock = _key_locks.setdefault(key, threading.Lock())

    with lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        value = producer()
        _cache[key] = (time.time(), value)
        return value


def _invalidate(key: str) -> None:
    _cache.pop(key, None)


def _detach_bot_log_handler() -> None:
    """Stop this process from writing into the bot's audit log.

    Importing `bot` runs its module-level `logging.basicConfig`, which
    attaches a FileHandler to logs/bot.log. If the importer has not
    configured logging yet (a REPL, a test, a one-off script), every line
    this module emits would land in the bot's own audit trail -- and since
    the dashboard tails that same file, the output can feed back on itself.
    The bot never imports this module, so detaching here is safe.
    """
    target = (Config.LOGS_DIR / "bot.log").resolve()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not isinstance(handler, logging.FileHandler):
            continue
        try:
            same_file = Path(handler.baseFilename).resolve() == target
        except Exception:
            same_file = False
        if same_file:
            root.removeHandler(handler)
            handler.close()


def get_orchestrator():
    """
    A read-only orchestrator instance owned by the dashboard process.

    This is deliberately separate from the process actually trading: the
    running bot keeps its session state in memory, and the dashboard only
    needs the broker/market-data/optimizer surfaces.
    """
    global _orchestrator, _orchestrator_error
    with _lock:
        if _orchestrator is None:
            try:
                from bot import TradingBotOrchestrator
                _detach_bot_log_handler()
                _orchestrator = TradingBotOrchestrator()
                _orchestrator_error = None
                logger.info("Dashboard: read-only orchestrator ready")
            except Exception as e:  # pragma: no cover - depends on broker reachability
                _orchestrator_error = f"{type(e).__name__}: {e}"
                logger.error("Dashboard: orchestrator init failed: %s", _orchestrator_error)
                raise
        return _orchestrator


def orchestrator_status() -> Dict[str, Any]:
    """Non-throwing probe so the UI can show *why* data is missing."""
    try:
        get_orchestrator()
        return {"ready": True, "error": None}
    except Exception as e:
        return {"ready": False, "error": f"{type(e).__name__}: {e}"}


def _jrnl():
    from execution.trade_journal import TradeJournal
    return TradeJournal()


# --------------------------------------------------------------------------
# Account & positions
# --------------------------------------------------------------------------

def get_account() -> Dict[str, Any]:
    """Equity, cash, buying power, daily P&L, drawdown, circuit breaker."""
    return _cached("account", 2.0, _compute_account)


def _compute_account() -> Dict[str, Any]:
    orch = get_orchestrator()
    acc = orch.broker.get_account_summary()

    equity = float(acc.get("equity") or 0.0)
    last_equity = float(acc.get("last_equity") or 0.0) or equity

    daily_pnl = equity - last_equity
    daily_pnl_pct = (daily_pnl / last_equity * 100.0) if last_equity else 0.0

    # Drawdown is measured against the previous close, which is the broker's
    # own definition of a trading day. The dashboard's RiskManager is a fresh
    # instance in this process: it has never seen the trading loop, so
    # `risk_mgr.start_of_day_equity` here would just equal today's equity and
    # report a permanent, meaningless 0.00% drawdown.
    drawdown_pct = max(0.0, -daily_pnl_pct)
    limit_pct = Config.DAILY_DRAWDOWN_LIMIT * 100.0

    return {
        "equity": round(equity, 2),
        "cash": round(float(acc.get("cash") or 0.0), 2),
        "buying_power": round(float(acc.get("buying_power") or 0.0), 2),
        "last_equity": round(last_equity, 2),
        "start_of_day_equity": round(last_equity, 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "daily_drawdown_pct": round(drawdown_pct, 2),
        "drawdown_limit_pct": round(limit_pct, 2),
        # Same rule the risk manager applies: drawdown at or past the limit
        # halts new entries. Derived from live equity so it self-corrects.
        "circuit_breaker_tripped": drawdown_pct >= limit_pct,
        "breaker_basis": "equity vs previous close",
        "mode": acc.get("mode", "UNKNOWN"),
        "currency": acc.get("currency", "USD"),
        "simulated": bool(getattr(orch.broker, "is_simulated", True)),
    }


def get_positions() -> Dict[str, Any]:
    """Open positions with stop-loss, take-profit and unrealized P&L."""
    return _cached("positions", 2.0, _compute_positions)


def _compute_positions() -> Dict[str, Any]:
    raw = get_orchestrator().broker.get_open_positions() or {}
    rows: List[Dict[str, Any]] = []
    total_unrealized = 0.0
    unprotected: List[str] = []

    for sym, p in raw.items():
        qty = int(p.get("qty") or 0)
        entry = float(p.get("entry_price") or 0.0)
        current = float(p.get("current_price") or 0.0)
        stop = float(p.get("stop_loss") or 0.0)
        target = float(p.get("take_profit") or 0.0)
        pl = float(p.get("unrealized_pl") or 0.0)
        pct = float(p.get("unrealized_plpc") or 0.0)

        cost = entry * qty
        exposure_pct = 0.0
        total_unrealized += pl

        if stop <= 0 or target <= 0:
            unprotected.append(sym)

        risk_per_share = (entry - stop) if stop > 0 else 0.0
        rows.append({
            "symbol": sym,
            "qty": qty,
            "entry_price": round(entry, 2),
            "current_price": round(current, 2),
            "stop_loss": round(stop, 2),
            "take_profit": round(target, 2),
            "market_value": round(current * qty, 2),
            "cost_basis": round(cost, 2),
            "unrealized_pl": round(pl, 2),
            "unrealized_pl_pct": round(pct, 2),
            "risk_per_share": round(risk_per_share, 2),
            "risk_amount": round(max(risk_per_share, 0.0) * qty, 2),
            "protected": stop > 0 and target > 0,
            "r_multiple": round((current - entry) / risk_per_share, 2) if risk_per_share > 0 else None,
        })

    rows.sort(key=lambda r: r["unrealized_pl"], reverse=True)
    return {
        "positions": rows,
        "count": len(rows),
        "max_positions": Config.MAX_OPEN_POSITIONS,
        "total_unrealized_pnl": round(total_unrealized, 2),
        "total_market_value": round(sum(r["market_value"] for r in rows), 2),
        "unprotected_symbols": unprotected,
    }


# --------------------------------------------------------------------------
# Trade journal analytics (test rows excluded)
# --------------------------------------------------------------------------

def _closed_and_open_rows() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Returns (closed_rows, open_rows, excluded_test_rows) from trades.db."""
    conn = _jrnl()._get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    closed: List[Dict[str, Any]] = []
    open_rows: List[Dict[str, Any]] = []
    excluded = 0

    for r in rows:
        d = dict(r)
        if is_test_symbol(d.get("symbol", "")):
            excluded += 1
            continue
        if d.get("status") == "CLOSED" or d.get("exit_time"):
            closed.append(d)
        else:
            open_rows.append(d)

    return closed, open_rows, excluded


def get_trade_summary() -> Dict[str, Any]:
    return _cached("trade_summary", 5.0, _compute_trade_summary)


def _compute_trade_summary() -> Dict[str, Any]:
    closed, open_rows, excluded = _closed_and_open_rows()

    pnls = [float(t.get("realized_pnl") or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = len(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    durations = [float(t.get("holding_period_seconds") or 0.0) for t in closed
                 if float(t.get("holding_period_seconds") or 0.0) > 0]

    return {
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / total * 100, 2) if total else 0.0,
        "total_realized_pnl": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "avg_trade_pnl": round(sum(pnls) / total, 2) if total else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "avg_holding_hours": round(sum(durations) / len(durations) / 3600, 2) if durations else 0.0,
        "open_in_journal": len(open_rows),
        "excluded_test_rows": excluded,
        "data_note": TEST_ROW_NOTE if excluded else None,
        "has_real_trades": total > 0,
    }


def get_recent_trades(limit: int = 40) -> List[Dict[str, Any]]:
    closed, open_rows, _ = _closed_and_open_rows()
    rows = sorted(closed + open_rows, key=lambda r: r.get("id", 0), reverse=True)[:limit]
    out = []
    for r in rows:
        risk = None
        try:
            stop = float(r.get("stop_loss") or 0.0)
            entry = float(r.get("entry_price") or 0.0)
            qty = int(r.get("qty") or 0)
            if stop > 0 and entry > 0 and qty > 0:
                risk = (entry - stop) * qty
        except Exception:
            pass
        pnl = float(r.get("realized_pnl") or 0.0)
        out.append({
            "trade_id": r.get("trade_id"),
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "qty": r.get("qty"),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "stop_loss": r.get("stop_loss"),
            "take_profit": r.get("take_profit"),
            "entry_time": r.get("entry_time"),
            "exit_time": r.get("exit_time"),
            "realized_pnl": round(pnl, 2),
            "realized_pnl_pct": round(float(r.get("realized_pnl_pct") or 0.0), 2),
            "holding_hours": round(float(r.get("holding_period_seconds") or 0.0) / 3600, 2),
            "mae": r.get("mae"),
            "mfe": r.get("mfe"),
            "status": r.get("status"),
            "reason": (r.get("reason") or "")[:160],
            "r_multiple": round(pnl / risk, 2) if risk else None,
        })
    return out


def get_portfolio_history(period: str, timeframe: str) -> Dict[str, Any]:
    """Read cached Alpaca portfolio history for the current dashboard profile."""
    validate_portfolio_history_request(period, timeframe)
    cache_key = f"portfolio_history:{period}:{timeframe}"
    return _cached(cache_key, 30.0, lambda: _compute_portfolio_history(period, timeframe))


def validate_portfolio_history_request(period: str, timeframe: str) -> None:
    """Reject unsupported history windows before they reach Alpaca."""
    periods = {"1D", "1W", "1M", "3M", "6M", "1Y", "All"}
    timeframes = {"15Min", "1H", "1D"}
    if period not in periods:
        raise ValueError(f"Unsupported portfolio-history period: {period}")
    if timeframe not in timeframes:
        raise ValueError(f"Unsupported portfolio-history timeframe: {timeframe}")
    if period in {"1M", "3M", "6M", "1Y", "All"} and timeframe != "1D":
        raise ValueError(f"Portfolio-history period {period} requires daily timeframe")
    if period in {"1D", "1W"} and timeframe not in {"15Min", "1H", "1D"}:
        raise ValueError(f"Unsupported timeframe {timeframe} for period {period}")


def _compute_portfolio_history(period: str, timeframe: str) -> Dict[str, Any]:
    """Fetch history through this process's profile-configured broker client."""
    return get_orchestrator().broker.get_portfolio_history(period, timeframe)


def get_analytics() -> Dict[str, Any]:
    """Equity curve, per-symbol attribution, R-multiples, holding-time buckets."""
    return _cached("analytics", 10.0, _compute_analytics)


def _compute_analytics() -> Dict[str, Any]:
    closed, open_rows, excluded = _closed_and_open_rows()

    # --- cumulative realized equity curve -------------------------------
    # No seeded placeholder point: an empty curve is the truthful answer when
    # nothing has closed yet, and the dashboard renders its own empty state.
    curve: List[Dict[str, Any]] = []
    cumulative = 0.0
    for t in closed:
        exit_time = t.get("exit_time")
        if not exit_time:
            continue
        cumulative += float(t.get("realized_pnl") or 0.0)
        curve.append({"t": exit_time, "pnl": round(cumulative, 2)})

    # --- per-symbol attribution ----------------------------------------
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for t in closed:
        sym = t.get("symbol") or "?"
        bucket = by_symbol.setdefault(sym, {"symbol": sym, "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        pnl = float(t.get("realized_pnl") or 0.0)
        bucket["trades"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

    symbol_rows = []
    for b in by_symbol.values():
        b["pnl"] = round(b["pnl"], 2)
        b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
        symbol_rows.append(b)
    symbol_rows.sort(key=lambda r: r["pnl"], reverse=True)

    # --- R-multiple distribution ---------------------------------------
    r_values: List[float] = []
    for t in closed:
        try:
            entry = float(t.get("entry_price") or 0.0)
            stop = float(t.get("stop_loss") or 0.0)
            qty = int(t.get("qty") or 0)
            pnl = float(t.get("realized_pnl") or 0.0)
            risk = (entry - stop) * qty
            if risk > 0:
                r_values.append(round(pnl / risk, 2))
        except Exception:
            continue

    r_buckets = {"<=-2R": 0, "-2R..-1R": 0, "-1R..0": 0, "0..1R": 0, "1R..2R": 0, ">2R": 0}
    for r in r_values:
        if r <= -2:
            r_buckets["<=-2R"] += 1
        elif r <= -1:
            r_buckets["-2R..-1R"] += 1
        elif r < 0:
            r_buckets["-1R..0"] += 1
        elif r < 1:
            r_buckets["0..1R"] += 1
        elif r < 2:
            r_buckets["1R..2R"] += 1
        else:
            r_buckets[">2R"] += 1

    # --- exit-reason attribution ---------------------------------------
    reasons: Dict[str, int] = {}
    for t in closed:
        reason = (t.get("reason") or "unspecified").split("|")[-1].strip()
        key = "Stop loss" if "stop" in reason.lower() else \
              "Take profit" if "take profit" in reason.lower() or "target" in reason.lower() else \
              "Scale-out" if "scale" in reason.lower() else \
              "Signal exit" if "bear" in reason.lower() or "signal" in reason.lower() or "momentum" in reason.lower() else \
              "Other"
        reasons[key] = reasons.get(key, 0) + 1

    return {
        "equity_curve": curve,
        "by_symbol": symbol_rows,
        "r_distribution": [{"bucket": k, "count": v} for k, v in r_buckets.items()],
        "exit_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])],
        "excluded_test_rows": excluded,
        "open_in_journal": len(open_rows),
        "has_real_trades": len(closed) > 0,
    }


# --------------------------------------------------------------------------
# Live scan snapshot
# --------------------------------------------------------------------------

def get_macro_snapshot() -> Dict[str, Any]:
    """Benchmark trend gate, cached apart from the full symbol scan.

    Kept separate so the 2-second overview feed can report a bearish macro
    regime without ever triggering a nine-symbol bar download.
    """
    return _cached("macro", 60.0, _compute_macro)


def _compute_macro() -> Dict[str, Any]:
    if not Config.ENABLE_MACRO_TREND_FILTER:
        return {"bearish": False, "benchmarks": [], "enabled": False}
    try:
        orch = get_orchestrator()
        macro = orch.macro_filter.evaluate_macro_environment(orch.market_data) or {}
    except Exception as e:
        logger.warning("Macro evaluation failed: %s", e)
        return {"bearish": False, "benchmarks": [], "enabled": True, "error": str(e)}
    return {
        "bearish": bool(macro.get("is_bearish")),
        "benchmarks": macro.get("bearish_benchmarks", []),
        "enabled": True,
    }


def peek_scan() -> Optional[Dict[str, Any]]:
    """Last completed scan, or None.

    Deliberately non-blocking: the workflow diagram must render instantly
    on first load rather than waiting on a full universe scan.
    """
    hit = _cache.get("scan")
    return hit[1] if hit else None


def get_scan_snapshot(force: bool = False) -> Dict[str, Any]:
    if force:
        _invalidate("scan")
    return _cached("scan", 20.0, _compute_scan)


def _bars_payload(df, n: int = 60) -> List[Dict[str, Any]]:
    """Flatten the last `n` OHLCV bars into records for the candle chart.

    The scan rows already carry a full DataFrame (the same one the strategy
    analyzed), but only scalar indicators survive to the payload. Exposing the
    raw bars lets the dashboard render a candlestick chart without a second
    data fetch or a new endpoint.
    """
    bars: List[Dict[str, Any]] = []
    for idx, row in df.tail(n).iterrows():
        ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
        bars.append({
            "t": ts,
            "o": float(row["Open"]),
            "h": float(row["High"]),
            "l": float(row["Low"]),
            "c": float(row["Close"]),
            "v": float(row["Volume"]),
        })
    return bars


def _compute_scan() -> Dict[str, Any]:
    orch = get_orchestrator()
    market_data = orch.market_data

    universe: List[str] = list(Config.WATCHLIST)
    if Config.ENABLE_INVERSE_ETFS:
        universe += [s for s in Config.INVERSE_ETFS if s not in universe]

    macro = get_macro_snapshot()

    rows: List[Dict[str, Any]] = []
    for sym in universe:
        try:
            df = market_data.get_historical_bars(sym, timeframe=Config.BAR_TIMEFRAME, days_back=60)
            if df is None or df.empty or len(df) < 50:
                rows.append({"symbol": sym, "error": "insufficient bars", "action": "NO DATA"})
                continue

            params = orch.self_improver.get_symbol_params(sym)
            signal = AdaptiveStrategy(params).generate_signal(sym, df)
            ind = TechnicalIndicators.add_all_indicators(
                df, params.ema_fast, params.ema_slow, params.rsi_period, params.atr_period
            )
            cur = ind.iloc[-1]

            close = float(cur["Close"])
            regime = str(cur.get("Regime", "UNKNOWN"))
            rsi = float(cur.get("RSI") or 0.0)
            atr = float(cur.get("ATR") or 0.0)
            volume_ratio = float(cur.get("Volume_Ratio") or 1.0)
            vwap = float(cur.get("VWAP") or close)
            ema_fast = float(cur.get(f"EMA_{params.ema_fast}") or 0.0)
            ema_slow = float(cur.get(f"EMA_{params.ema_slow}") or 0.0)
            macd = float(cur.get("MACD") or 0.0)

            # Conviction score, mirroring strategy/scanner.py exactly
            score = 0.0
            reasons: List[str] = []
            if regime == "TRENDING_BULL":
                score += 50.0
                reasons.append("trending bull")
                if close > vwap:
                    score += 15.0
                    reasons.append("above VWAP")
            elif regime == "RANGING":
                score += 25.0
                reasons.append("ranging")
            elif regime == "HIGH_VOLATILITY":
                score -= 30.0
                reasons.append("high volatility")
            elif regime == "TRENDING_BEAR":
                score -= 20.0
                reasons.append("trending bear")
            if volume_ratio >= 1.2:
                score += 15.0
                reasons.append(f"volume {volume_ratio:.1f}x")
            if regime == "TRENDING_BULL" and 35 <= rsi <= 50:
                score += 20.0
                reasons.append(f"bullish pullback RSI {rsi:.0f}")

            rows.append({
                "symbol": sym,
                "price": round(close, 2),
                "regime": regime,
                "rsi": round(rsi, 1),
                "atr": round(atr, 3),
                "atr_pct": round(atr / close * 100, 2) if close else 0.0,
                "volume_ratio": round(volume_ratio, 2),
                "vwap": round(vwap, 2),
                "ema_fast": round(ema_fast, 2),
                "ema_slow": round(ema_slow, 2),
                "macd": round(macd, 3),
                "action": str(signal.action),
                "confidence": round(float(signal.confidence), 3),
                "entry_price": round(float(signal.entry_price), 2),
                "stop_loss": round(float(signal.stop_loss_price), 2),
                "take_profit": round(float(signal.take_profit_price), 2),
                "reason": signal.reason,
                "score": round(score, 1),
                "conviction": " | ".join(reasons) or "no edge",
                "bars": _bars_payload(df),
            })
        except Exception as e:
            logger.warning("Scan failed for %s: %s", sym, e)
            rows.append({"symbol": sym, "error": str(e), "action": "ERROR"})

    ranked = sorted(rows, key=lambda r: r.get("score", -999), reverse=True)
    counts: Dict[str, int] = {}
    for r in ranked:
        a = r.get("action", "NO DATA")
        counts[a] = counts.get(a, 0) + 1

    return {
        "symbols": ranked,
        "signal_counts": counts,
        "macro": macro,
        "timeframe": Config.BAR_TIMEFRAME,
        "watchlist": Config.WATCHLIST,
        "inverse_etfs": Config.INVERSE_ETFS if Config.ENABLE_INVERSE_ETFS else [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Optimizer / tuned parameters
# --------------------------------------------------------------------------

def get_optimizer_state() -> Dict[str, Any]:
    return _cached("optimizer", 30.0, _compute_optimizer)


def _compute_optimizer() -> Dict[str, Any]:
    state = get_orchestrator().self_improver.state or {}
    symbols = {}
    for sym, info in (state.get("symbols") or {}).items():
        symbols[sym] = {
            "params": info.get("params", {}),
            "performance": info.get("performance", {}),
            "improved": bool(info.get("improved", False)),
            "optimized_at": info.get("optimized_at", ""),
        }

    adopted = [s for s, v in symbols.items() if v["improved"]]
    return {
        "last_updated": state.get("last_updated"),
        "symbols": symbols,
        "adopted_count": len(adopted),
        "validated_count": len(symbols) - len(adopted),
        "interval_hours": Config.OPTIMIZATION_INTERVAL_HOURS,
        "gates": {
            "min_fitness": Config.MIN_FITNESS_TO_ADOPT,
            "min_improvement": Config.MIN_FITNESS_IMPROVEMENT,
            "min_trades": Config.MIN_TRADES_TO_ADOPT,
            "timeframe": Config.OPTIMIZATION_TIMEFRAME,
            "lookback_days": Config.OPTIMIZATION_LOOKBACK_DAYS,
        },
    }


# --------------------------------------------------------------------------
# Liveness & process health
# --------------------------------------------------------------------------

HEARTBEAT_PATH = Config.HEARTBEAT_PATH


def _read_heartbeat() -> Dict[str, Any]:
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _find_bot_processes() -> List[Dict[str, Any]]:
    """Cached wrapper: enumeration shells out to PowerShell."""
    return _cached("bot_processes", 20.0, _scan_bot_processes)


def _profile_from_command(command: str) -> Optional[str]:
    """Identify a bot worker command line without counting Windows venv shims.

    Windows virtualenvs use a launcher stub (`venv\\Scripts\\python.exe`) that
    spawns the base interpreter (`C:\\Python...\\python.exe`) with the same
    arguments and waits on it. `_scan_bot_processes` collapses that parent/child
    wrapper pair so a single worker is not reported as a duplicate.
    """
    command = command.lower()
    if "pytest" in command or "dashboard_server" in command:
        return None
    worker = re.search(r"--worker-profile(?:=|\s+)(primary|secondary)\b", command)
    if worker:
        return worker.group(1).lower()
    if "run_accounts.py" in command:
        return None
    return "primary" if re.search(r"(?:^|[\\/\s\"])(?:bot\.py)(?:[\s\"]|$)", command) else None


def _collapse_windows_venv_wrappers(processes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse a Windows venv shim parent when its child runs the same profile."""
    by_pid = {item["pid"]: item for item in processes if item.get("pid")}
    wrapper_pids = {
        item.get("ppid")
        for item in processes
        if item.get("ppid") in by_pid
        and by_pid[item["ppid"]].get("profile") == item.get("profile")
    }
    return [
        {"pid": item["pid"], "profile": item["profile"]}
        for item in processes
        if item.get("pid") not in wrapper_pids
    ]


def _scan_bot_processes() -> List[Dict[str, Any]]:
    """Find bot workers and their isolated account profiles."""
    found: List[Dict[str, Any]] = []
    try:
        if os.name == "nt":
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
                "| Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=12,
            )
            if not res.stdout.strip():
                return found
            data = json.loads(res.stdout)
            if isinstance(data, dict):
                data = [data]
            raw: List[Dict[str, Any]] = []
            for item in data:
                cmd = item.get("CommandLine") or ""
                pid = item.get("ProcessId")
                ppid = item.get("ParentProcessId")
                profile = _profile_from_command(cmd)
                if pid and pid != os.getpid() and profile:
                    raw.append({"pid": pid, "ppid": ppid, "profile": profile})
            found.extend(_collapse_windows_venv_wrappers(raw))
        else:
            proc_root = Path("/proc")
            for entry in proc_root.iterdir():
                if not entry.name.isdigit() or int(entry.name) == os.getpid():
                    continue
                try:
                    cmd = (entry / "cmdline").read_bytes().decode("utf-8", "ignore").replace("\x00", " ")
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                profile = _profile_from_command(cmd)
                if profile:
                    found.append({"pid": int(entry.name), "profile": profile})
    except Exception as e:
        logger.warning("Process enumeration failed: %s", e)
    return found


def get_liveness() -> Dict[str, Any]:
    # The overview feed asks for this every 2 seconds. Caching it keeps the
    # header responsive without spawning a PowerShell subprocess per poll.
    return _cached("liveness", 3.0, _compute_liveness)


def _compute_liveness() -> Dict[str, Any]:
    hb = _read_heartbeat()

    age: Optional[float] = None
    updated = hb.get("updated_at")
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            age = None

    # A heartbeat older than 3 minutes while the market is open means the
    # loop is stuck, not merely idle: it scans every 60 seconds.
    market_state = str(hb.get("market_state", "unknown")).upper()
    if age is None:
        verdict = "NO HEARTBEAT"
    elif age < 120:
        verdict = "RUNNING"
    elif market_state == "OPEN":
        verdict = "STALLED"
    else:
        verdict = "IDLE (market closed)"

    processes = [
        process for process in _find_bot_processes()
        if process.get("profile", "primary") == Config.BOT_PROFILE
    ]

    return {
        "profile": Config.BOT_PROFILE,
        "verdict": verdict,
        "heartbeat_age_sec": round(age, 1) if age is not None else None,
        "market_state": market_state,
        "scans": hb.get("scans", 0),
        "cycles": hb.get("cycles", 0),
        "session_trades": hb.get("session_trades", 0),
        "warnings": list(hb.get("warnings") or []),
        "started_at": hb.get("started_at"),
        "last_scan_at": hb.get("last_scan_at"),
        "pid": hb.get("pid"),
        "bot_processes": processes,
        "process_count": len(processes),
        "duplicate_warning": (
            f"{len(processes)} processes are running the {Config.BOT_PROFILE} bot profile at once (PIDs "
            f"{', '.join(str(p['pid']) for p in processes)}). They share one broker account, "
            f"so they can duplicate orders and clash with the same profile state files."
            if len(processes) > 1 else None
        ),
    }


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

LOG_PATH = Config.LOGS_DIR / "bot.log"

_LEVEL_RE = re.compile(r"\[\s*(CRITICAL|ERROR|WARNING|INFO|DEBUG)\s*\]")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s*")
# Dotted module paths are logger names; a bare "SGOV:" is part of the message.
_LOGGER_RE = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+):\s+")


def get_logs(after: int = 0, max_bytes: int = 120_000) -> Dict[str, Any]:
    """
    Incremental log tail.

    `after` is a byte offset. The client stores the returned cursor and
    passes it back, so only new bytes are transferred.
    """
    try:
        size = LOG_PATH.stat().st_size
    except FileNotFoundError:
        return {"lines": [], "cursor": 0, "size": 0, "missing": True}

    start = after if 0 <= after <= size else max(0, size - max_bytes)
    if size - start > max_bytes and after == 0:
        start = size - max_bytes

    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(start)
            chunk = f.read(max_bytes)
    except Exception as e:
        return {"lines": [], "cursor": after, "size": size, "error": str(e)}

    text = chunk.decode("utf-8", "replace")
    # Drop a trailing partial line so it is not shown twice on the next poll
    cursor = start + len(chunk)
    if not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut != -1:
            text = text[:cut + 1]
            cursor = start + len(text.encode("utf-8", "replace"))

    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _LEVEL_RE.search(raw)
        level = m.group(1) if m else "INFO"
        msg = _LEVEL_RE.sub("", raw, count=1)

        # Keep the timestamp. The log file spans days, so a stripped
        # timestamp turns a week-old circuit breaker into what looks like a
        # live emergency.
        ts = ""
        tm = _TS_RE.match(msg)
        if tm:
            ts = tm.group(1)
            msg = msg[tm.end():]

        msg = _LOGGER_RE.sub("", msg, count=1).strip()
        lines.append({"level": level, "ts": ts, "message": msg})

    return {"lines": lines[-400:], "cursor": cursor, "size": size}


# --------------------------------------------------------------------------
# Config (no secrets)
# --------------------------------------------------------------------------

def get_config() -> Dict[str, Any]:
    return {
        "watchlist": Config.WATCHLIST,
        "timeframe": Config.BAR_TIMEFRAME,
        "max_open_positions": Config.MAX_OPEN_POSITIONS,
        "max_risk_per_trade_pct": round(Config.MAX_PORTFOLIO_RISK_PER_TRADE * 100, 2),
        "daily_drawdown_limit_pct": round(Config.DAILY_DRAWDOWN_LIMIT * 100, 2),
        "profit_target_rr": Config.PROFIT_TARGET_RR,
        "min_cash_buffer_pct": round(Config.MIN_CASH_BUFFER_PCT * 100, 2),
        "max_positions_per_sector": Config.MAX_POSITIONS_PER_SECTOR,
        "sector_map": Config.SECTOR_MAP,
        "inverse_etfs_enabled": Config.ENABLE_INVERSE_ETFS,
        "inverse_etfs": Config.INVERSE_ETFS,
        "macro_filter_enabled": Config.ENABLE_MACRO_TREND_FILTER,
        "macro_benchmarks": Config.MACRO_BENCHMARK_SYMBOLS,
        "earnings_filter_enabled": Config.ENABLE_EARNINGS_FILTER,
        "earnings_blackout_days": Config.EARNINGS_BLACKOUT_DAYS,
        "partial_profit_enabled": Config.ENABLE_PARTIAL_PROFIT_TAKING,
        "scale_out_rr": Config.SCALE_OUT_RR,
        "scale_out_pct": round(Config.SCALE_OUT_PERCENT * 100, 1),
        "self_improvement_enabled": Config.ENABLE_SELF_IMPROVEMENT,
        "optimization_interval_hours": Config.OPTIMIZATION_INTERVAL_HOURS,
        "session_report_enabled": Config.ENABLE_SESSION_REPORT,
        "benchmark_symbol": Config.BENCHMARK_SYMBOL,
        "alpaca_live": Config.ALPACA_LIVE,
        "has_alpaca_keys": Config.has_alpaca_credentials(),
        "allow_simulator_fallback": Config.ALLOW_SIMULATOR_FALLBACK,
    }


# --------------------------------------------------------------------------
# Workflow graph (static topology + live stage status)
# --------------------------------------------------------------------------

def get_workflow() -> Dict[str, Any]:
    """The bot's real decision pipeline, with each stage's current status.

    Uses the last completed scan rather than triggering one, so the
    diagram renders immediately on first load.
    """
    scan = peek_scan() or {}
    positions = get_positions()
    optimizer = get_optimizer_state()
    liveness = get_liveness()
    signals = scan.get("signal_counts", {}) or {}
    macro = get_macro_snapshot()

    buy = signals.get("BUY", 0)
    sell = signals.get("SELL", 0)
    hold = signals.get("HOLD", 0)

    stages = [
        {
            "id": "data", "label": "Market Data", "sub": f"{Config.BAR_TIMEFRAME} bars",
            "status": "active" if scan.get("symbols") else "idle",
            "detail": (f"Alpaca / yfinance feeding {len(scan.get('symbols', []))} symbols"
                       if scan.get("symbols") else "Waiting for the first market-data fetch"),
        },
        {
            "id": "macro", "label": "Macro Gate", "sub": "SPY / QQQ regime",
            "status": "blocked" if macro.get("bearish") else ("active" if macro.get("enabled") else "off"),
            "detail": ("Bearish benchmarks: " + ", ".join(macro.get("benchmarks", [])))
                      if macro.get("bearish") else
                      ("Longs permitted — benchmarks not in downtrend" if macro.get("enabled") else "Disabled"),
        },
        {
            "id": "scanner", "label": "Conviction Scanner", "sub": "rank the universe",
            "status": "active" if scan.get("symbols") else "idle",
            "detail": "Highest score is considered first for the remaining position slots",
        },
        {
            "id": "strategy", "label": "Strategy Engine", "sub": "regime → signal",
            "status": "active" if scan.get("symbols") else "idle",
            "detail": (f"{buy} BUY · {sell} SELL · {hold} HOLD across {len(scan.get('symbols', []))} symbols"
                       if scan.get("symbols") else "Waiting for the first scan to complete"),
        },
        {
            "id": "risk", "label": "Risk Manager", "sub": "size & gates",
            "status": "tripped" if get_account().get("circuit_breaker_tripped") else "active",
            "detail": (f"Max {Config.MAX_PORTFOLIO_RISK_PER_TRADE * 100:.1f}% equity risk/trade · "
                       f"{positions['count']}/{Config.MAX_OPEN_POSITIONS} slots used"),
        },
        {
            "id": "execution", "label": "Execution", "sub": "Alpaca bracket",
            "status": "active" if not get_account().get("simulated") else "simulated",
            "detail": "Market entry + server-side OCO stop-loss / take-profit",
        },
        {
            "id": "protection", "label": "Protection Audit", "sub": "OCO & trailing",
            "status": "warning" if positions.get("unprotected_symbols") else "active",
            "detail": (f"Unprotected: {', '.join(positions['unprotected_symbols'])}"
                       if positions.get("unprotected_symbols")
                       else "Every open position carries a stop and a target"),
        },
        {
            "id": "journal", "label": "Trade Journal", "sub": "SQLite ledger",
            "status": "active",
            "detail": f"{positions['count']} open · checking MAE/MFE excursions each cycle",
        },
        {
            "id": "optimizer", "label": "Self-Improver", "sub": "walk-forward",
            "status": "active" if Config.ENABLE_SELF_IMPROVEMENT else "off",
            "detail": (f"{optimizer['adopted_count']} adopted · {optimizer['validated_count']} validated · "
                       f"every {Config.OPTIMIZATION_INTERVAL_HOURS}h"),
        },
        {
            "id": "notify", "label": "Notifications", "sub": "Telegram / Discord",
            "status": "off" if not Config.ENABLE_SESSION_REPORT else "active",
            "detail": "Order fills, circuit-breaker alerts, end-of-session report",
        },
    ]

    edges = [
        ("data", "macro"), ("data", "scanner"),
        ("macro", "strategy"), ("scanner", "strategy"),
        ("strategy", "risk"), ("risk", "execution"),
        ("execution", "protection"), ("protection", "journal"),
        ("journal", "optimizer"), ("optimizer", "strategy"),
        ("execution", "notify"),
    ]

    return {
        "stages": stages,
        "edges": [{"from": a, "to": b} for a, b in edges],
        "liveness": liveness,
        "scans": liveness.get("scans", 0),
    }


# --------------------------------------------------------------------------
# Combined snapshot for the WebSocket feed
# --------------------------------------------------------------------------

def fetch_overview() -> Dict[str, Any]:
    """Fast snapshot: safe to push every couple of seconds."""
    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "orchestrator": orchestrator_status(),
    }
    for key, fn in (("account", get_account), ("positions", get_positions),
                    ("attention", get_attention), ("liveness", get_liveness)):
        try:
            payload[key] = fn()
        except Exception as e:
            payload[key] = {"error": str(e)}
    return payload


def get_attention() -> Dict[str, Any]:
    """Everything worth an operator's eyes right now, ranked by severity."""
    items: List[Dict[str, Any]] = []

    try:
        live = get_liveness()
        if live["verdict"] in ("STALLED", "NO HEARTBEAT"):
            items.append({
                "severity": "critical",
                "title": f"Trading loop {live['verdict'].lower()}",
                "detail": (f"Last heartbeat {live['heartbeat_age_sec']}s ago while the market is "
                           f"{live['market_state']}. It normally scans every 60s."),
            })
        if live.get("duplicate_warning"):
            items.append({
                "severity": "critical",
                "title": "Duplicate bot processes",
                "detail": live["duplicate_warning"],
            })
        for warning in live.get("warnings") or []:
            items.append({
                "severity": "info",
                "title": "Bot report",
                "detail": str(warning),
            })
    except Exception as e:
        items.append({"severity": "warning", "title": "Liveness check failed", "detail": str(e)})

    try:
        pos = get_positions()
        for sym in pos.get("unprotected_symbols", []):
            items.append({
                "severity": "critical",
                "title": f"{sym} has no protective orders",
                "detail": "The position can lose without limit and nothing else in the bot reports it.",
            })
    except Exception:
        pass

    try:
        acc = get_account()
        if acc.get("circuit_breaker_tripped"):
            items.append({
                "severity": "critical",
                "title": "Daily circuit breaker tripped",
                "detail": f"Drawdown {acc['daily_drawdown_pct']}% reached the "
                          f"{acc['drawdown_limit_pct']}% limit. New entries are halted.",
            })
        elif acc.get("daily_drawdown_pct", 0) >= acc.get("drawdown_limit_pct", 100) * 0.66:
            items.append({
                "severity": "warning",
                "title": "Drawdown approaching the circuit breaker",
                "detail": f"{acc['daily_drawdown_pct']}% of the {acc['drawdown_limit_pct']}% limit used.",
            })
    except Exception:
        pass

    try:
        trade = get_trade_summary()
        if trade.get("excluded_test_rows"):
            items.append({
                "severity": "info",
                "title": f"{trade['excluded_test_rows']} test rows hidden",
                "detail": TEST_ROW_NOTE,
            })
        if not trade.get("has_real_trades"):
            items.append({
                "severity": "info",
                "title": "No completed trades in the journal yet",
                "detail": "Performance metrics stay blank until the first position closes.",
            })
    except Exception:
        pass

    try:
        macro = get_macro_snapshot()
        if macro.get("bearish"):
            items.append({
                "severity": "warning",
                "title": "Macro gate bearish",
                "detail": "Benchmarks are in a daily downtrend. Longs need relative strength; "
                          "inverse ETFs become eligible.",
            })
    except Exception:
        pass

    order = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda i: order.get(i["severity"], 3))
    return {
        "items": items,
        "critical": sum(1 for i in items if i["severity"] == "critical"),
        "warning": sum(1 for i in items if i["severity"] == "warning"),
        "info": sum(1 for i in items if i["severity"] == "info"),
    }

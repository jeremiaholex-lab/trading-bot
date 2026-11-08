"""
Mandate Gate - Structural Pre-Trade Compliance

Implements Vibe-Trading's mandate gate pattern: a fast, local, fail-closed check
that answers "is this order ALLOWED?" before any broker call.

The mandate gate enforces structural compliance:
- Symbol universe (allowed/denied lists)
- Notional size caps (per-order, per-symbol, daily)
- Leverage limits
- Daily order/trade caps
- Kill switch integration (halt all new entries)
- Position concentration limits

This runs BEFORE any advisory layer. Advisory answers "is this wise?".
Mandate answers "is this allowed?". They are separate layers.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Set, Any
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)


class MandateViolationType(Enum):
    """Types of mandate violations that can block an order."""
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    SYMBOL_DENIED = "symbol_denied"
    NOTIONAL_EXCEEDS_PER_ORDER_CAP = "notional_exceeds_per_order_cap"
    NOTIONAL_EXCEEDS_PER_SYMBOL_CAP = "notional_exceeds_per_symbol_cap"
    NOTIONAL_EXCEEDS_DAILY_CAP = "notional_exceeds_daily_cap"
    LEVERAGE_EXCEEDS_LIMIT = "leverage_exceeds_limit"
    DAILY_ORDER_CAP_EXCEEDED = "daily_order_cap_exceeded"
    DAILY_TRADE_CAP_EXCEEDED = "daily_trade_cap_exceeded"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    POSITION_CONCENTRATION_EXCEEDED = "position_concentration_exceeded"
    SECTOR_CONCENTRATION_EXCEEDED = "sector_concentration_exceeded"
    ACCOUNT_MODE_MISMATCH = "account_mode_mismatch"
    INSUFFICIENT_EQUITY = "insufficient_equity"


@dataclass(frozen=True)
class MandateViolation:
    """Represents a single mandate violation that blocks an order."""
    violation_type: MandateViolationType
    message: str
    details: Dict[str, any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class MandateContext:
    """
    Context passed to the mandate gate for evaluation.

    Contains all account and position state needed to make
    structural compliance decisions.
    """
    # Order intent
    symbol: str
    side: str  # "buy" or "sell"
    notional_usd: float
    qty: int

    # Account state
    account_equity: float
    account_cash: float
    account_buying_power: float
    account_mode: str  # "paper" or "live"

    # Position state
    open_positions: Dict[str, Dict]  # symbol -> position dict
    open_position_count: int

    # Daily counters (reset at session start)
    daily_notional_usd: float = 0.0
    daily_order_count: int = 0
    daily_trade_count: int = 0

    # Kill switch state
    kill_switch_active: bool = False

    # Configuration (from Config)
    max_notional_per_order: float = 50000.0
    max_notional_per_symbol: float = 100000.0
    max_daily_notional_usd: float = 500000.0
    max_leverage: float = 2.0
    max_daily_orders: int = 100
    max_daily_trades: int = 50
    max_position_concentration: float = 0.25  # 25% of equity per position
    max_sector_concentration: float = 0.40  # 40% of equity per sector
    allowed_symbols: Set[str] = field(default_factory=set)
    denied_symbols: Set[str] = field(default_factory=set)


class MandateGate:
    """
    Structural pre-trade compliance gate.

    Evaluates whether an order is ALLOWED based on hard limits.
    This is the FIRST gate - it runs before any advisory layer.

    Fail-closed: Any error or missing data results in REJECTION.
    Fast: Pure local computation, no network calls.
    Auditable: Every decision is logged with full context.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        kill_switch: Optional['KillSwitch'] = None,
        audit_ledger: Optional['AuditLedger'] = None,
    ):
        self.config = config or Config
        self.kill_switch = kill_switch
        self.audit_ledger = audit_ledger

        # Load symbol universe from config
        self._allowed_symbols: Set[str] = set()
        self._denied_symbols: Set[str] = set()
        self._load_symbol_universe()

        # Daily counters (persisted across restarts via audit ledger)
        self._daily_notional_usd: float = 0.0
        self._daily_order_count: int = 0
        self._daily_trade_count: int = 0
        self._daily_symbol_notional: Dict[str, float] = {}
        self._last_reset_date: Optional[str] = None
        self._load_daily_counters()

        logger.info(
            f"MandateGate initialized: "
            f"allowed_symbols={len(self._allowed_symbols)}, "
            f"denied_symbols={len(self._denied_symbols)}"
        )

    def _get_config(self, key: str, default: Any) -> Any:
        """Get config value with fallback, supporting both class attributes and dict-like access."""
        if hasattr(self.config, key):
            return getattr(self.config, key, default)
        if hasattr(self.config, 'get'):
            return self.config.get(key, default)
        return default

    def _load_symbol_universe(self):
        """Load allowed/denied symbol lists from configuration."""
        # Default to watchlist as allowed universe
        if hasattr(self.config, 'WATCHLIST'):
            self._allowed_symbols = {s.upper() for s in self.config.WATCHLIST}

        # Add inverse ETFs if enabled
        if getattr(self.config, 'ENABLE_INVERSE_ETFS', False):
            if hasattr(self.config, 'INVERSE_ETFS'):
                self._allowed_symbols.update({s.upper() for s in self.config.INVERSE_ETFS})

        # Allow explicit overrides via env
        extra_allowed = getattr(self.config, 'MANDATE_EXTRA_ALLOWED_SYMBOLS', '')
        if extra_allowed:
            self._allowed_symbols.update({s.strip().upper() for s in extra_allowed.split(',') if s.strip()})

        extra_denied = getattr(self.config, 'MANDATE_DENIED_SYMBOLS', '')
        if extra_denied:
            self._denied_symbols.update({s.strip().upper() for s in extra_denied.split(',') if s.strip()})

    def _load_daily_counters(self):
        """Load daily counters from audit ledger if available."""
        if self.audit_ledger:
            try:
                session_date = datetime.now(ZoneInfo("America/New_York")).date()
                session_start = datetime.combine(
                    session_date, datetime.min.time(), tzinfo=ZoneInfo("America/New_York")
                ).astimezone(timezone.utc)
                session_end = session_start + timedelta(days=1)
                utc_dates = {
                    session_start.date().isoformat(),
                    (session_start.date() + timedelta(days=1)).isoformat(),
                }
                entries = [
                    entry
                    for day in sorted(utc_dates)
                    for entry in self.audit_ledger.get_entries_for_date(day)
                ]
                submitted = [
                    e for e in entries
                    if getattr(e.entry_type, "value", e.entry_type) == "order_submitted"
                    and str(e.side or "").lower() == "buy"
                    and session_start <= datetime.fromisoformat(e.timestamp) < session_end
                ]
                self._daily_notional_usd = sum(float(e.notional_usd or 0.0) for e in submitted)
                self._daily_order_count = len(submitted)
                # Count submitted entry orders conservatively as trades so the
                # cap is effective even when fills arrive asynchronously.
                self._daily_trade_count = len(submitted)
                self._daily_symbol_notional = {}
                for entry in submitted:
                    if entry.symbol:
                        symbol = entry.symbol.upper()
                        self._daily_symbol_notional[symbol] = (
                            self._daily_symbol_notional.get(symbol, 0.0)
                            + float(entry.notional_usd or 0.0)
                        )
                self._last_reset_date = session_date.isoformat()
                logger.info(f"Loaded daily counters from audit ledger: "
                           f"notional=${self._daily_notional_usd:,.2f}, "
                           f"orders={self._daily_order_count}, "
                           f"trades={self._daily_trade_count}")
            except Exception as e:
                logger.warning(f"Could not load daily counters from audit ledger: {e}")

    def _reset_daily_counters_if_needed(self):
        """Reset daily counters at the start of a new trading day."""
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if self._last_reset_date != today:
            self._daily_notional_usd = 0.0
            self._daily_order_count = 0
            self._daily_trade_count = 0
            self._daily_symbol_notional = {}
            self._last_reset_date = today
            logger.info(f"Daily mandate counters reset for {today}")

    def check(self, context: MandateContext) -> List[MandateViolation]:
        """
        Evaluate an order against all mandate rules.

        Returns empty list if allowed, list of violations if blocked.
        Fail-closed: any exception results in a violation.
        """
        self._reset_daily_counters_if_needed()

        violations = []
        symbol = context.symbol.upper()
        side = context.side.lower()

        try:
            # 1. Kill Switch - absolute override
            if self._is_kill_switch_active(context):
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.KILL_SWITCH_ACTIVE,
                    message="Kill switch is ACTIVE - all new entries blocked",
                    details={"kill_switch_active": True}
                ))
                return violations  # Fail fast on kill switch

            # 2. Symbol Universe - allowed list
            if self._allowed_symbols and symbol not in self._allowed_symbols:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.SYMBOL_NOT_ALLOWED,
                    message=f"Symbol {symbol} not in allowed universe",
                    details={"symbol": symbol, "allowed_count": len(self._allowed_symbols)}
                ))

            # 3. Symbol Universe - denied list (explicit block)
            if symbol in self._denied_symbols:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.SYMBOL_DENIED,
                    message=f"Symbol {symbol} is explicitly denied",
                    details={"symbol": symbol}
                ))

            # 4. Per-order notional cap
            if context.notional_usd > self._get_config('MANDATE_MAX_NOTIONAL_PER_ORDER', 50000.0):
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.NOTIONAL_EXCEEDS_PER_ORDER_CAP,
                    message=f"Order notional ${context.notional_usd:,.2f} exceeds per-order cap",
                    details={
                        "notional_usd": context.notional_usd,
                        "cap": self._get_config('MANDATE_MAX_NOTIONAL_PER_ORDER', 50000.0)
                    }
                ))

            # 5. Per-symbol notional cap (aggregate across orders)
            symbol_daily_notional = self._get_symbol_daily_notional(symbol)
            max_per_symbol = self._get_config('MANDATE_MAX_NOTIONAL_PER_SYMBOL', 100000.0)
            if symbol_daily_notional + context.notional_usd > max_per_symbol:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.NOTIONAL_EXCEEDS_PER_SYMBOL_CAP,
                    message=f"Symbol {symbol} daily notional would exceed cap",
                    details={
                        "symbol": symbol,
                        "current_daily": symbol_daily_notional,
                        "this_order": context.notional_usd,
                        "cap": max_per_symbol
                    }
                ))

            # 6. Daily notional cap
            max_daily = self._get_config('MANDATE_MAX_DAILY_NOTIONAL_USD', 500000.0)
            if self._daily_notional_usd + context.notional_usd > max_daily:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.NOTIONAL_EXCEEDS_DAILY_CAP,
                    message=f"Daily notional cap would be exceeded",
                    details={
                        "current_daily": self._daily_notional_usd,
                        "this_order": context.notional_usd,
                        "cap": max_daily
                    }
                ))

            # 7. Leverage check
            if context.account_equity > 0:
                current_leverage = (context.account_equity + context.notional_usd) / context.account_equity
                max_leverage = self._get_config('MANDATE_MAX_LEVERAGE', 2.0)
                if current_leverage > max_leverage:
                    violations.append(MandateViolation(
                        violation_type=MandateViolationType.LEVERAGE_EXCEEDS_LIMIT,
                        message=f"Order would push leverage to {current_leverage:.2f}x (max {max_leverage:.2f}x)",
                        details={
                            "projected_leverage": current_leverage,
                            "max_leverage": max_leverage,
                            "account_equity": context.account_equity,
                            "order_notional": context.notional_usd
                        }
                    ))

            # 8. Daily order count cap
            max_daily_orders = self._get_config('MANDATE_MAX_DAILY_ORDERS', 100)
            if self._daily_order_count >= max_daily_orders:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.DAILY_ORDER_CAP_EXCEEDED,
                    message=f"Daily order cap ({max_daily_orders}) reached",
                    details={"current_count": self._daily_order_count, "cap": max_daily_orders}
                ))

            # 9. Daily trade count cap
            max_daily_trades = self._get_config('MANDATE_MAX_DAILY_TRADES', 50)
            if self._daily_trade_count >= max_daily_trades:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.DAILY_TRADE_CAP_EXCEEDED,
                    message=f"Daily trade cap ({max_daily_trades}) reached",
                    details={"current_count": self._daily_trade_count, "cap": max_daily_trades}
                ))

            # 10. Position concentration limit
            if context.account_equity > 0:
                # Check if this order would create a position exceeding concentration limit
                existing_position = context.open_positions.get(symbol, {})
                existing_notional = existing_position.get('qty', 0) * existing_position.get('current_price', 0)
                projected_notional = existing_notional + context.notional_usd
                concentration = projected_notional / context.account_equity
                max_concentration = self._get_config('MANDATE_MAX_POSITION_CONCENTRATION', 0.25)

                if concentration > max_concentration:
                    violations.append(MandateViolation(
                        violation_type=MandateViolationType.POSITION_CONCENTRATION_EXCEEDED,
                        message=f"Position concentration would be {concentration:.1%} (max {max_concentration:.1%})",
                        details={
                            "symbol": symbol,
                            "projected_notional": projected_notional,
                            "concentration": concentration,
                            "max_concentration": max_concentration
                        }
                    ))

            # 11. Sector concentration limit
            sector = getattr(self.config, 'SECTOR_MAP', {}).get(symbol, 'OTHER')
            if sector not in ('INDEX', 'OTHER') and context.account_equity > 0:
                sector_notional = sum(
                    pos.get('qty', 0) * pos.get('current_price', 0)
                    for sym, pos in context.open_positions.items()
                    if getattr(self.config, 'SECTOR_MAP', {}).get(sym, 'OTHER') == sector
                )
                projected_sector_notional = sector_notional + context.notional_usd
                sector_concentration = projected_sector_notional / context.account_equity
                max_sector_concentration = self._get_config('MANDATE_MAX_SECTOR_CONCENTRATION', 0.40)

                if sector_concentration > max_sector_concentration:
                    violations.append(MandateViolation(
                        violation_type=MandateViolationType.SECTOR_CONCENTRATION_EXCEEDED,
                        message=f"Sector {sector} concentration would be {sector_concentration:.1%} (max {max_sector_concentration:.1%})",
                        details={
                            "sector": sector,
                            "projected_notional": projected_sector_notional,
                            "concentration": sector_concentration,
                            "max_concentration": max_sector_concentration
                        }
                    ))

            # 12. Account mode validation (paper vs live)
            expected_mode = getattr(self.config, 'MANDATE_EXPECTED_ACCOUNT_MODE', None)
            if expected_mode and expected_mode.lower() not in context.account_mode.lower():
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.ACCOUNT_MODE_MISMATCH,
                    message=f"Account mode mismatch: expected {expected_mode}, got {context.account_mode}",
                    details={
                        "expected": expected_mode,
                        "actual": context.account_mode
                    }
                ))

            # 13. Minimum equity check
            min_equity = self._get_config('MANDATE_MIN_ACCOUNT_EQUITY', 1000.0)
            if context.account_equity < min_equity:
                violations.append(MandateViolation(
                    violation_type=MandateViolationType.INSUFFICIENT_EQUITY,
                    message=f"Account equity ${context.account_equity:,.2f} below minimum ${min_equity:,.2f}",
                    details={
                        "equity": context.account_equity,
                        "minimum": min_equity
                    }
                ))

        except Exception as e:
            logger.error(f"Mandate gate evaluation error: {e}")
            # Fail-closed: any evaluation error is a violation
            violations.append(MandateViolation(
                violation_type=MandateViolationType.INSUFFICIENT_EQUITY,
                message=f"Mandate evaluation failed: {e}",
                details={"error": str(e)}
            ))

        # Audit the decision
        if self.audit_ledger:
            self.audit_ledger.log_mandate_decision(
                symbol=symbol,
                side=side,
                notional_usd=context.notional_usd,
                allowed=len(violations) == 0,
                violations=violations,
                context=context
            )

        return violations

    def _is_kill_switch_active(self, context: MandateContext) -> bool:
        """Check kill switch status from multiple sources."""
        # Check context (passed from broker)
        if context.kill_switch_active:
            return True

        # Check kill switch instance
        if self.kill_switch and self.kill_switch.is_active():
            return True

        return False

    def _get_symbol_daily_notional(self, symbol: str) -> float:
        """Get aggregate entry notional for this symbol in the current session."""
        return self._daily_symbol_notional.get(symbol.upper(), 0.0)

    def record_order_submitted(self, symbol: str, notional_usd: float):
        """Record an accepted entry order for daily cap tracking.

        Count it conservatively as a trade intent: fills can arrive
        asynchronously and a restart must not reset the daily limit.
        """
        self._daily_notional_usd += notional_usd
        self._daily_order_count += 1
        self._daily_trade_count += 1
        symbol = symbol.upper()
        self._daily_symbol_notional[symbol] = self._daily_symbol_notional.get(symbol, 0.0) + notional_usd

    def record_trade_filled(self, symbol: str, notional_usd: float):
        """Record a confirmed fill when a caller has fill-level evidence."""
        # Entry caps already count the submitted order; avoid counting a fill
        # twice. Retained for integrations that track fills separately.
        return None

    def get_daily_summary(self) -> Dict:
        """Get current daily counter status."""
        return {
            "daily_notional_usd": self._daily_notional_usd,
            "daily_order_count": self._daily_order_count,
            "daily_trade_count": self._daily_trade_count,
            "last_reset_date": self._last_reset_date,
        }
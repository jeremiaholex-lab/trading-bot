import json
import logging
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Any, List
from config import Config
from strategy.adaptive_strategy import TradeSignal, MIN_ENTRY_CONFIDENCE
from .earnings_filter import EarningsFilter
from .news_filter import NewsFilter
from .congress_filter import CongressFilter
from .correlation_manager import PortfolioCorrelationManager
from .macro_calendar_filter import MacroCalendarFilter
from .spread_filter import BidAskSpreadFilter

# Safety infrastructure
from safety import KillSwitch, AuditLedger, MandateGate

logger = logging.getLogger(__name__)

@dataclass
class PositionSizeResult:
    allowed: bool
    shares: int
    cost: float
    risk_amount: float
    rejection_reason: str = ""

class RiskManager:
    """
    Enforces institutional-grade risk controls:
    1. Volatility-based position sizing
    2. Portfolio heat & open position caps
    3. Sector & correlation exposure limits
    4. Binary event & earnings blackout guards
    5. Daily drawdown circuit breaker
    6. Trailing stops and profit protection

    Now integrates with Vibe-Trading inspired safety infrastructure:
    - Kill Switch: Emergency halt
    - Audit Ledger: Immutable audit trail
    - Mandate Gate: Structural compliance (delegates to broker's mandate gate)
    """

    def __init__(
        self,
        earnings_filter: Optional[EarningsFilter] = None,
        news_filter: Optional[NewsFilter] = None,
        congress_filter: Optional[CongressFilter] = None,
        kill_switch: Optional[KillSwitch] = None,
        audit_ledger: Optional[AuditLedger] = None,
        mandate_gate: Optional[MandateGate] = None,
        correlation_manager: Optional[PortfolioCorrelationManager] = None,
        macro_calendar_filter: Optional[MacroCalendarFilter] = None,
        spread_filter: Optional[BidAskSpreadFilter] = None,
        state_path: Optional[Path] = None,
    ):
        self.max_risk_pct = Config.MAX_PORTFOLIO_RISK_PER_TRADE
        self.max_positions = Config.MAX_OPEN_POSITIONS
        self.daily_drawdown_limit = Config.DAILY_DRAWDOWN_LIMIT
        self.start_of_day_equity: Optional[float] = None
        self.circuit_breaker_tripped: bool = False
        self.session_date: Optional[str] = None
        self.state_path = state_path
        self._daily_state_untrusted = False
        self._load_daily_state()
        self.earnings_filter = earnings_filter or EarningsFilter()
        self.news_filter = news_filter or NewsFilter(
            sec_user_agent=getattr(Config, "SEC_USER_AGENT", "")
        )
        self.congress_filter = congress_filter or CongressFilter()

        # Safety infrastructure references (set by orchestrator)
        self.kill_switch = kill_switch
        self.audit_ledger = audit_ledger
        self.mandate_gate = mandate_gate

        self.macro_calendar_filter = macro_calendar_filter or MacroCalendarFilter(
            blackout_minutes_before=getattr(Config, "MACRO_BLACKOUT_MINUTES_BEFORE", 30),
            blackout_minutes_after=getattr(Config, "MACRO_BLACKOUT_MINUTES_AFTER", 30)
        )
        self.correlation_manager = correlation_manager or PortfolioCorrelationManager(
            max_allowed_correlation=getattr(Config, "MAX_PORTFOLIO_CORRELATION", 0.75)
        )
        self.spread_filter = spread_filter or BidAskSpreadFilter(
            max_spread_pct=getattr(Config, "MAX_SPREAD_PCT", 0.0015)
        )

    @staticmethod
    def _today_et() -> str:
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    def _load_daily_state(self) -> None:
        """Restore today's loss baseline and latch across process restarts."""
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not {
                "session_date", "start_of_day_equity", "circuit_breaker_tripped"
            }.issubset(data):
                raise ValueError("risk state is missing required fields")
            if data.get("session_date") != self._today_et():
                return
            baseline = float(data["start_of_day_equity"])
            if not math.isfinite(baseline) or baseline <= 0:
                return
            self.start_of_day_equity = baseline
            self.session_date = data["session_date"]
            self.circuit_breaker_tripped = bool(data.get("circuit_breaker_tripped", False))
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            # A malformed same-session state may have contained a tripped
            # breaker. Fail closed until the next session establishes fresh state.
            self._daily_state_untrusted = True
            self.session_date = self._today_et()
            logger.critical("Could not restore daily risk state from %s: %s", self.state_path, exc)

    def _save_daily_state(self) -> bool:
        if self.state_path is None:
            return True
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "session_date": self.session_date,
                "start_of_day_equity": self.start_of_day_equity,
                "circuit_breaker_tripped": self.circuit_breaker_tripped,
            }, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
            self._daily_state_untrusted = False
            return True
        except OSError as exc:
            # Never continue entering if a breaker/baseline cannot survive restart.
            self._daily_state_untrusted = True
            logger.critical("Could not persist daily risk state: %s", exc)
            return False

    def update_start_of_day_equity(self, current_equity: float, session_date: Optional[str] = None):
        """Initialize today's baseline once; reset only when the session changes."""
        session_date = session_date or self._today_et()
        if not isinstance(current_equity, (int, float)) or not math.isfinite(current_equity) or current_equity <= 0:
            logger.error("Refusing invalid start-of-day equity: %s", current_equity)
            return
        # A corrupt/unwritable state for the current session must not be
        # silently replaced by a fresh baseline on process startup.
        if self._daily_state_untrusted and self.session_date == session_date and self.start_of_day_equity is None:
            return
        if self.session_date != session_date or self.start_of_day_equity is None:
            self.session_date = session_date
            self.start_of_day_equity = float(current_equity)
            self.circuit_breaker_tripped = False
            self._save_daily_state()

    def check_circuit_breaker(self, current_equity: float) -> bool:
        """
        Returns True if the circuit breaker is tripped due to daily drawdown limit.
        Halts all new entry orders.
        """
        if self._daily_state_untrusted or self.circuit_breaker_tripped:
            return True
        if not isinstance(current_equity, (int, float)) or not math.isfinite(current_equity) or current_equity <= 0:
            logger.critical("Invalid account equity for drawdown check: %s", current_equity)
            self._daily_state_untrusted = True
            return True
        if self.start_of_day_equity is None or self.start_of_day_equity <= 0:
            self.update_start_of_day_equity(current_equity)
            return self.circuit_breaker_tripped

        drawdown = (self.start_of_day_equity - current_equity) / self.start_of_day_equity
        if drawdown >= self.daily_drawdown_limit:
            if not self.circuit_breaker_tripped:
                logger.critical(
                    f"CIRCUIT BREAKER TRIGGERED: Daily drawdown of {drawdown*100:.2f}% "
                    f"exceeds limit {self.daily_drawdown_limit*100:.2f}%. Halting new entries."
                )
                self.circuit_breaker_tripped = True
                self._save_daily_state()
            return True

        # Once hit, the daily breaker remains latched until the next session.
        return False

    def calculate_position_size(
        self,
        signal: TradeSignal,
        portfolio_equity: float,
        cash_available: float,
        current_open_positions_count: int,
        already_holding_symbol: bool,
        open_positions: Optional[Dict[str, Any]] = None,
        price_history_map: Optional[Dict[str, Any]] = None,
        bid_price: float = 0.0,
        ask_price: float = 0.0,
    ) -> PositionSizeResult:
        """
        Calculates exact number of shares to buy using strict volatility risk sizing
        and validates institutional risk gates (drawdown, correlation, earnings).
        Formula:
            Max Risk $ = Equity * MAX_RISK_PCT
            Risk Per Share = Entry Price - Stop Loss Price
            Target Shares = Max Risk $ / Risk Per Share
        """
        # 0. Kill Switch check - absolute override
        if self.kill_switch and self.kill_switch.is_active():
            if self.audit_ledger:
                self.audit_ledger.log_risk_event(
                    event_type="kill_switch_block",
                    message=f"Order blocked by active kill switch: {signal.symbol}",
                    symbol=signal.symbol,
                    context={'side': 'buy', 'signal_confidence': signal.confidence},
                )
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason="Kill switch active - all new entries blocked."
            )

        # 1. Circuit breaker check
        if self.check_circuit_breaker(portfolio_equity):
            if self.audit_ledger:
                self.audit_ledger.log_risk_event(
                    event_type="circuit_breaker_block",
                    message=f"Order blocked by circuit breaker: {signal.symbol}",
                    symbol=signal.symbol,
                    context={'drawdown_pct': (self.start_of_day_equity - portfolio_equity) / self.start_of_day_equity * 100},
                )
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason="Daily drawdown circuit breaker active."
            )

        # 2. Existing position check
        if already_holding_symbol:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason=f"Position already open for {signal.symbol}."
            )

        # 3. Position count limit
        if current_open_positions_count >= self.max_positions:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason=f"Max concurrent positions ({self.max_positions}) reached."
            )

        # 4. Sector & Correlation Exposure Cap
        if open_positions:
            candidate_sector = Config.SECTOR_MAP.get(signal.symbol, "OTHER")
            if candidate_sector not in ("INDEX", "OTHER"):
                sector_positions = [
                    sym for sym in open_positions
                    if Config.SECTOR_MAP.get(sym, "OTHER") == candidate_sector
                ]
                if len(sector_positions) >= Config.MAX_POSITIONS_PER_SECTOR:
                    return PositionSizeResult(
                        allowed=False,
                        shares=0,
                        cost=0.0,
                        risk_amount=0.0,
                        rejection_reason=(
                            f"Sector exposure limit reached: already holding {len(sector_positions)} position(s) "
                            f"in sector '{candidate_sector}' (max {Config.MAX_POSITIONS_PER_SECTOR}). "
                            f"Active: {', '.join(sector_positions)}."
                        )
                    )

        # 4b. Dynamic Portfolio Correlation Cap
        if open_positions and price_history_map and self.correlation_manager:
            cand_series = price_history_map.get(signal.symbol)
            if cand_series is not None and hasattr(cand_series, "columns"):
                close_col = next((c for c in cand_series.columns if str(c).lower() == "close"), None)
                if close_col:
                    cand_series = cand_series[close_col]
            if cand_series is not None:
                existing_series = {}
                for open_sym in open_positions:
                    p_series = price_history_map.get(open_sym)
                    if p_series is not None:
                        if hasattr(p_series, "columns"):
                            c_col = next((c for c in p_series.columns if str(c).lower() == "close"), None)
                            if c_col:
                                p_series = p_series[c_col]
                        existing_series[open_sym] = p_series

                corr_ok, corr_reason = self.correlation_manager.validate_candidate_correlation(
                    signal.symbol, cand_series, existing_series
                )
                if not corr_ok:
                    return PositionSizeResult(
                        allowed=False,
                        shares=0,
                        cost=0.0,
                        risk_amount=0.0,
                        rejection_reason=corr_reason
                    )

        # 5. Binary Event & Earnings Blackout Guard
        if Config.ENABLE_EARNINGS_FILTER and self.earnings_filter:
            in_blackout, earnings_reason = self.earnings_filter.is_in_blackout(
                signal.symbol, blackout_days=Config.EARNINGS_BLACKOUT_DAYS
            )
            if in_blackout:
                if self.audit_ledger:
                    self.audit_ledger.log_risk_event(
                        event_type="earnings_blackout_block",
                        message=f"Order blocked by earnings blackout: {signal.symbol}",
                        symbol=signal.symbol,
                        context={'reason': earnings_reason},
                    )
                return PositionSizeResult(
                    allowed=False,
                    shares=0,
                    cost=0.0,
                    risk_amount=0.0,
                    rejection_reason=f"Earnings Blackout: {earnings_reason}."
                )

        # 5b. Breaking-News / Event-Risk Guard
        # Blocks entries on material event risk (important SEC 8-K filings,
        # risk-keyword headlines) within the lookback window. A fetch failure
        # never blocks: the filter logs and allows, so an outage cannot halt
        # trading on a false positive.
        if Config.ENABLE_NEWS_FILTER and self.news_filter:
            news_blocked, news_reason = self.news_filter.is_blocked(
                signal.symbol,
                hours=Config.NEWS_BLACKOUT_HOURS,
                important_only=Config.NEWS_IMPORTANT_ONLY,
            )
            if news_blocked:
                logger.warning(
                    "News gate blocked %s: %s", signal.symbol, news_reason
                )
                if self.audit_ledger:
                    self.audit_ledger.log_risk_event(
                        event_type="news_gate_block",
                        message=f"Order blocked by news gate: {signal.symbol}",
                        symbol=signal.symbol,
                        context={'reason': news_reason},
                    )
                return PositionSizeResult(
                    allowed=False,
                    shares=0,
                    cost=0.0,
                    risk_amount=0.0,
                    rejection_reason=f"News Risk: {news_reason}."
                )

        # 4c. Real-Time Bid-Ask Spread Gate
        if self.spread_filter and bid_price > 0 and ask_price > 0:
            spread_ok, spread_reason = self.spread_filter.validate_spread(
                symbol=signal.symbol,
                bid_price=bid_price,
                ask_price=ask_price
            )
            if not spread_ok:
                return PositionSizeResult(
                    allowed=False,
                    shares=0,
                    cost=0.0,
                    risk_amount=0.0,
                    rejection_reason=f"Spread Gate: {spread_reason}"
                )

        # 5b. Macroeconomic Calendar Gate (FOMC / CPI / NFP)
        if getattr(Config, "ENABLE_MACRO_CALENDAR_FILTER", True) and self.macro_calendar_filter:
            in_macro_blackout, macro_reason = self.macro_calendar_filter.is_in_macro_blackout()
            if in_macro_blackout:
                return PositionSizeResult(
                    allowed=False,
                    shares=0,
                    cost=0.0,
                    risk_amount=0.0,
                    rejection_reason=f"Macro Calendar Gate: {macro_reason}."
                )

        # 6. Minimum confidence filter
        if signal.confidence < MIN_ENTRY_CONFIDENCE:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason=(
                    f"Signal confidence {signal.confidence:.2f} below threshold "
                    f"{MIN_ENTRY_CONFIDENCE:.2f}."
                )
            )

        entry = signal.entry_price
        stop_loss = signal.stop_loss_price

        if entry <= 0 or stop_loss >= entry:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason="Invalid entry or stop loss price relationship."
            )

        risk_per_share = entry - stop_loss
        max_risk_dollars = portfolio_equity * self.max_risk_pct

        # Number of shares based on dollar risk
        target_shares = int(max_risk_dollars // risk_per_share)
        if target_shares <= 0:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason="Portfolio equity or risk allowance too low for 1 full share."
            )

        total_cost = target_shares * entry

        # Cap single position size to 25% of total portfolio equity
        max_capital_per_pos = portfolio_equity * 0.25
        if total_cost > max_capital_per_pos:
            target_shares = int(max_capital_per_pos // entry)
            total_cost = target_shares * entry

        # Ensure cash buffer
        usable_cash = cash_available * (1.0 - Config.MIN_CASH_BUFFER_PCT)
        if total_cost > usable_cash:
            target_shares = int(usable_cash // entry)
            total_cost = target_shares * entry

        if target_shares <= 0:
            return PositionSizeResult(
                allowed=False,
                shares=0,
                cost=0.0,
                risk_amount=0.0,
                rejection_reason="Insufficient cash after preserving minimum liquidity buffer."
            )

        actual_risk_dollars = target_shares * risk_per_share

        # Log successful risk check
        if self.audit_ledger:
            self.audit_ledger.log_risk_event(
                event_type="position_size_calculated",
                message=f"Position size calculated for {signal.symbol}: {target_shares} shares",
                symbol=signal.symbol,
                context={
                    'target_shares': target_shares,
                    'total_cost': total_cost,
                    'risk_amount': actual_risk_dollars,
                    'entry_price': entry,
                    'stop_loss': stop_loss,
                },
            )

        return PositionSizeResult(
            allowed=True,
            shares=target_shares,
            cost=round(total_cost, 2),
            risk_amount=round(actual_risk_dollars, 2),
            rejection_reason=""
        )

    def calculate_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        current_stop: float,
        atr: float,
        atr_multiplier: float
    ) -> float:
        """
        Dynamic profit locking:
        - If profit > 1.0R, move stop to break-even + fee buffer.
        - If price reaches higher highs, trail by ATR multiplier.
        """
        initial_risk = entry_price - current_stop
        unrealized_gain = current_price - entry_price

        # Step 1: Move to Break-Even if profit >= 1.0R
        new_stop = current_stop
        if unrealized_gain >= initial_risk:
            breakeven_stop = entry_price + (atr * 0.2) # Lock in fee buffer
            new_stop = max(new_stop, breakeven_stop)

        # Step 2: ATR Trailing Stop
        trailing_stop = current_price - (atr * atr_multiplier)
        new_stop = max(new_stop, trailing_stop)

        # A trailing stop must never loosen
        new_stop = max(new_stop, current_stop)

        return round(new_stop, 2)

    def log_congress_advisories(self, symbols: Optional[List[str]] = None):
        """Logs fresh congressional STOCK Act disclosures touching the watchlist.

        Advisory only (an alpha signal, never a block) and off by default.
        Fails soft: a congressional-data outage is logged and ignored.
        """
        if not getattr(Config, "ENABLE_CONGRESS_WATCH", False):
            return
        if not symbols:
            symbols = Config.WATCHLIST
        try:
            matches = self.congress_filter.recent_trades(
                symbols,
                since_days=getattr(Config, "CONGRESS_SINCE_DAYS", 45),
            )
        except Exception as exc:
            logger.warning("Congress watch failed (%s); skipping", exc)
            return
        for m in matches:
            logger.info(
                "Congress disclosure: %s (%s) traded %s on %s — doc %s %s",
                m.get("member", "?"), m.get("district", "?"),
                ", ".join(m.get("matched_tickers", m.get("tickers", []))),
                m.get("filed", "?"), m.get("doc_id", "?"), m.get("url", ""),
            )

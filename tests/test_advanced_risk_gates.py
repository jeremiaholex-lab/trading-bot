import pytest
from datetime import date, timedelta
from pathlib import Path
import tempfile
import pandas as pd
from config import Config
from risk.earnings_filter import EarningsFilter
from risk.risk_manager import RiskManager
from strategy.macro_filter import MacroTrendFilter
from strategy.adaptive_strategy import TradeSignal
from execution.alpaca_broker import AlpacaBroker
from bot import TradingBotOrchestrator


class FakeEarningsFilter(EarningsFilter):
    def __init__(self, earnings_dates_map):
        super().__init__()
        self.earnings_dates_map = earnings_dates_map

    def get_next_earnings_date(self, symbol: str):
        return self.earnings_dates_map.get(symbol)


@pytest.fixture
def sample_signal():
    return TradeSignal(
        action="BUY",
        symbol="NVDA",
        entry_price=200.0,
        stop_loss_price=190.0,
        take_profit_price=220.0,
        confidence=0.85,
        regime="TRENDING_BULL",
        reason="Bullish test",
        metadata={}
    )


def test_earnings_filter_blackout_logic():
    today = date(2026, 10, 27)
    # Earnings tomorrow -> in blackout
    filter_inst = FakeEarningsFilter({"NVDA": date(2026, 10, 28)})
    in_bo, reason = filter_inst.is_in_blackout("NVDA", blackout_days=2, current_date=today)
    assert in_bo is True
    assert "in 1 day(s)" in reason

    # Earnings in 10 days -> outside blackout
    filter_inst = FakeEarningsFilter({"NVDA": date(2026, 11, 6)})
    in_bo, reason = filter_inst.is_in_blackout("NVDA", blackout_days=2, current_date=today)
    assert in_bo is False

    # Symbol with no earnings (e.g. SPY)
    filter_inst = FakeEarningsFilter({})
    in_bo, reason = filter_inst.is_in_blackout("SPY", blackout_days=2, current_date=today)
    assert in_bo is False


def test_risk_manager_blocks_on_earnings_blackout(sample_signal):
    fake_ef = FakeEarningsFilter({"NVDA": date.today() + timedelta(days=1)})
    rm = RiskManager(earnings_filter=fake_ef)
    rm.update_start_of_day_equity(100000.0)

    res = rm.calculate_position_size(
        signal=sample_signal,
        portfolio_equity=100000.0,
        cash_available=50000.0,
        current_open_positions_count=0,
        already_holding_symbol=False,
    )
    assert res.allowed is False
    assert "Earnings Blackout" in res.rejection_reason


def test_sector_exposure_cap_blocks_excess_positions(sample_signal):
    fake_ef = FakeEarningsFilter({})
    rm = RiskManager(earnings_filter=fake_ef)
    rm.update_start_of_day_equity(100000.0)

    open_pos = {
        "AMD": {"qty": 10, "current_price": 500.0},
        "AVGO": {"qty": 5, "current_price": 800.0},
    }
    Config.SECTOR_MAP["AVGO"] = "SEMIS"

    res = rm.calculate_position_size(
        signal=sample_signal,  # NVDA is SEMIS
        portfolio_equity=100000.0,
        cash_available=50000.0,
        current_open_positions_count=2,
        already_holding_symbol=False,
        open_positions=open_pos,
    )
    assert res.allowed is False
    assert "Sector exposure limit reached" in res.rejection_reason
    assert "SEMIS" in res.rejection_reason


def test_macro_trend_filter_blocks_counter_trend_buy():
    filter_inst = MacroTrendFilter()
    macro_state = {
        "is_bearish": True,
        "bearish_benchmarks": ["SPY", "QQQ"],
    }

    closes = [120.0 - i * 1.0 for i in range(30)]
    daily_df = pd.DataFrame({
        "Open": closes,
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
        "Close": closes,
        "Volume": [1000000] * 30,
    })

    ok, reason = filter_inst.validate_candidate_long("NVDA", daily_df, macro_state)
    assert ok is False
    assert "Macro Trend Gate" in reason
    assert "below its Daily 21 EMA" in reason


def test_macro_trend_filter_permits_relative_strength_leader():
    filter_inst = MacroTrendFilter()
    macro_state = {
        "is_bearish": True,
        "bearish_benchmarks": ["SPY"],
    }

    closes = [100.0 + i * 2.0 for i in range(30)]
    daily_df = pd.DataFrame({
        "Open": closes,
        "High": [c + 2.0 for c in closes],
        "Low": [c - 2.0 for c in closes],
        "Close": closes,
        "Volume": [1000000] * 30,
    })

    ok, reason = filter_inst.validate_candidate_long("AAPL", daily_df, macro_state)
    assert ok is True
    assert "Relative strength confirmed" in reason


def test_simulator_partial_close(tmp_path):
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim_state.json")
    broker.sim_cash = 100000.0
    broker.sim_positions = {}
    broker.place_bracket_buy_order(
        symbol="AAPL", qty=10, entry_price=150.0,
        stop_loss_price=140.0, take_profit_price=170.0,
    )
    assert broker.sim_positions["AAPL"].qty == 10
    assert broker.sim_cash == 100000.0 - 1500.0

    broker.sim_positions["AAPL"].current_price = 160.0
    res = broker.place_close_position_order(symbol="AAPL", qty=5, reason="50% scale-out")

    assert res.success is True
    assert "Partially closed 5 shares" in res.message
    assert broker.sim_positions["AAPL"].qty == 5
    assert broker.sim_cash == (100000.0 - 1500.0) + 800.0


def test_trailing_stop_triggers_partial_scale_out(tmp_path):
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim_state.json")
    broker.sim_cash = 100000.0
    broker.sim_positions = {}
    broker.place_bracket_buy_order("AAPL", 10, 100.0, 90.0, 120.0)

    orch = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    orch.broker = broker
    orch.risk_mgr = RiskManager()
    orch._scaled_out_positions = set()
    orch.notifier = None

    class FakeMarketData:
        def get_historical_bars(self, *a, **kw):
            return pd.DataFrame({
                "Open": [100.0] * 30, "High": [102.0] * 30, "Low": [98.0] * 30,
                "Close": [100.0] * 30, "Volume": [1000] * 30
            })
    orch.market_data = FakeMarketData()

    price_map = {"AAPL": 115.0}
    broker.sim_positions["AAPL"].current_price = 115.0
    orch._apply_trailing_stops(price_map)

    # 5 shares scaled out, 5 remain
    assert broker.sim_positions["AAPL"].qty == 5
    assert "AAPL" in orch._scaled_out_positions
    # Remaining stop moved to breakeven + buffer
    assert broker.sim_positions["AAPL"].stop_loss >= 100.0


def test_simulated_bracket_exit_records_in_journal_and_session(tmp_path):
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim_state.json")
    broker.sim_cash = 100000.0
    broker.sim_positions = {}
    broker.place_bracket_buy_order("SPY", 10, 500.0, 490.0, 520.0)

    from execution.trade_journal import TradeJournal
    journal = TradeJournal(db_path=tmp_path / "trades.db")
    journal.record_entry("SPY", 10, 500.0, 490.0, 520.0)

    orch = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    orch.broker = broker
    orch.risk_mgr = RiskManager()
    from optimizer.self_improver import SelfImprover
    orch.self_improver = SelfImprover(state_file=tmp_path / "state.json")
    orch._journal = journal
    orch._scaled_out_positions = {"SPY"}
    orch.notifier = None
    orch._session_trades = []
    orch._session_warnings = []
    orch._session_scans = 0

    class FakeMarketData:
        def get_historical_bars(self, sym, *a, **kw):
            return pd.DataFrame({
                "Open": [485.0] * 50,
                "High": [486.0] * 50,
                "Low": [484.0] * 50,
                "Close": [485.0] * 50,
                "Volume": [1000000] * 50
            })
    orch.market_data = FakeMarketData()

    signals = orch.scan_and_trade()

    # Position closed in broker
    assert "SPY" not in broker.sim_positions
    # Trade registered in session trades
    assert len(orch._session_trades) == 1
    assert orch._session_trades[0]["symbol"] == "SPY"
    assert orch._session_trades[0]["side"] == "SELL"
    # Position cleared from _scaled_out_positions
    assert "SPY" not in orch._scaled_out_positions
    # Trade in journal closed
    summary = journal.get_performance_summary()
    assert summary["total_trades"] == 1
    assert summary["losing_trades"] == 1

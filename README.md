# 🤖 Adaptive AI Trading Bot (Stocks & ETFs)

An institutional-grade, self-improving quantitative trading bot designed for US Equities and ETFs. Built to trade with strict risk management, regime awareness, and automated parameter adaptation.

---

## 🌟 Key Features

1. **Market Regime Detection**:
   - Classifies market conditions into **Bull Trend**, **Bear Trend**, **Ranging (Mean Reversion)**, and **High Volatility**.
   - Refuses to buy into falling knives or choppy volatility spikes.
2. **Asymmetric Risk/Reward Strategy**:
   - Buys low-risk pullbacks in confirmed uptrends (tests of dynamic EMA support with RSI dip + volume confirmation).
   - Exploits Bollinger Band mean reversion during ranging markets.
   - Requires a minimum 1.8x to 2.5x Reward-to-Risk ratio on all entries.
3. **Institutional Risk Management ("Protecting Capital")**:
   - **Volatility Sizing**: Dynamically calculates position size so that no trade risks more than 1.5% of portfolio equity (based on Average True Range).
   - **Server-Side Bracket Orders**: Every trade automatically attaches an OCO (One-Cancels-Other) Stop-Loss and Take-Profit.
   - **Daily Circuit Breaker**: Automatically halts trading if daily drawdown reaches 3%.
4. **Self-Improving Parameter Optimizer**:
   - Runs **Walk-Forward Validation** (70% train / 30% test) to dynamically self-tune strategy parameters (EMAs, RSI buy zones, ATR stops).
   - The held-out slice is scored **once**, on the single candidate that won the training phase. Ranking candidates by their held-out score would turn the test set into a second training set.
   - New parameters are adopted only if they clear the the held-out gates: `MIN_FITNESS_TO_ADOPT` (default `0.0`), `MIN_TRADES_TO_ADOPT` (default `3`), and `MIN_FITNESS_IMPROVEMENT` (default `0.25`) — a required **margin over the parameters already in use**. The margin gate exists because a rolling-origin calibration over 311 (symbol, origin) pairs showed that absolute fitness does not predict a good forward outcome; the size of the improvement over the baseline does.
   - The parameter search runs on its own timeframe (`OPTIMIZATION_TIMEFRAME`, default `1Hour`) with a long window (`OPTIMIZATION_LOOKBACK_DAYS`, default `400`). Alpaca serves only about 60 days of 15-minute bars, which produced 1–3 held-out trades — too few to ever clear a 3-trade gate, so the optimiser never adopted anything.
   - The search is seeded (`OPTIMIZER_SEED`), so calibration is reproducible.
   - Stores and updates optimal parameters per ticker in `strategy_state.json`.
5. **Zero-Config Paper Simulator + Alpaca Cloud Integration**:
   - Runs out-of-the-box with a built-in virtual **$100,000 Paper Simulator**, whose positions and cash are persisted across restarts.
   - Seamlessly connects to **Alpaca Markets** (Paper or Live) simply by setting your API keys in `.env`.
   - If credentials are present but Alpaca rejects them, the bot **refuses to start** rather than silently downgrading to the simulator. Set `ALLOW_SIMULATOR_FALLBACK=True` to override.

---

## 🚀 Quick Start (One Command / One Click)

### 1. Launch the Autonomous Bot
Simply double-click **`start_bot.bat`** or run:
```bash
python bot.py
```
**That is it!** When enabled, the bot automatically:
1. Verifies your connection (Alpaca or safe local simulation).
2. Checks if parameters need self-improvement calibration for today's market.
3. Scans your watchlist constantly on autopilot.
4. Places disciplined bracket orders (with automatic Stop-Loss & Take-Profit).
5. Monitors open positions and manages trailing stops.
6. Re-tunes parameters automatically every 24 hours in the background.

---

## 🛠️ Commands & Usage

### 🔍 1. Market Scanner
Scans your watchlist (`SPY, QQQ, AAPL, NVDA, MSFT, TSLA, AMD`), detects regimes, computes indicators, and checks for high-conviction setups:
```bash
python bot.py --scan
```

### 🧠 2. Self-Improvement Parameter Tuning
Runs the walk-forward optimizer to tune parameters on recent market data:
```bash
# Optimize a single symbol
python bot.py --optimize --symbol SPY

# Optimize the entire watchlist
python bot.py --optimize
```

### 📊 3. Backtest Analysis
Generates an institutional quantitative performance report (Win Rate, Profit Factor, Sharpe Ratio, Max Drawdown):
```bash
python bot.py --backtest SPY
```

### 💼 4. Account & Portfolio Status
Inspects account equity, available cash, buying power, and active open positions:
```bash
python bot.py --status
```

### 🔄 5. Live Automated Trading Loop
Starts the automated loop (scans every 60s, manages trailing stops, executes setups, and runs daily self-improvement cycles):
```bash
python bot.py --run --interval 60
```

---

## ⚙️ Configuration Parameters (`.env` or `config.py`)

| Setting | Default | Description |
| :--- | :--- | :--- |
| `ALPACA_API_KEY` | - | Your Alpaca Paper/Live API Key |
| `ALPACA_SECRET_KEY` | - | Your Alpaca Secret Key |
| `ALPACA_LIVE` | `False` | `False` for Paper Trading, `True` for Live Real Money |
| `ALLOW_SIMULATOR_FALLBACK` | `False` | Allow the bot to run on the simulator when Alpaca rejects your credentials |
| `WATCHLIST` | `SPY,QQQ,AAPL,...` | Liquid stocks and ETFs to monitor |
| `BAR_TIMEFRAME` | `15Min` | Bar timeframe for analysis (`1Min`, `5Min`, `15Min`, `1Hour`, `1Day`) |
| `MAX_PORTFOLIO_RISK_PER_TRADE`| `0.015` (1.5%) | Max portfolio equity risked on any single trade |
| `MAX_OPEN_POSITIONS` | `5` | Maximum concurrent positions allowed |
| `DAILY_DRAWDOWN_LIMIT` | `0.03` (3%) | Daily loss limit to activate circuit breaker |
| `PROFIT_TARGET_RR` | `2.0` | Target profit ratio (2.0x ATR risk) |
| `OPTIMIZATION_TIMEFRAME` | `1Hour` | Timeframe the parameter search reads, independent of `BAR_TIMEFRAME` |
| `OPTIMIZATION_LOOKBACK_DAYS` | `400` | Window of history used to tune parameters |
| `MIN_FITNESS_TO_ADOPT` | `0.0` | Absolute out-of-sample fitness floor for adopting tuned parameters |
| `MIN_TRADES_TO_ADOPT` | `3` | Minimum out-of-sample trades required to trust a tuned parameter set |
| `MIN_FITNESS_IMPROVEMENT` | `0.25` | Required margin over the parameters already in use |
| `OPTIMIZER_SEED` | `42` | Seed for the parameter search (makes calibration reproducible) |

---

## 📁 Project Structure

```
trading Ai/
├── config.py                 # Configuration loader and environment manager
├── bot.py                    # Master CLI orchestrator & rich dashboard
├── requirements.txt          # Python dependencies
├── pytest.ini                # Scopes test collection to tests/ only
├── strategy_state.json       # Persisted self-improved parameters per ticker
├── reset_strategy_state.py   # Clears unvalidated parameters (keeps a backup)
├── data/
│   └── market_data.py        # Universal data provider (Alpaca + Yahoo Finance fallback)
├── indicators/
│   └── technicals.py         # ATR, RSI, EMAs, MACD, Bollinger Bands, Regime Classifier
├── strategy/
│   ├── adaptive_strategy.py  # Regime-aware entry/exit signal generator
│   └── scanner.py            # Conviction ranking across the watchlist
├── risk/
│   └── risk_manager.py       # Volatility position sizing, circuit breakers, stops
├── optimizer/
│   ├── backtester.py         # Performance evaluation (Sharpe, Profit Factor, Drawdown)
│   └── self_improver.py      # Walk-forward parameter optimizer
├── execution/
│   ├── alpaca_broker.py      # Alpaca trading client + local paper simulator
│   └── webhook.py            # Discord / Telegram notifications
└── tests/                    # pytest suite (safety, optimizer, live loop)
```

---

## 🧪 Testing

```bash
python -m pytest              # runs the whole suite
python -m pytest -v           # verbose
python -m pytest tests/test_optimizer_integrity.py
```

Test collection is scoped to `tests/` via `pytest.ini`. This matters: an
earlier version had a root-level `test_scan.py` that pytest *imported* on
every run, which executed the live trading loop and submitted real Alpaca
orders. `tests/test_safety.py` now guards against that recurring.

---

## 🔑 Credential Check

```bash
python set_alpaca_keys.py --check     # validate the keys in .env
python set_alpaca_keys.py              # set new keys (input is hidden)
```

If the bot logs `BrokerAuthenticationError`, your keys were rejected. It will
not silently trade on the simulator — set `ALLOW_SIMULATOR_FALLBACK=True` if
that is what you want.

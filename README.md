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
   - Guards against curve-fitting by validating candidate parameters out-of-sample before adopting them.
   - Stores and updates optimal parameters per ticker in `strategy_state.json`.
5. **Zero-Config Paper Simulator + Alpaca Cloud Integration**:
   - Runs out-of-the-box with a built-in virtual **$100,000 Paper Simulator**.
   - Seamlessly connects to **Alpaca Markets** (Paper or Live) simply by setting your API keys in `.env`.

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
| `WATCHLIST` | `SPY,QQQ,AAPL,...` | Liquid stocks and ETFs to monitor |
| `BAR_TIMEFRAME` | `15Min` | Bar timeframe for analysis (`5Min`, `15Min`, `1Hour`, `1Day`) |
| `MAX_PORTFOLIO_RISK_PER_TRADE`| `0.015` (1.5%) | Max portfolio equity risked on any single trade |
| `MAX_OPEN_POSITIONS` | `5` | Maximum concurrent positions allowed |
| `DAILY_DRAWDOWN_LIMIT` | `0.03` (3%) | Daily loss limit to activate circuit breaker |
| `PROFIT_TARGET_RR` | `2.0` | Target profit ratio (2.0x ATR risk) |

---

## 📁 Project Structure

```
trading Ai/
├── config.py                 # Configuration loader and environment manager
├── bot.py                    # Master CLI orchestrator & rich dashboard
├── requirements.txt          # Python dependencies
├── strategy_state.json       # Persisted self-improved parameters per ticker
├── data/
│   └── market_data.py        # Universal data provider (Alpaca + Yahoo Finance fallback)
├── indicators/
│   └── technicals.py         # ATR, RSI, EMAs, MACD, Bollinger Bands, Regime Classifier
├── strategy/
│   └── adaptive_strategy.py  # Regime-aware entry/exit signal generator
├── risk/
│   └── risk_manager.py       # Volatility position sizing, circuit breakers, stops
├── optimizer/
│   ├── backtester.py         # Performance evaluation (Sharpe, Profit Factor, Drawdown)
│   └── self_improver.py      # Walk-forward parameter optimizer
└── execution/
    └── alpaca_broker.py      # Alpaca trading client + local paper simulator
```

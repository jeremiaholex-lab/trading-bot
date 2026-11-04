# 🤖 Adaptive Quant Trading Bot — US Equities & ETFs

A self-improving algorithmic trading bot for US stocks and ETFs. It reads the market, classifies the regime, sizes its risk, and places bracket orders through Alpaca — then re-tunes its own parameters overnight and tells you what happened.

It ships with a **$100,000 paper simulator** and runs with zero configuration. Add Alpaca keys when you want it to talk to a real account.

---

## What makes it different

Most trading bots fail in a way that *looks* like success: they trade nothing, they adopt bad parameters, or they look healthy while quietly leaking your account number into a git commit. This one is built around making each of those failures loud.

### 1. Nothing fails silently

| Failure | What this bot does |
| :--- | :--- |
| Alpaca rejects your credentials | **Refuses to start.** Falling back to the simulator would look operational while placing zero orders. Opt out explicitly with `ALLOW_SIMULATOR_FALLBACK=True`. |
| The optimiser finds a candidate | It must clear an **absolute floor**, a **minimum trade count**, *and* a **margin over the parameters already in use**. A candidate that merely "beats the baseline" is not enough. |
| A test run happens | `pytest.ini` scopes collection to `tests/`. A root-level script that executed the live trading loop at import time (submitting real orders on every `pytest` run) is guarded against by `tests/test_safety.py`. |
| An output file contains your account number | A test scans every untracked file git would publish and fails if any contains an Alpaca account number. It checks the *content*, not a list of filenames. |

The theme: **a bot that looks healthy while doing nothing is the worst possible failure mode**, because it costs you nothing and teaches you nothing.

### 2. The optimiser is calibrated, not guessed

A rolling-origin walk-forward over **311 (symbol, origin) pairs** across seven liquid symbols, with a third untouched forward slice measuring what actually happened after each adoption, produced two findings that shaped the gates:

- **Absolute fitness does not predict success.** Candidates scoring above 3.0 did no better forward than the defaults. The `[-999, -0.5)` bucket actually performed best.
- **The size of the improvement does.** Hence `MIN_FITNESS_IMPROVEMENT` — a required margin over the parameters currently in use.

The gate calibration from that study:

| Gate | Adoption rate | Mean forward P&L | % positive | vs. baseline |
| :--- | :--- | :--- | :--- | :--- |
| Never adopt (defaults) | — | +0.21% | 41% | — |
| Fitness ≥ 0, trades ≥ 3 | 3% | **+1.50%** | **62%** | **+0.18%** |
| Fitness ≥ 0, trades ≥ 2 | 14% | +0.76% | 51% | −0.25% |

Loosening the trade gate raises the adoption rate but loses the edge. The shipped defaults sit at the row that worked.

<p align="center"><img src="docs/optimizer-gates.svg" alt="Walk-forward optimizer and adoption gates: the best candidate is picked on the 70% train set, scored exactly once on the 30% holdout, and must pass three gates — fitness at least zero, at least 0.25 improvement over the parameters in use, and at least three holdout trades — before adoption." width="820"></p>
<p align="center"><em>Adoption is a conjunction, not a threshold. The candidate is chosen on the train set and scored exactly once on the holdout; all three gates must pass, otherwise the defaults stay.</em></p>

### 3. The search runs on its own timeframe

Live signals use 15-minute bars. The parameter search uses **1-hour bars over 400 days**.

This separation is not cosmetic. Alpaca serves only ~1,534 bars of 15-minute history (~83 days), which produced 1–3 held-out trades — a sample too small to distinguish a good parameter set from a bad one. The optimiser simply could not work, and no amount of gate-tuning fixed it. At 1-hour bars Alpaca serves ~2 years, yielding 20+ training trades and 8+ held-out trades. The parameters are all timeframe-agnostic ratios (EMA spans, RSI levels, ATR multiples), so they transfer to the live bar size and the ATR stop rescales itself.

---

## Features

**Market regime detection** — classifies conditions into `TRENDING_BULL`, `TRENDING_BEAR`, `RANGING`, or `HIGH_VOLATILITY` from EMA structure, MACD, ATR percentile, and Bollinger width. Every downstream decision branches on the regime.

<p align="center"><img src="docs/regime.svg" alt="Regime classifier decision tree: a bar is HIGH_VOLATILITY if ATR percentile spikes (that takes precedence), otherwise TRENDING_BULL or TRENDING_BEAR if the moving averages and MACD align, and RANGING otherwise." width="720"></p>
<p align="center"><em>How a bar becomes a regime. A volatility spike wins regardless of trend alignment because it is assigned last — so the strategy sits out of the market rather than trading a violent trend.</em></p>

**Asymmetric risk/reward strategy** — buys low-risk pullbacks in confirmed uptrends (EMA support test with an RSI dip and volume confirmation), and takes Bollinger mean reversion when the market is ranging. The reward-to-risk ratio is structural rather than filtered: the stop sits at 1.8× ATR and the target at `PROFIT_TARGET_RR` times that distance, so every entry is asymmetric by construction. In `HIGH_VOLATILITY` the strategy holds and preserves capital instead of entering.

**Conviction ranking** — the watchlist is scanned and ranked, so a strong setup is never crowded out by a weak one competing for the last position slot.

**Institutional risk management**
- *Volatility sizing*: no trade risks more than 1.5% of equity, sized from Average True Range.
- *Server-side bracket orders*: every entry attaches an OCO stop-loss and take-profit.
- *Trailing stops* amend the existing protective stop when supported, rather than canceling it before a replacement.
- *Daily circuit breaker*: halts new entries when the day's drawdown hits 3%.

**Layered execution safety** — new entries are checked against a local mandate (symbol allow/deny lists, order and daily notional limits, daily order counts, and position/sector concentration) before broker submission. Its leverage check compares the proposed order notional with equity; it is not a full portfolio-margin calculation. The paper/live boundary checks the client’s configured mode against the expected profile; verify the broker account in its own dashboard before enabling live trading. A restart-persistent filesystem kill switch blocks new entries but does **not** automatically flatten open positions; exits remain available so the operator can reduce risk.

**Tamper-evident audit trail** — order submissions and rejections, mandate decisions, kill-switch changes, and selected risk events are written to a SHA-256 hash-chained ledger. `AuditLedger.verify_chain()` checks the ledger’s internal links and detects edits or gaps when later entries remain. Local ledger files are not immutable storage; truncating the tail or deleting the entire ledger requires an external backup/checkpoint to detect, so protect and back up the state directory.

**Optional pre-trade advisory** — an HTTP advisory provider can return observations about an order and account context. It is disabled by default and **fail-open / observational only**: an unavailable service or a reject verdict does not prevent a mandate-compliant order. It is not a replacement for the local structural risk gates.

**Self-improving optimizer** — walk-forward validation (70/30 split), the held-out slice scored exactly once on the training-phase winner, seeded for reproducibility, persisted per ticker in `strategy_state.json`.

**Telegram / Discord notifications** — a message per filled order, a CRITICAL one if the breaker trips, and an **end-of-session report** when the market closes containing equity, daily P&L, every open position with its unrealised P&L, every trade and the reason for it, and a "worth knowing" section for anything that went wrong (tripped breaker, optimiser parameter change, or a session where nothing happened at all).

> The session report fires on the market-close *transition*, so a bot started after hours does not send a bogus report for a session it never observed.

**Zero-config paper simulator** — a $100,000 virtual account with positions and cash persisted across restarts, for running the bot before you trust it with money.

---

## Quick start

```bash
git clone https://github.com/jbolex10-lab/trading-bot.git
cd trading-bot
```

> This repository is private. GitHub no longer accepts account passwords for HTTPS clones, so authenticate with a [personal access token](https://github.com/settings/tokens) (`repo` scope) as the password, or use an SSH key.

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
copy .env.example .env         # cp on macOS/Linux

python bot.py
```

That is it. The bot connects (or starts the simulator), checks whether parameters need calibration, then scans the watchlist on autopilot — placing bracket orders, managing trailing stops, and re-tuning parameters every 24 hours in the background.

On Windows, `start_bot.bat` does the same thing in one double-click.

### Going live

1. Get [Alpaca paper trading keys](https://app.alpaca.markets) (free).
2. Put them in `.env` and validate:
   ```bash
   python set_alpaca_keys.py --check     # validate existing keys
   python set_alpaca_keys.py              # set new ones (input hidden)
   ```
3. Leave `ALPACA_LIVE=False` and let it run a full session on paper.
4. Only then set `ALPACA_LIVE=True`.

---

## Commands

| Command | What it does |
| :--- | :--- |
| `python bot.py` | Start autonomous trading (market-hours aware) |
| `python bot.py --run --interval 60` | Same, with an explicit scan interval in seconds |
| `python bot.py --scan` | One scan of the watchlist, no trading loop |
| `python bot.py --status` | Equity, cash, buying power, open positions |
| `python bot.py --report` | Send the session report to Telegram/Discord now |
| `python bot.py --optimize` | Run the walk-forward optimiser over the watchlist |
| `python bot.py --optimize --symbol SPY` | Optimise a single symbol |
| `python bot.py --backtest SPY` | Win rate, profit factor, Sharpe, max drawdown |

---

## Configuration

Everything lives in `.env` (copy from `.env.example`). `config.py` validates it at startup and refuses to run on an invalid value.

| Setting | Default | Description |
| :--- | :--- | :--- |
| `ALPACA_API_KEY` | – | Alpaca paper or live key |
| `ALPACA_SECRET_KEY` | – | Alpaca secret key |
| `ALPACA_LIVE` | `False` | `False` for paper, `True` for real money |
| `ALLOW_SIMULATOR_FALLBACK` | `False` | Deliberately run on the simulator despite bad keys |
| `WATCHLIST` | `SPY,QQQ,AAPL,NVDA,MSFT,TSLA,AMD` | Symbols to monitor |
| `BAR_TIMEFRAME` | `15Min` | Bar size for live signals (`1Min`, `5Min`, `15Min`, `1Hour`, `1Day`) |
| `MAX_PORTFOLIO_RISK_PER_TRADE` | `0.015` | Max equity risked on any single trade |
| `MAX_OPEN_POSITIONS` | `5` | Maximum concurrent positions |
| `DAILY_DRAWDOWN_LIMIT` | `0.03` | Daily loss that trips the circuit breaker |
| `PROFIT_TARGET_RR` | `2.0` | Target reward-to-risk ratio |
| `OPTIMIZATION_TIMEFRAME` | `1Hour` | Bar size the parameter search reads |
| `OPTIMIZATION_LOOKBACK_DAYS` | `400` | History window used to tune parameters |
| `MIN_FITNESS_TO_ADOPT` | `0.0` | Absolute out-of-sample fitness floor |
| `MIN_TRADES_TO_ADOPT` | `3` | Minimum out-of-sample trades to trust a result |
| `MIN_FITNESS_IMPROVEMENT` | `0.25` | Required margin over the parameters in use |
| `OPTIMIZER_SEED` | `42` | Makes the search reproducible |
| `MANDATE_MAX_NOTIONAL_PER_ORDER` | `50000.0` | Maximum USD notional for a single entry |
| `MANDATE_MAX_NOTIONAL_PER_SYMBOL` | `100000.0` | Maximum daily entry notional per symbol |
| `MANDATE_MAX_DAILY_NOTIONAL_USD` | `500000.0` | Maximum aggregate daily entry notional |
| `MANDATE_MAX_LEVERAGE` | `2.0` | Maximum projected leverage |
| `MANDATE_MAX_DAILY_ORDERS` | `100` | Maximum submitted orders per day |
| `MANDATE_MAX_DAILY_TRADES` | `50` | Maximum fills per day |
| `MANDATE_MAX_POSITION_CONCENTRATION` | `0.25` | Maximum equity fraction in one symbol |
| `MANDATE_MAX_SECTOR_CONCENTRATION` | `0.40` | Maximum equity fraction in one sector |
| `MANDATE_MIN_ACCOUNT_EQUITY` | `1000.0` | Minimum equity allowed for new entries |
| `MANDATE_EXPECTED_ACCOUNT_MODE` | `paper` or `live` | Expected broker mode; defaults from `ALPACA_LIVE` |
| `MANDATE_EXTRA_ALLOWED_SYMBOLS` | – | Comma-separated additions to the watchlist universe |
| `MANDATE_DENIED_SYMBOLS` | – | Comma-separated symbols always denied by the mandate |
| `ENABLE_PAPER_LIVE_BOUNDARY` | `True` | Verify configured broker account mode before orders |
| `ADVISORY_ENABLED` | `False` | Enable optional observational HTTP pre-trade reviews |
| `ADVISORY_TIMEOUT_MS` | `150.0` | Per-provider advisory timeout in milliseconds |
| `ADVISORY_1_ENDPOINT` | – | First advisory provider URL; required only when advisory is enabled |
| `ADVISORY_1_API_KEY` | – | Optional bearer token for the first advisory provider |
| `ADVISORY_1_TIMEOUT_MS` | `150` | HTTP request timeout for the first advisory provider |
| `TELEGRAM_BOT_TOKEN` | – | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | – | Chat that receives them |
| `DISCORD_WEBHOOK_URL` | – | Discord webhook for notifications |
| `ENABLE_SESSION_REPORT` | `True` | Send the end-of-session report |

---

## Project structure

<p align="center"><img src="docs/architecture.svg" alt="System architecture: market data flows through indicators, regime-aware strategy, and risk management into execution; the walk-forward optimizer feeds learned parameters back to the strategy; execution emits notifications; a safety boundary of four invariants guards the whole loop." width="920"></p>
<p align="center"><em>The pipeline and its feedback loop. Each layer has one owner; the optimizer writes learned parameters back into the strategy, and a boundary of four safety invariants guards the loop.</em></p>

```
trading-bot/
├── config.py                 # Config loading and validation
├── bot.py                    # CLI orchestrator and the live loop
├── pytest.ini                # Scopes test collection to tests/ only
├── strategy_state.json       # Learned parameters per ticker
├── reset_strategy_state.py   # Clears unvalidated parameters (keeps a backup)
├── data/
│   └── market_data.py        # OHLCV from Alpaca, yfinance fallback
├── indicators/
│   └── technicals.py         # EMA/RSI/ATR/BB/MACD/VWAP + regime classifier
├── strategy/
│   ├── adaptive_strategy.py  # Regime-aware signal generation
│   ├── scanner.py            # Conviction ranking across the watchlist
│   └── macro_filter.py       # Macro-trend confluence filter
├── risk/
│   ├── risk_manager.py       # Volatility sizing, circuit breaker, trailing stops
│   └── earnings_filter.py    # Earnings blackout / event-risk gate
├── safety/
│   ├── mandate_gate.py        # Structural pre-trade limits and symbol universe
│   ├── kill_switch.py         # Restart-persistent entry halt (no auto-flatten)
│   ├── audit_ledger.py        # Hash-chained, verifiable event log
│   ├── paper_live_boundary.py # Configured broker-mode and connector limit checks
│   ├── advisory.py            # Optional observational advisory interface
│   └── http_advisory.py       # External HTTP advisory provider
├── optimizer/
│   ├── backtester.py         # Sharpe, profit factor, drawdown, fitness
│   └── self_improver.py      # Walk-forward optimiser and adoption gates
├── execution/
│   ├── alpaca_broker.py      # Alpaca client + local paper simulator
│   ├── trade_journal.py      # Trade journal (SQLite)
│   └── webhook.py            # Telegram / Discord notifications
└── tests/                    # Safety, optimizer, risk, wiring, integration
```

Each layer has exactly one owner. A change to what the bot buys belongs in `strategy/` and must be mirrored in `optimizer/backtester.py`, so the backtest and the live bot never disagree.

---

## Testing

```bash
python -m pytest                          # whole suite
python -m pytest -v                       # verbose
python -m pytest tests/test_optimizer_integrity.py
python -m pytest tests/test_safety_integration.py
```

The suite is hermetic: no network, no orders. That is enforced, not assumed — `tests/test_safety.py` collects the suite in a subprocess and fails if importing anything executes the trading loop. The safety integration tests also cover mandate limits, kill-switch persistence, audit-chain verification, advisory behavior, and paper/live boundary checks.

---

## Notes and limitations

- **Sharpe is computed from the daily equity curve**, resampled to daily closes and clipped to ±5, not from trade returns. Short windows produce unstable trade-level Sharpe.
- **The paper simulator is a learning tool, not a backtest.** It models orders and positions; it does not model slippage, partial fills, or borrow costs. Treat its numbers as directional.
- **The calibration study covers seven liquid symbols over roughly two years.** The gates are calibrated for that regime and would need re-running before being trusted on a different universe or timeframe.
- **`strategy_state.json` is tracked in git** because it is the bot's learned memory. Runtime output files that embed your account number are ignored and guarded by a test.

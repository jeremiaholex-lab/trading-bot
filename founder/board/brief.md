# founder/board/brief.md

## Project: Autonomous Quant Trading Bot

### What it is
A self-improving algorithmic trading bot for US equities/ETFs that classifies market regimes, sizes risk, places bracket orders via Alpaca, and re-tunes its own parameters overnight via walk-forward optimization.

### Who it is for
Individual quant traders and small proprietary trading desks who want automated, regime-aware execution with institutional-grade risk controls and self-improving parameters.

### What it sells, at what price
Not yet commercialized. Target: SaaS subscription $200-500/month per account, or revenue share 10-20% of profits above benchmark.

### Where and how
Cloud-hosted (AWS/GCP) with web dashboard. Customers connect Alpaca paper/live accounts via OAuth. Distribution: direct B2B, quant community forums, Alpaca marketplace.

### Budget and constraints
Self-funded dev. Two Alpaca accounts (paper + live) for testing. No external capital. One developer. Regulatory: not a registered advisor; operates as software tool, not fund manager.

---

## Current State (as of 2026-11-05)

### Technical Stack
- **Language**: Python 3.12
- **Broker**: Alpaca (paper + live), yfinance fallback
- **Data**: Alpaca Market Data, yfinance fallback
- **Execution**: Alpaca Trading API (bracket orders, portfolio history)
- **Dashboard**: FastAPI + vanilla JS/SVG (no framework), WebSocket + polling
- **Scheduler**: APScheduler / custom event loop
- **Optimizer**: Walk-forward (70/30 split), seeded, persisted per ticker
- **Tests**: 237 pytest tests, hermetic (no network in CI), CI-ready

### Core Architecture
- **Regime classification**: EMA/MACD/ATR/Bollinger → TRENDING_BULL, TRENDING_BEAR, RANGING, HIGH_VOLATILITY
- **Signal generation**: Regime-aware pullback/breakout/mean-reversion
- **Risk management**: Volatility sizing (ATR), circuit breaker (3% daily), sector caps, earnings/news blackout, trailing stops
- **Self-improvement**: Walk-forward optimizer (70/30 split), three adoption gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3)
- **Safety infrastructure** (Vibe-Trading inspired):
  - Mandate gate: symbol universe, notional caps, leverage, daily caps, kill switch
  - Kill switch: filesystem sentinel, survives restart, flatten latch
  - Audit ledger: SHA-256 chained, tamper-evident
  - Paper/live boundary: server-reported mode, fail-closed
  - Pre-trade advisory: optional, fail-open, broker-agnostic
- Simulator: $100k virtual, bracket orders, slippage/latency modeling, stop/target/partial-close
- Dashboard: Primary/Secondary accounts on separate ports, WebSocket + polling, profile isolation

### Safety Invariants (never relaxed)
1. Fatal auth: broker rejects bad keys, never silently falls back
2. Hermetic tests: no network in tests, no live orders in CI
3. Absolute optimizer gates: fitness floor, improvement margin, minimum trades
4. No account data in git: credentials in .env only, state files ignored

### Current Safety Infrastructure (recent additions)
- Mandate gate: symbol universe, notional caps, leverage, daily caps, kill switch
- Kill switch: filesystem sentinel, survives restart, flatten latch prevents double-flatten
- Audit ledger: SHA-256 chained, tamper-evident, append-only
- Paper/live boundary: server-reported mode verification, fail-closed
- Advisory layer: opt-in HTTP, fail-open, observational only
- Paper/live boundary: server-reported mode verification, fail-closed
- Audit ledger: SHA-256 chained, tamper-evident
- Simulator: $100k virtual, bracket orders, slippage/latency modeling, stop/target/partial-close

### Recent Additions (2026-11-05)
1. **Data quality gates** (gap/outlier/stale-quote/OHLC/volume anomaly detection, hash-chained audit ledger)
2. **Paper trading realism** (slippage model: 5bps * notional/equity; latency: 100ms configurable)
3. **Portfolio history dashboard** (Alpaca portfolio-history API, range/resolution controls, Equity/P&L% toggle)

### Order Execution Variety (not yet started)
- Planned: limit, stop-limit, TWAP, VWAP, iceberg order types
- Currently only: market bracket orders (Alpaca BRACKET class)

### Multi-Account
- Primary + Secondary profiles, separate .env files, separate state directories
- Separate dashboard servers on ports 8765/8766
- run_accounts.py launcher with lock file

### Tests
- 237 tests passing (hermetic, no network in CI)
- Test categories: config, strategy, optimizer, risk, broker, dashboard, safety, optimizer gates, optimizer integrity
- CI-ready: pytest, no network in test suite

### What's Not Done Yet
- Order execution variety (limit/stop-limit/TWAP/VWAP/iceberg)
- External alerting (Slack/email/PagerDuty)
- CI/CD pipeline (GitHub Actions)
- Secrets management (1Password/Doppler)
- Model persistence/versioning (strategy_state.json schema versioning)
- External alerting (Slack/email/PagerDuty)
- Model persistence/versioning
- CI/CD pipeline
- External alerting
- Alternative data feeds
- Web-based strategy builder
- Multi-asset class (crypto, futures, options)
# Product lens
Score: 7 | Vote: FUND IF
## What is strong
- **The one thing is clear**: An autonomous regime-aware trading bot that self-improves overnight. The pitch fits in one sentence.
- **Taste and craft in the invisible parts**: The safety infrastructure is exceptional — mandate gate, kill switch with flatten latch, SHA-256 chained audit ledger, paper/live boundary verified server-side with fail-closed behavior. These are details no customer sees until they prevent catastrophe.
- **Hermetic discipline**: 237 tests, zero network calls in CI, no live orders in tests. This is craft where nobody looks.
- **Regime awareness, not single-strategy**: Classifies four market regimes and switches signal logic accordingly. This is the right abstraction for the problem.
- **Realistic simulation**: 5bps slippage model scaled by notional/equity, 100ms configurable latency, bracket orders with stop/target/partial-close. The paper experience respects the live reality.
- **Recent velocity**: Data quality gates, paper trading realism, portfolio history dashboard all shipped recently. The team ships.

## What would kill it (the top 3, most dangerous first)
1. **The paper-to-live seam is a single point of existential failure**. The boundary is "server-reported mode, fail-closed" — but the moment a user flips from paper to live, one misclassified mode, one stale config, one race condition in the sentinel file, and real money trades on paper parameters (or vice versa). The dashboard runs two separate servers on ports 8765/8766 for primary/secondary accounts. That is two seams. The experience hands off between processes, ports, and .env files. Seams are where it breaks.

2. **The "self-improving" claim creates a trust cliff**. Walk-forward optimizer with three gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3) is disciplined. But regime changes invalidate history overnight. If the optimizer adopts a parameter set that worked in TRENDING_BULL and the market switches to HIGH_VOLATILITY, the bot scales into a drawdown with "improved" parameters. The customer cannot verify the optimizer isn't overfitting — they only see P&L. When it stops working, they churn and tell everyone it's curve-fit.

3. **The dashboard fragments the experience across ports and processes**. Primary on 8765, Secondary on 8766, separate .env files, separate state directories, launcher with lock file. The user who wants to monitor both accounts sees two browser tabs, two WebSocket connections, two polling cycles. The "whole experience" — from OAuth connect to daily P&L review — is split. A unified view with account selector is table stakes for a $200-500/month SaaS.

## The questions I would ask the founder
1. Walk me through the first five minutes: a new user lands, connects Alpaca via OAuth, and then what? Show me the exact screens, the copy, the moment they feel "this works."
2. When the optimizer adopts new parameters overnight, what does the user see in the morning? Is there a changelog, a diff, a "why"? Or just a new equity curve?
3. The paper/live boundary: what exactly happens in the 30 seconds after I toggle "Go Live"? What confirms mode on the dashboard? What prevents a stale sentinel file from a crashed process?
4. Why two dashboard servers instead of one server with account isolation? What does the user lose by merging them?
5. In one sentence, what is this product? (If it takes a paragraph, it's not clear yet.)

## If FUND IF: the conditions
1. **Unify the dashboard** into a single server/port with account selector, shared WebSocket, single OAuth flow for multiple accounts. Ship before any external beta.
2. **Make the optimizer transparent**: every parameter change surfaces a diff (old → new), the regime context, the walk-forward window, and the three gate values. Add a "revert" button that rolls back to the previous parameter set in one click.
3. **Hardened paper→live transition**: a mandatory pre-flight checklist on the dashboard (mode confirmed by broker API, sentinel file fresh, config hash matches, kill switch armed) with a "Go Live" button that is the only way to transition — no manual .env edits, no process restarts.
4. **Kill the dual-port launcher**. One process, one port, account-scoped state. The lock file and run_accounts.py are implementation details leaking into UX.
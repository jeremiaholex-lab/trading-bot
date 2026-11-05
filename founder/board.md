# founder/board.md

## Vote
**2 FUND IF, 1 PASS** | Average score: 6.3

| Lens | Score | Vote |
|------|-------|------|
| Offers | 6 | FUND IF |
| Monopoly | 6 | FUND IF |
| Product | 7 | FUND IF |

---

## Risks (raised by ≥2 members)

### 1. No live track record / proof — kills perceived likelihood
- **Offers**: "Zero live P&L history, no backtest results published, no Sharpe/sortino numbers, no max drawdown data."
- **Product**: "The 'self-improving' claim creates a trust cliff... When it stops working, they churn and tell everyone it's curve-fit."
- **Monopoly**: "Alpha decay + self-improvement ceiling... If the optimizer hits a regime where *no* parameter set clears the gates, the bot stops trading."

### 2. Distribution vacuum / no channel
- **Offers**: "Distribution is a ghost... No content engine, no email capture, no demo video, no case study, no affiliate program, no partnership with Alpaca beyond marketplace listing."
- **Monopoly**: "Single-founder bus factor + distribution vacuum... CAC is unknown and likely high... Alpaca marketplace is a channel, not a moat."
- **Product**: "The dashboard fragments the experience across ports and processes... two browser tabs, two WebSocket connections, two polling cycles."

### 3. Regulatory risk on profit-share model
- **Monopoly**: "If the SEC decides that 'self-improving parameters + automated execution + profit-share pricing' = investment adviser, the entire model collapses... The profit-share tier (10-20% of profits above benchmark) is the specific trigger."
- **Offers**: "Revenue share model creates misaligned incentives... the customer bears all downside risk."

### 4. Paper-to-live seam / optimizer trust cliff
- **Product**: "The paper-to-live seam is a single point of existential failure... one misclassified mode, one stale config, one race condition in the sentinel file, and real money trades on paper parameters."
- **Offers**: "What happens when the optimizer adopts a parameter set that blows up in live? The gates don't prevent regime-shift failure."

### 5. Single-founder bus factor
- **Monopoly**: "One developer, no CI/CD pipeline, no secrets management, no alerting... If the founder gets hit by a bus, the product dies."
- **Product**: "The dashboard fragments the experience across ports and processes... two browser tabs, two WebSocket connections, two polling cycles."

---

## Conditions (merged, as a checklist)

### Regulatory & Legal
- [ ] **Regulatory clearance**: Written counsel opinion (or no-action letter) confirming the profit-share model does not require IA registration, *before* any customer onboards on that tier. Fallback: pure SaaS pricing validated at $299/month with 20 paying customers. *(Monopoly, Offers)*

### Optimizer Validation
- [ ] **Optimizer stress test**: Publish a 24-month walk-forward backtest (70/30 split, monthly re-optimization) showing the *adaptive* strategy beats the *static* best-fixed-parameters baseline by ≥25% net of slippage/commissions across the full universe, with zero gate-relaxation events. If the adaptive version underperforms static, the self-improvement claim is false. *(Monopoly, Offers)*
- [ ] **Optimizer circuit breaker**: If the adopted parameter set produces >2x the historical max drawdown in paper within 5 trading days, auto-revert and alert. Document this in the dashboard. *(Offers)*

### Distribution Proof
- [ ] **Distribution proof**: 50 paying customers (any tier) acquired *without* founder personal network, with CAC ≤ 3 months' revenue, within 6 months of funding. This proves the channel scales. *(Monopoly, Offers)*
- [ ] **Build the distribution machine before launch:**
   - 10-minute demo video showing the full paper→live flow
   - Weekly "regime report" newsletter (free) capturing the 4 regimes on SPY/QQQ—builds the email list
   - Alpaca marketplace listing with live simulator stats embedded
   - 5 design partners (named) committed to 3-month paper trials at $0, converting to paid if metrics hit *(Offers)*

### Business Model
- [ ] **Replace revenue share with a pure SaaS tier** ($299/month) and a performance-fee *add-on* (10% above benchmark, high-water mark, quarterly settlement). The base price covers infrastructure; the performance fee aligns upside without asymmetric downside. *(Offers, Monopoly)*

### Guarantee
- [ ] **Define the guarantee in writing:** "90-day paper-trading money-back guarantee. If the bot doesn't outperform SPY total return on paper over 90 consecutive days, full refund." This moves the risk to the founder where it belongs. *(Offers)*

### Product & UX
- [ ] **Unify the dashboard** into a single server/port with account selector, shared WebSocket, single OAuth flow for multiple accounts. Ship before any external beta. *(Product)*
- [ ] **Make the optimizer transparent**: every parameter change surfaces a diff (old → new), the regime context, the walk-forward window, and the three gate values. Add a "revert" button that rolls back to the previous parameter set in one click. *(Product)*
- [ ] **Hardened paper→live transition**: a mandatory pre-flight checklist on the dashboard (mode confirmed by broker API, sentinel file fresh, config hash matches, kill switch armed) with a "Go Live" button that is the only way to transition — no manual .env edits, no process restarts. *(Product, Monopoly)*
- [ ] **Kill the dual-port launcher**: One process, one port, account-scoped state. The lock file and run_accounts.py are implementation details leaking into UX. *(Product)*
- [ ] **Make the optimizer transparent**: every parameter change surfaces a diff (old → new), the regime context, the walk-forward window, and the three gate values. Add a "revert" button that rolls back to the previous parameter set in one click. *(Product)*

### Operational Resilience
- [ ] **Bus factor mitigation**: CI/CD pipeline, secrets management (1Password/Doppler), external alerting, and model persistence/versioning shipped within 90 days. A second engineer hired (or contracted with equity) within 120 days. *(Monopoly)*
- [ ] **Kill-switch transparency**: Public documentation of the exact gate thresholds, fallback behavior, and historical gate-failure rate so customers can model "time in cash" risk. This turns a potential churn driver into a trust builder. *(Monopoly)*

### Proof & Trust
- [ ] **Publish a 6-month simulator equity curve** (with the slippage/latency model enabled) before any customer onboarding. Sharpe, max drawdown, win rate, profit factor—all of it. This becomes the "proof" that lifts perceived likelihood. *(Offers)*
- [ ] **Optimizer stress test**: Publish a 24-month walk-forward backtest (70/30 split, monthly re-optimization) showing the *adaptive* strategy beats the *static* best-fixed-parameters baseline by ≥25% net of slippage/commissions across the full universe, with zero gate-relaxation events. If the adaptive version underperforms static, the self-improvement claim is false. *(Monopoly)*

---

## The Strongest Version of This Business

**Autonomous Quant Trading Bot** — a self-improving, regime-aware execution engine for individual quants and small prop desks that runs on Alpaca, self-tunes overnight via walk-forward optimization, and wraps it all in institutional-grade safety infrastructure (mandate gate, kill switch, audit ledger, paper/live boundary) that makes "not blowing up" the product's core value prop.

**What makes this version stronger than the pitch:**
- The product is not "a bot that makes money" — it's **infrastructure that prevents the catastrophic errors that erase months of alpha**. That is a defensible, differentiable position in a market where blowing up is the norm.
- The safety infrastructure (mandate gate, kill switch with flatten latch, SHA-256 chained audit ledger, paper/live boundary with fail-closed semantics, hermetic tests) is not marketing fluff — it's engineered, tested, and running. The 237 hermetic tests are the receipts.
- The walk-forward optimizer with three hard adoption gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3) is a real technical moat: most retail bots optimize for returns; this optimizes for *survival*.
- The simulator with slippage/latency modeling and bracket orders makes paper trading honest. The portfolio history dashboard (Alpaca portfolio-history API) gives users the same equity curve view they'd get in a live broker.
- The team ships: data quality gates, paper trading realism, portfolio history dashboard all delivered recently. Velocity is real.

**What changes from the pitch:**
- Revenue share is an *add-on*, not the core model. Core is SaaS ($299/mo). This fixes the regulatory risk and misaligned incentives.
- The product is **one dashboard, one port, one account selector** — not two ports, two .env files, two processes. The dual-account architecture is an implementation detail, not UX.
- The optimizer is **transparent and revertible**. Every parameter change shows a diff, regime context, and gate values. One-click revert is a feature, not a nice-to-have.
- The go-live transition is **a checklist, not a .env edit**. The "Go Live" button is the only way to transition; it runs a pre-flight checklist (mode confirmed by broker API, sentinel file fresh, config hash matches, kill switch armed).

**The bet:** Safety-first quant execution is an underserved wedge. Retail quants and small props have capital and skill but lack institutional infrastructure. This product sells them the infrastructure they'd build themselves if they had two engineers and six months — for $299/month instead. If the optimizer stress test validates the adaptive edge, the moat deepens from "safety" to "safety + alpha." If not, the safety product still stands alone at a price the market pays.

---

## Next Steps

1. **Immediate**: Open the two PRs on the safety integration branch; merge when CI passes.
2. **Week 1-2**: Ship the unified dashboard (Task 3 conditions) and optimizer transparency features.
3. **Week 3-4**: Publish the 6-month simulator equity curve; launch the optimizer stress test backtest.
4. **Month 2**: Recruit 5 design partners for 3-month paper trials; build the distribution machine (newsletter, demo video, marketplace listing).
5. **Month 3**: Regulatory counsel opinion on profit-share model; decide on pure SaaS vs. add-on pricing.
6. **Month 4**: Open beta with 20 paying customers at $299/mo; hit CAC ≤ 3 months revenue.
7. **Month 6**: 50 paying customers, CAC ≤ 3 months revenue, optimizer stress test published, bus factor mitigated.

**Board recommendation**: FUND IF all conditions are met. The core product is real, the safety architecture is exceptional, and the market wedge is clear. The conditions are specific, measurable, and de-risk the three fatal risks (proof, distribution, regulation). If the founder executes the checklist, this becomes a fundable SaaS with a genuine technical moat.
# Monopoly lens
Score: 6 | Vote: FUND IF

## What is strong

**Proprietary technology that is genuinely 10x on safety, not strategy.** The walk-forward optimizer with absolute adoption gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3) combined with institutional-grade safety infrastructure—kill switch with flatten latch, SHA-256 chained audit ledger, server-reported paper/live boundary with fail-closed semantics—is a real technical moat. Most retail bots optimize for returns; this optimizes for *not blowing up*. That is a defensible differentiator in a market where "blowing up" is the norm.

**The secret is safety-as-product.** The founder knows that quant traders don't need better signals (signals are commodities); they need infrastructure that prevents the catastrophic errors that erase months of alpha. The 237 hermetic tests, zero-network CI, and mandate gate (symbol universe, notional caps, leverage limits, daily caps) prove this isn't marketing—it's engineered.

**Timing is right.** Alpaca's API has matured to support bracket orders and portfolio history. Zero-commission trading normalized retail algorithmic execution. The "vibe trading" safety pattern (filesystem sentinel kill switch, audit ledger) is a known-good pattern from the prop world, now portable to retail via Alpaca.

**Small beachhead market exists.** "Individual quant traders and small prop desks who want automated, regime-aware execution with self-improving parameters" is a narrow, well-defined segment. The product speaks their language (regime classification, walk-forward, ATR sizing, bracket orders). First 100 customers are reachable via Alpaca marketplace, QuantConnect forums, and direct outreach to prop desks using Alpaca.

## What would kill it (the top 3, most dangerous first)

1. **Alpha decay + self-improvement ceiling.** The walk-forward optimizer re-tunes parameters overnight on historical data. But markets are non-stationary adversarial environments. If the optimizer hits a regime where *no* parameter set clears the gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3), the bot stops trading. That's a feature, not a bug—but if it happens systematically across tickers, the product becomes "software that mostly sits in cash." Competitors without gates keep trading (and blowing up); this one stops. Customers may churn from "it doesn't trade enough."

2. **Regulatory reclassification.** The business operates as a software tool, not a registered advisor. If the SEC or state regulators decide that "self-improving parameters + automated execution + profit-share pricing" = investment adviser, the entire model collapses. The profit-share tier (10-20% of profits above benchmark) is the specific trigger. One enforcement action against a similar product kills distribution (Alpaca marketplace delisting) and customer trust simultaneously.

3. **Single-founder bus factor + distribution vacuum.** One developer, no CI/CD pipeline, no secrets management, no alerting. The technical debt on *operations* (not the core bot) is high. If the founder gets hit by a bus, the product dies. More immediately: there is no distribution moat. Alpaca marketplace is a channel, not a moat—Alpaca can promote a competitor tomorrow. Quant forums are fragmented and low-trust. CAC is unknown and likely high for a $200-500/month SaaS with no brand.

## The questions I would ask the founder

1. **What happens when the optimizer finds zero valid parameter sets for 30 consecutive days across the entire universe?** Do you ship "no trade" as a feature, or do you relax gates? Show me the backtest where gates *failed* and what the fallback behavior was.

2. **How do you defend the profit-share model against regulatory risk?** Have you received a no-action letter or counsel opinion? If not, what is the Plan B pricing model that keeps unit economics viable?

3. **What is your distribution flywheel?** Alpaca marketplace gets you the first 10. How do you get the next 100 without paying $2,000 CAC each? Where is the network effect or brand compounding?

4. **Where is the 10x *product* moat, not just the 10x *safety* moat?** Safety is table stakes for institutional money. For retail quant traders, what makes this *irreplaceable* versus a well-configured QuantConnect algorithm + Alpaca execution? The self-improvement is the claim—show me the out-of-sample walk-forward curve where the *re-tuned* parameters beat the *static* parameters by >25% net of costs over 12 months.

5. **What stops a copycat in month six?** The optimizer gates and safety infra are code—they can be cloned. The audit ledger is a pattern. The regime classifier is standard TA. If a competitor raises $2M and hires three engineers, what is the structural advantage that survives?

## If FUND IF: the conditions

1. **Regulatory clearance**: Written counsel opinion (or no-action letter) confirming the profit-share model does not require IA registration, *before* any customer onboards on that tier. Fallback: pure SaaS pricing validated at $299/month with 20 paying customers.

2. **Optimizer stress test**: Publish a 24-month walk-forward backtest (70/30 split, monthly re-optimization) showing the *adaptive* strategy beats the *static* best-fixed-parameters baseline by ≥25% net of slippage/commissions across the full universe, with zero gate-relaxation events. If the adaptive version underperforms static, the self-improvement claim is false.

3. **Distribution proof**: 50 paying customers (any tier) acquired *without* founder personal network, with CAC ≤ 3 months' revenue, within 6 months of funding. This proves the channel scales.

4. **Bus factor mitigation**: CI/CD pipeline, secrets management (1Password/Doppler), external alerting, and model persistence/versioning shipped within 90 days. A second engineer hired (or contracted with equity) within 120 days.

5. **Kill-switch transparency**: Public documentation of the exact gate thresholds, fallback behavior, and historical gate-failure rate so customers can model "time in cash" risk. This turns a potential churn driver into a trust builder.
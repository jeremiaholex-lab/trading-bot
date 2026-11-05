# Offers lens
Score: 6/10 | Vote: FUND IF

## What is strong

**The value equation has real teeth.** The dream outcome (automated, regime-aware execution that self-improves overnight) is exactly what a quant trader wants. The perceived likelihood is boosted by concrete evidence: 237 hermetic tests, four safety invariants that are architectural (not aspirational), a paper trading simulator with slippage/latency modeling, and a walk-forward optimizer with three hard adoption gates. The time-to-first-result is overnight (optimizer runs while you sleep). The effort asked of the buyer is low: connect an Alpaca account via OAuth and pick paper or live.

**The stack is unusually well-built for pre-revenue.** Most founders at this stage have a strategy and a hope. This has: regime classification (4 regimes), signal generation (pullback/breakout/mean-reversion per regime), volatility sizing, circuit breakers, sector caps, earnings/news blackout, trailing stops, a kill switch that survives restart, SHA-256 chained audit ledger, paper/live boundary verification, and a portfolio history dashboard. Each of these is a problem the buyer hits on the way to the outcome, and each is already solved in code.

**The market scores well on three of four.** Pain is massive (manual execution is error-prone and regime-blind). Buyers have money (prop desks and serious quants allocate $2k-6k/year for tools). The market is growing (algo trading adoption rising, Alpaca marketplace expanding). The weakness: buyers are *not* easy to find and reach—they're fragmented across Discord, Twitter, niche forums, and the Alpaca marketplace. No obvious channel dominates.

**Not competing on price.** At $200-500/month or 10-20% profit share, this is positioned as premium. The stack justifies it—if the guarantee lands.

## What would kill it (the top 3, most dangerous first)

1. **No guarantee, no proof, no track record.** The value equation's "perceived likelihood" column is the weakest link. There is zero live P&L history, no backtest results published, no Sharpe/sortino numbers, no max drawdown data. A quant trader's first question: "Show me the equity curve." The answer today is "run it on paper for a month." That asks the buyer to do the work of validation—exactly the effort Hormozi says kills offers.

2. **Distribution is a ghost.** "Quant community forums, Alpaca marketplace, direct B2B" is a wish list, not a channel. There's no content engine, no email capture, no demo video, no case study, no affiliate program, no partnership with Alpaca beyond marketplace listing. The first 100 customers have no defined acquisition path. At $200-500/month, CAC must stay under ~$500 to make unit economics work. No plan shown for how.

3. **Revenue share model creates misaligned incentives.** The 10-20% profit share sounds great until a drawdown hits. If the bot loses money, the founder earns $0 while the customer loses capital. If the bot makes money, the founder takes a cut. This asymmetry makes the customer bear all downside risk. Smart quants will spot this and either negotiate it away or walk. It also complicates the "guarantee" conversation—what are you guaranteeing when your revenue depends on their P&L?

## The questions I would ask the founder

1. **What is the live equity curve on the $100k simulator over the last 6 months?** Not backtest—simulator with slippage/latency. If you don't have this, why not?

2. **What exactly does the customer see in their first 5 minutes?** Walk me through: OAuth connect → paper mode → first signal → first bracket order → dashboard view. Where is the friction?

3. **How do you get the first 20 paying customers by name?** Not "channels"—names. Who are they, how do you reach them, what do you say?

4. **What happens when the optimizer adopts a parameter set that blows up in live?** The gates (fitness ≥ 0, margin ≥ 0.25, trades ≥ 3) prevent *obviously* bad params, but they don't prevent regime-shift failure. What's the circuit breaker on the optimizer itself?

5. **Why revenue share instead of pure SaaS?** At $500/month, a $50k account paying 20% of profits needs only $2,500/month profit to break even on the SaaS price. What's the customer's breakeven on the rev-share, and why is it better for them?

6. **What is the guarantee?** "Try paper free for 30 days" is not a guarantee. "If you don't beat SPY on paper in 90 days, full refund" is. Which one are you willing to put in writing?

## If FUND IF: the conditions

1. **Publish a 6-month simulator equity curve** (with the slippage/latency model enabled) before any customer onboarding. Sharpe, max drawdown, win rate, profit factor—all of it. This becomes the "proof" that lifts perceived likelihood.

2. **Replace revenue share with a pure SaaS tier** ($299/month) and a performance-fee *add-on* (10% above benchmark, high-water mark, quarterly settlement). The base price covers infrastructure; the performance fee aligns upside without asymmetric downside.

3. **Define the guarantee in writing:** "90-day paper-trading money-back guarantee. If the bot doesn't outperform SPY total return on paper over 90 consecutive days, full refund." This moves the risk to the founder where it belongs.

4. **Build the distribution machine before launch:**
   - 10-minute demo video showing the full paper→live flow
   - Weekly "regime report" newsletter (free) capturing the 4 regimes on SPY/QQQ—builds the email list
   - Alpaca marketplace listing with live simulator stats embedded
   - 5 design partners (named) committed to 3-month paper trials at $0, converting to paid if metrics hit

5. **Add an optimizer circuit breaker:** If the adopted parameter set produces >2x the historical max drawdown in paper within 5 trading days, auto-revert and alert. Document this in the dashboard.

6. **Instrument the first-user funnel:** Event tracking from OAuth click → paper trade placed → live toggle flipped → first month renewal. No analytics = no iteration.
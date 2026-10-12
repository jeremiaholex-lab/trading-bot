import threading
import time
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from config import Config
from bot import TradingBotOrchestrator

app = FastAPI(title="AI Trading Bot Dashboard")
orchestrator = None
bot_thread = None

def run_trading_bot_worker():
    """Background worker loop running the trading orchestrator continuously."""
    global orchestrator
    orchestrator = TradingBotOrchestrator()
    orchestrator.start_live_loop(interval_seconds=60, auto_calibrate=True)

@app.on_event("startup")
def startup_event():
    """Launch the trading bot in a background thread when the server starts."""
    global bot_thread
    bot_thread = threading.Thread(target=run_trading_bot_worker, daemon=True)
    bot_thread.start()

@app.get("/", response_class=HTMLResponse)
def index():
    """Live web dashboard displaying account equity, open positions, and status."""
    acc = {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "mode": "INITIALIZING..."}
    open_pos = {}
    if orchestrator and orchestrator.broker:
        acc = orchestrator.broker.get_account_summary()
        open_pos = orchestrator.broker.get_open_positions()

    positions_html = ""
    if open_pos:
        for sym, p in open_pos.items():
            pl = p.get('unrealized_pl', 0.0)
            plpc = p.get('unrealized_plpc', 0.0)
            color = "#10B981" if pl >= 0 else "#EF4444"
            positions_html += f"""
            <tr style="border-bottom: 1px solid #374151;">
                <td style="padding: 12px; font-weight: bold; color: #60A5FA;">{sym}</td>
                <td style="padding: 12px; text-align: right;">{p.get('qty')}</td>
                <td style="padding: 12px; text-align: right;">${p.get('entry_price', 0.0):.2f}</td>
                <td style="padding: 12px; text-align: right;">${p.get('current_price', 0.0):.2f}</td>
                <td style="padding: 12px; text-align: right; color: {color}; font-weight: bold;">${pl:+,.2f} ({plpc:+.2f}%)</td>
            </tr>
            """
    else:
        positions_html = """
        <tr>
            <td colspan="5" style="padding: 24px; text-align: center; color: #9CA3AF;">
                No active open positions. Bot is scanning the market for high-probability setups.
            </td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI Trading Bot - Live Dashboard</title>
        <meta http-equiv="refresh" content="30">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: #111827;
                color: #F9FAFB;
                margin: 0;
                padding: 24px;
            }}
            .container {{
                max-width: 1000px;
                margin: 0 auto;
            }}
            .card {{
                background-color: #1F2937;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 24px;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
                border: 1px solid #374151;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 16px;
            }}
            .stat-label {{
                font-size: 13px;
                color: #9CA3AF;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            .stat-val {{
                font-size: 24px;
                font-weight: 700;
                margin-top: 4px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 12px;
            }}
            th {{
                background-color: #374151;
                padding: 12px;
                text-align: left;
                font-size: 13px;
                color: #D1D5DB;
            }}
            .badge {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 9999px;
                font-size: 12px;
                font-weight: 600;
                background-color: #065F46;
                color: #6EE7B7;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px;">
                <div>
                    <h1 style="margin: 0; font-size: 28px;">⚡ Autonomous AI Trading Bot</h1>
                    <p style="margin: 4px 0 0; color: #9CA3AF; font-size: 14px;">24/7 Cloud Engine on Hugging Face Spaces</p>
                </div>
                <div>
                    <span class="badge">● LIVE AUTOPILOT</span>
                </div>
            </div>

            <div class="card">
                <div class="grid">
                    <div>
                        <div class="stat-label">Execution Mode</div>
                        <div class="stat-val" style="color: #60A5FA; font-size: 18px;">{acc.get('mode', 'SIMULATION')}</div>
                    </div>
                    <div>
                        <div class="stat-label">Total Portfolio Equity</div>
                        <div class="stat-val" style="color: #10B981;">${acc.get('equity', 0.0):,.2f}</div>
                    </div>
                    <div>
                        <div class="stat-label">Available Cash</div>
                        <div class="stat-val">${acc.get('cash', 0.0):,.2f}</div>
                    </div>
                    <div>
                        <div class="stat-label">Watchlist Tickers</div>
                        <div class="stat-val" style="font-size: 16px; color: #F59E0B;">{', '.join(Config.WATCHLIST)}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2 style="margin: 0 0 16px; font-size: 20px;">Active Positions</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th style="text-align: right;">Qty</th>
                            <th style="text-align: right;">Entry Price</th>
                            <th style="text-align: right;">Current Price</th>
                            <th style="text-align: right;">Unrealized P&L</th>
                        </tr>
                    </thead>
                    <tbody>
                        {positions_html}
                    </tbody>
                </table>
            </div>

            <div class="card" style="font-size: 13px; color: #9CA3AF;">
                <p style="margin: 0;">
                    🛡️ <b>Risk Protection Active:</b> 1.5% max risk per trade • Server-side bracket stops • Daily 3% circuit breaker
                </p>
                <p style="margin: 4px 0 0;">
                    Auto-refreshes every 30 seconds. Last updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}
                </p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860)

import os
import logging
import html
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger("WebhookNotifier")

class WebhookNotifier:
    """
    Sends real-time telemetry and alerts to Discord and/or Telegram webhooks.
    Supports trade execution notifications, risk circuit breaker alerts, and daily PnL summaries.
    """

    def __init__(self):
        self.discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        
        # Enable flags
        self.enable_discord = bool(self.discord_url and self.discord_url.startswith("http"))
        self.enable_telegram = bool(self.telegram_token and self.telegram_chat_id)

    def send_alert(self, title: str, message: str, level: str = "INFO", fields: Optional[Dict[Any, Any]] = None):
        """
        Sends an alert message to all configured webhooks (Discord & Telegram).
        Level can be 'INFO', 'SUCCESS', 'WARNING', 'CRITICAL'.
        """
        if not self.enable_discord and not self.enable_telegram:
            logger.debug(f"[Webhook Skipped] {title}: {message} (No webhooks configured)")
            return

        # Format emojis based on level
        emoji_map = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "CRITICAL": "🚨"
        }
        prefix = emoji_map.get(level.upper(), "📢")
        formatted_title = f"{prefix} [{level.upper()}] {title}"

        if self.enable_discord:
            self._send_discord(formatted_title, message, level, fields)

        if self.enable_telegram:
            self._send_telegram(formatted_title, message, fields)

    def _send_discord(self, title: str, message: str, level: str, fields: Optional[Dict[Any, Any]] = None):
        try:
            color_map = {
                "INFO": 3447003,      # Blue
                "SUCCESS": 3066993,   # Green
                "WARNING": 16776960,  # Yellow
                "CRITICAL": 15158332  # Red
            }
            embed = {
                "title": title,
                "description": message,
                "color": color_map.get(level.upper(), 3447003),
            }
            if fields:
                embed["fields"] = [{"name": str(k), "value": str(v), "inline": True} for k, v in fields.items()]

            payload = {"embeds": [embed]}
            response = requests.post(self.discord_url, json=payload, timeout=5)
            if response.status_code not in (200, 204):
                logger.warning(f"Discord webhook failed with status {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"Error sending Discord webhook: {e}")

    def _send_telegram(self, title: str, message: str, fields: Optional[Dict[Any, Any]] = None):
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            # The message body carries its own intentional HTML (bold labels,
            # italics), so it is NOT escaped here. Field values are plain
            # data and are escaped.
            text_lines = [f"<b>{html.escape(title)}</b>", "", message]
            if fields:
                text_lines.append("")
                for k, v in fields.items():
                    text_lines.append(f"<b>{html.escape(str(k))}:</b> {html.escape(str(v))}")
            
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": "\n".join(text_lines),
                "parse_mode": "HTML"
            }
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code != 200:
                logger.warning(f"Telegram webhook failed with status {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"Error sending Telegram webhook: {e}")

    def notify_order_execution(self, symbol: str, side: str, qty: float, price: float, order_type: str = "MARKET"):
        title = f"Order Executed: {side.upper()} {symbol}"
        msg = f"Successfully submitted {order_type} order for {qty} shares of {symbol} at ~${price:.2f}."
        fields = {"Symbol": symbol, "Side": side, "Quantity": qty, "Est. Price": f"${price:.2f}"}
        self.send_alert(title, msg, level="SUCCESS", fields=fields)

    def notify_circuit_breaker(self, reason: str, drawdown: float):
        title = "CIRCUIT BREAKER TRIGGERED"
        msg = f"Trading halted due to risk threshold breach. Drawdown: {drawdown*100:.2f}%."
        fields = {"Reason": reason, "Drawdown": f"{drawdown*100:.2f}%"}
        self.send_alert(title, msg, level="CRITICAL", fields=fields)

    def notify_daily_summary(self, total_equity: float, daily_pnl: float, open_positions: int):
        title = "Daily Trading Summary"
        pnl_sign = "+" if daily_pnl >= 0 else ""
        msg = f"Portfolio update at end of day scan."
        fields = {
            "Total Equity": f"${total_equity:,.2f}",
            "Daily PnL": f"{pnl_sign}${daily_pnl:,.2f}",
            "Open Positions": open_positions
        }
        level = "SUCCESS" if daily_pnl >= 0 else "WARNING"
        self.send_alert(title, msg, level=level, fields=fields)

    def notify_session_report(self, report: Dict[str, Any]):
        """Sends the end-of-session report.

        A report the operator can act on, not just a P&L number. The body
        answers the questions you actually have at 4 PM: what did the day
        make me, what is still open, did anything break, and did the
        optimiser change the strategy under me.

        Telegram renders the body as HTML, so the body carries the detail
        (positions, trades, warnings) and the fields carry the headline
        numbers that sit next to the message.
        """
        equity = float(report.get("equity", 0.0))
        daily_pnl = float(report.get("daily_pnl", 0.0))
        daily_pnl_pct = float(report.get("daily_pnl_pct", 0.0))
        positions = report.get("positions") or []
        trades = report.get("trades_taken") or []
        warnings = report.get("warnings") or []
        mode = report.get("mode", "")

        pnl_sign = "+" if daily_pnl >= 0 else ""
        pct_sign = "+" if daily_pnl_pct >= 0 else ""

        lines = []
        if mode:
            lines.append(f"<b>Mode:</b> {mode}")

        if positions:
            lines.append("")
            lines.append("<b>Open positions</b>")
            for p in positions:
                pl = float(p.get("unrealized_pl", 0.0))
                plpc = float(p.get("unrealized_plpc", 0.0))
                pl_sign = "+" if pl >= 0 else ""
                pct = f"{plpc:+.2f}%"
                lines.append(
                    f"  • {html.escape(str(p.get('symbol')))} {p.get('qty', 0)} @ "
                    f"${float(p.get('current_price', 0.0)):,.2f} "
                    f"({pl_sign}${pl:,.2f}, {pct})"
                )
        else:
            lines.append("")
            lines.append("<i>No open positions.</i>")

        if trades:
            lines.append("")
            lines.append("<b>Trades this session</b>")
            for t in trades:
                side = html.escape(str(t.get("side", "?")))
                reason = html.escape(str(t.get("reason", "")))
                lines.append(
                    f"  • {side} {html.escape(str(t.get('symbol')))} — {t.get('qty', 0)} @ "
                    f"${float(t.get('price', 0.0)):,.2f} — <i>{reason}</i>"
                )
        else:
            lines.append("")
            lines.append("<i>No trades executed this session.</i>")

        if warnings:
            lines.append("")
            lines.append("<b>Worth knowing</b>")
            for w in warnings:
                lines.append(f"  • {html.escape(str(w))}")

        body = "\n".join(lines)

        fields = {
            "Equity": f"${equity:,.2f}",
            "Daily P&L": f"{pnl_sign}${daily_pnl:,.2f} ({pct_sign}{daily_pnl_pct:.2f}%)",
            "Open Positions": len(positions),
            "Trades": len(trades),
        }
        if warnings:
            fields["Warnings"] = len(warnings)

        level = "SUCCESS" if daily_pnl >= 0 and not warnings else "WARNING"
        self.send_alert(
            "Session Report",
            body,
            level=level,
            fields=fields,
        )


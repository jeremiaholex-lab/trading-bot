import os
import logging
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
            text_lines = [f"<b>{title}</b>", "", message]
            if fields:
                text_lines.append("")
                for k, v in fields.items():
                    text_lines.append(f"<b>{k}:</b> {v}")
            
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

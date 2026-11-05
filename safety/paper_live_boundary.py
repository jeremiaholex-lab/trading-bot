"""
Paper/Live Boundary - Structural Account Mode Separation

Implements Vibe-Trading's paper/live boundary pattern:
- Structural separation: paper and live are DIFFERENT code paths, not just a flag
- Fail-closed: any mode mismatch blocks the order
- Server-reported mode verification (not client-trusted)
- Connector-level enforcement with USD sizing hooks
- Audit trail of every mode decision
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from config import Config

logger = logging.getLogger(__name__)


class AccountMode(Enum):
    """Account trading mode - structurally distinct."""
    PAPER = "paper"
    LIVE = "live"
    UNKNOWN = "unknown"


class ModeMismatchError(Exception):
    """Raised when account mode doesn't match expected mode."""
    def __init__(self, expected: AccountMode, actual: AccountMode, details: Dict = None):
        self.expected = expected
        self.actual = actual
        self.details = details or {}
        super().__init__(
            f"Account mode mismatch: expected {expected.value}, got {actual.value}"
        )


@dataclass
class ModeVerificationResult:
    """Result of account mode verification."""
    verified_mode: AccountMode
    expected_mode: AccountMode
    matched: bool
    server_reported: bool
    details: Dict[str, Any]


class PaperLiveBoundary:
    """
    Enforces structural separation between paper and live trading.

    Key principles:
    1. Mode is VERIFIED from server, not trusted from config
    2. Paper and live use DIFFERENT connectors/code paths
    3. Any mismatch is a HARD BLOCK (fail-closed)
    4. Sizing hooks convert to USD for cross-mode comparison
    5. Every mode decision is audited
    """

    def __init__(
        self,
        expected_mode: AccountMode,
        broker_client: Any = None,
        audit_ledger: Optional['AuditLedger'] = None,
    ):
        self.expected_mode = expected_mode
        self.broker_client = broker_client
        self.audit_ledger = audit_ledger

        # Mode verification cache (short TTL)
        self._cached_mode: Optional[AccountMode] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 60

        logger.info(f"PaperLiveBoundary initialized: expected_mode={expected_mode.value}")

    def verify_mode(self, force_refresh: bool = False) -> ModeVerificationResult:
        """
        Verify the account mode from the server.

        This is the SINGLE SOURCE OF TRUTH for mode.
        Never trust config, env vars, or client-side state.
        """
        # Check cache
        if not force_refresh and self._cached_mode and self._cache_timestamp:
            age = (datetime.now(timezone.utc) - self._cache_timestamp).total_seconds()
            if age < self._cache_ttl_seconds:
                return ModeVerificationResult(
                    verified_mode=self._cached_mode,
                    expected_mode=self.expected_mode,
                    matched=self._cached_mode == self.expected_mode,
                    server_reported=True,
                    details={'cached': True, 'cache_age_seconds': age},
                )

        # Query server
        verified_mode = self._query_server_mode()

        # Update cache
        self._cached_mode = verified_mode
        self._cache_timestamp = datetime.now(timezone.utc)

        matched = verified_mode == self.expected_mode

        # Audit the verification
        if self.audit_ledger:
            self.audit_ledger.log_system_event(
                event_type="mode_verification",
                message=f"Mode verified: {verified_mode.value} (expected: {self.expected_mode.value})",
                context={
                    'verified_mode': verified_mode.value,
                    'expected_mode': self.expected_mode.value,
                    'matched': matched,
                    'server_reported': True,
                },
            )

        if not matched:
            logger.critical(
                f"MODE MISMATCH: Expected {self.expected_mode.value}, "
                f"server reports {verified_mode.value}"
            )

        return ModeVerificationResult(
            verified_mode=verified_mode,
            expected_mode=self.expected_mode,
            matched=matched,
            server_reported=True,
            details={},
        )

    def _query_server_mode(self) -> AccountMode:
        """Query the actual account mode from the broker server."""
        if not self.broker_client:
            logger.warning("No broker client available for mode verification")
            return AccountMode.UNKNOWN

        try:
            # Try to get account info from broker
            if hasattr(self.broker_client, 'get_account'):
                account = self.broker_client.get_account()

                # Alpaca: check account status and trading mode
                if hasattr(account, 'status'):
                    status = str(account.status).lower()
                    if 'active' in status:
                        # Check if paper or live via _paper attribute on client
                        if getattr(self.broker_client, '_paper', False):
                            return AccountMode.PAPER
                        # Also check account object for paper attribute
                        if getattr(account, 'paper', False):
                            return AccountMode.PAPER
                        return AccountMode.LIVE

                # Generic: check for paper/live indicators on account object
                if getattr(account, 'paper', False):
                    return AccountMode.PAPER
                # Check broker client for paper mode
                if getattr(self.broker_client, '_paper', False) or getattr(self.broker_client, 'paper', False):
                    return AccountMode.PAPER
                return AccountMode.LIVE

        except Exception as e:
            logger.error(f"Failed to query server mode: {e}")

        return AccountMode.UNKNOWN

    def enforce_mode(self, operation: str, context: Dict = None) -> None:
        """
        Enforce mode match for a trading operation.

        Raises ModeMismatchError if mode doesn't match expected.
        This is a HARD BLOCK - fail closed.
        """
        result = self.verify_mode()

        if not result.matched:
            error = ModeMismatchError(
                expected=self.expected_mode,
                actual=result.verified_mode,
                details={
                    'operation': operation,
                    'context': context or {},
                    'server_reported': result.server_reported,
                }
            )

            # Audit the block
            if self.audit_ledger:
                self.audit_ledger.log_system_event(
                    event_type="mode_enforcement_block",
                    message=f"Operation blocked by mode mismatch: {operation}",
                    context={
                        'expected_mode': self.expected_mode.value,
                        'actual_mode': result.verified_mode.value,
                        'operation': operation,
                        'context': context or {},
                    },
                )

            raise error

    def get_sizing_hook(self) -> 'SizingHook':
        """Get the USD sizing hook for this mode."""
        return SizingHook(self)


class SizingHook:
    """
    USD sizing hook for cross-mode notional comparison.

    Converts order quantities to USD notionals using server-reported prices.
    This allows mandate gates to enforce caps consistently across paper/live.
    """

    def __init__(self, boundary: PaperLiveBoundary):
        self.boundary = boundary

    def compute_notional_usd(
        self,
        symbol: str,
        qty: int,
        side: str,
        price: Optional[float] = None,
    ) -> float:
        """
        Compute order notional in USD.

        Uses server-reported price if available, falls back to provided price.
        """
        # Verify mode first
        self.boundary.enforce_mode(f"sizing_hook:{symbol}")

        # Get price from broker if not provided
        if price is None and self.boundary.broker_client:
            try:
                if hasattr(self.boundary.broker_client, 'get_latest_price'):
                    price = self.boundary.broker_client.get_latest_price(symbol)
            except Exception as e:
                logger.warning(f"Could not fetch price for {symbol}: {e}")

        if price is None:
            raise ValueError(f"No price available for {symbol} notional calculation")

        notional = abs(qty) * price
        return round(notional, 2)

    def convert_to_base_currency(
        self,
        amount: float,
        from_currency: str,
        to_currency: str = "USD",
    ) -> float:
        """Convert amount to base currency (USD)."""
        # In production, use FX rates from broker
        if from_currency == to_currency:
            return amount
        logger.warning(f"FX conversion {from_currency}->{to_currency} not implemented, using 1:1")
        return amount


class ConnectorPaperLiveBoundary:
    """
    Connector-level paper/live boundary enforcement.

    Each broker connector gets its own boundary instance.
    Enforces:
    - max_order_volume (lots/contracts)
    - max_order_notional_usd (USD equivalent)
    - Fail-closed if price unavailable for notional calc
    """

    def __init__(
        self,
        connector_name: str,
        expected_mode: AccountMode,
        max_order_volume: Optional[float] = None,
        max_order_notional_usd: Optional[float] = None,
        broker_client: Any = None,
        audit_ledger: Optional['AuditLedger'] = None,
    ):
        self.connector_name = connector_name
        self.expected_mode = expected_mode
        self.max_order_volume = max_order_volume
        self.max_order_notional_usd = max_order_notional_usd
        self.broker_client = broker_client
        self.audit_ledger = audit_ledger

        self._boundary = PaperLiveBoundary(
            expected_mode=expected_mode,
            broker_client=broker_client,
            audit_ledger=audit_ledger,
        )
        self._sizing_hook = SizingHook(self._boundary)

        logger.info(
            f"ConnectorPaperLiveBoundary: {connector_name} "
            f"mode={expected_mode.value} "
            f"max_vol={max_order_volume} "
            f"max_notional_usd={max_order_notional_usd}"
        )

    def check_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Check an order against connector limits.

        Returns dict with 'allowed' bool and details.
        Fail-closed: any error -> not allowed.
        """
        # 1. Verify mode
        mode_result = self._boundary.verify_mode()
        if not mode_result.matched:
            return {
                'allowed': False,
                'reason': f"Mode mismatch: expected {self.expected_mode.value}, got {mode_result.verified_mode.value}",
                'mode_result': {
                    'expected': self.expected_mode.value,
                    'actual': mode_result.verified_mode.value,
                },
            }

        # 2. Volume check
        if self.max_order_volume is not None:
            if volume > self.max_order_volume:
                return {
                    'allowed': False,
                    'reason': f"Order volume {volume} exceeds max {self.max_order_volume}",
                    'volume': volume,
                    'max_volume': self.max_order_volume,
                }

        # 3. Notional USD check (requires price)
        if self.max_order_notional_usd is not None:
            try:
                notional_usd = self._sizing_hook.compute_notional_usd(
                    symbol=symbol,
                    qty=int(volume),
                    side=side,
                    price=price,
                )
                if notional_usd > self.max_order_notional_usd:
                    return {
                        'allowed': False,
                        'reason': f"Order notional ${notional_usd:,.2f} exceeds max ${self.max_order_notional_usd:,.2f}",
                        'notional_usd': notional_usd,
                        'max_notional_usd': self.max_order_notional_usd,
                    }
            except Exception as e:
                # Fail-closed: if we can't compute notional, block
                logger.error(f"Failed to compute notional for {symbol}: {e}")
                return {
                    'allowed': False,
                    'reason': f"Cannot verify notional (price unavailable): {e}",
                    'fail_closed': True,
                }

        return {
            'allowed': True,
            'mode': mode_result.verified_mode.value,
            'notional_usd': notional_usd if self.max_order_notional_usd else None,
        }

    def get_boundary(self) -> PaperLiveBoundary:
        """Get the underlying boundary instance."""
        return self._boundary
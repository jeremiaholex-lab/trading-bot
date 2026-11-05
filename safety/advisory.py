"""
Pre-Trade Advisory Interface - Broker-Agnostic "Is This Wise?" Layer

Implements Vibe-Trading's PreTradeAdvisoryInterface pattern:
- Sits ABOVE the mandate gate (which answers "is this allowed?")
- Advisory answers "is this wise given current account state?"
- Broker-agnostic: works across all connectors
- Capital-scale-aware: considers equity, drawdown, open positions
- Fail-open: if advisory service unreachable, order proceeds
- Verdict is signed and recomputable for audit trail
- Supports multiple advisory providers with aggregation
"""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AdvisoryVerdict(Enum):
    """Advisory verdict on a proposed order."""
    APPROVE = "approve"                    # Order is sound, proceed
    APPROVE_WITH_CONCERNS = "approve_with_concerns"  # Proceed but log concerns
    REJECT = "reject"                      # Order is unwise, recommend hold
    REVIEW_UNAVAILABLE = "review_unavailable"  # Advisory service unreachable, fail-open


@dataclass(frozen=True)
class AdvisoryContext:
    """
    Context passed to advisory providers for review.

    Contains capital-scale state needed for a "wise?" verdict.
    Frozen to prevent mutation during review.
    """
    # Order intent
    symbol: str
    side: str                          # "buy" or "sell"
    notional_usd: float
    qty: int

    # Account state (capital-scale awareness)
    account_equity: float
    account_drawdown: float            # Current drawdown from peak (0.0 to 1.0)
    open_position_count: int
    total_exposure_usd: float          # Sum of all open position notionals
    funding_usd: float                 # Available margin/funding

    # Position context
    existing_position_qty: int = 0     # Qty already held in this symbol
    existing_position_notional: float = 0.0

    # Risk context
    daily_pnl_usd: float = 0.0
    daily_pnl_pct: float = 0.0

    # Metadata
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'symbol': self.symbol,
            'side': self.side,
            'notional_usd': self.notional_usd,
            'qty': self.qty,
            'account_equity': self.account_equity,
            'account_drawdown': self.account_drawdown,
            'open_position_count': self.open_position_count,
            'total_exposure_usd': self.total_exposure_usd,
            'funding_usd': self.funding_usd,
            'existing_position_qty': self.existing_position_qty,
            'existing_position_notional': self.existing_position_notional,
            'daily_pnl_usd': self.daily_pnl_usd,
            'daily_pnl_pct': self.daily_pnl_pct,
            'timestamp': self.timestamp,
            'request_id': self.request_id,
        }


@dataclass(frozen=True)
class AdvisoryResult:
    """Result from a single advisory provider."""
    provider_id: str
    verdict: AdvisoryVerdict
    concerns: List[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 1.0            # Provider's confidence in verdict (0.0 to 1.0)
    latency_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'provider_id': self.provider_id,
            'verdict': self.verdict.value,
            'concerns': self.concerns,
            'summary': self.summary,
            'confidence': self.confidence,
            'latency_ms': self.latency_ms,
            'timestamp': self.timestamp,
            'metadata': self.metadata,
        }


@dataclass(frozen=True)
class AggregatedVerdict:
    """Aggregated verdict from multiple advisory providers."""
    final_verdict: AdvisoryVerdict
    all_results: List[AdvisoryResult]
    all_concerns: List[str]
    provider_count: int
    approving_count: int
    rejecting_count: int
    unavailable_count: int
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_approved(self) -> bool:
        return self.final_verdict in (AdvisoryVerdict.APPROVE, AdvisoryVerdict.APPROVE_WITH_CONCERNS)

    @property
    def is_rejected(self) -> bool:
        return self.final_verdict == AdvisoryVerdict.REJECT

    @property
    def is_unavailable(self) -> bool:
        return self.final_verdict == AdvisoryVerdict.REVIEW_UNAVAILABLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            'final_verdict': self.final_verdict.value,
            'provider_count': self.provider_count,
            'approving_count': self.approving_count,
            'rejecting_count': self.rejecting_count,
            'unavailable_count': self.unavailable_count,
            'all_concerns': self.all_concerns,
            'results': [r.to_dict() for r in self.all_results],
            'timestamp': self.timestamp,
        }


class PreTradeAdvisoryInterface(ABC):
    """
    Abstract interface for pre-trade advisory providers.

    Implementations must be:
    - Fast (target < 200ms)
    - Stateless (no side effects)
    - Fail-open (return REVIEW_UNAVAILABLE on any error)
    - Deterministic for same inputs (recomputable for audit)
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique identifier for this advisory provider."""
        pass

    @abstractmethod
    def review(self, context: AdvisoryContext) -> AdvisoryResult:
        """
        Review a proposed order and return a verdict.

        Must complete within timeout. Any exception results in
        REVIEW_UNAVAILABLE being returned by the orchestrator.

        Args:
            context: Full capital-scale context for the proposed order

        Returns:
            AdvisoryResult with verdict, concerns, and summary
        """
        pass

    def health_check(self) -> bool:
        """Optional health check. Default returns True."""
        return True


class MockAdvisory(PreTradeAdvisoryInterface):
    """
    Mock advisory provider for testing.

    Configurable verdict, delay, exception injection, and call history recording.
    No network dependencies.
    """

    def __init__(
        self,
        provider_id: str = "mock_advisory",
        verdict: AdvisoryVerdict = AdvisoryVerdict.APPROVE,
        concerns: Optional[List[str]] = None,
        summary: str = "Mock advisory approval",
        delay_ms: float = 0.0,
        raise_exception: bool = False,
        exception_type: type = Exception,
        exception_message: str = "Mock advisory error",
    ):
        self._provider_id = provider_id
        self._verdict = verdict
        self._concerns = concerns or []
        self._summary = summary
        self._delay_ms = delay_ms
        self._raise_exception = raise_exception
        self._exception_type = exception_type
        self._exception_message = exception_message

        # Call history for test assertions
        self.call_history: List[AdvisoryContext] = []
        self._lock = __import__('threading').RLock()

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def review(self, context: AdvisoryContext) -> AdvisoryResult:
        start_time = time.perf_counter()

        with self._lock:
            self.call_history.append(context)

        if self._raise_exception:
            raise self._exception_type(self._exception_message)

        if self._delay_ms > 0:
            time.sleep(self._delay_ms / 1000.0)

        latency_ms = (time.perf_counter() - start_time) * 1000

        return AdvisoryResult(
            provider_id=self._provider_id,
            verdict=self._verdict,
            concerns=list(self._concerns),
            summary=self._summary,
            confidence=1.0,
            latency_ms=latency_ms,
            metadata={'mock': True},
        )

    def configure(
        self,
        verdict: Optional[AdvisoryVerdict] = None,
        concerns: Optional[List[str]] = None,
        summary: Optional[str] = None,
        delay_ms: Optional[float] = None,
        raise_exception: Optional[bool] = None,
    ):
        """Update mock configuration."""
        if verdict is not None:
            self._verdict = verdict
        if concerns is not None:
            self._concerns = concerns
        if summary is not None:
            self._summary = summary
        if delay_ms is not None:
            self._delay_ms = delay_ms
        if raise_exception is not None:
            self._raise_exception = raise_exception

    def get_call_history(self) -> List[AdvisoryContext]:
        """Get recorded call history for test assertions."""
        with self._lock:
            return list(self.call_history)

    def clear_history(self):
        """Clear call history."""
        with self._lock:
            self.call_history.clear()


class AdvisoryOrchestrator:
    """
    Orchestrates multiple advisory providers with fail-open aggregation.

    Execution model:
    1. Run all providers sequentially (fast local first, remote last)
    2. Any provider exception -> REVIEW_UNAVAILABLE for that provider
    3. Aggregate using worst-case logic:
       - If ANY provider REJECTs -> final REJECT
       - Else if ANY provider APPROVE_WITH_CONCERNS -> final APPROVE_WITH_CONCERNS
       - Else if ALL providers APPROVE -> final APPROVE
       - Else if ALL providers UNAVAILABLE -> final REVIEW_UNAVAILABLE
    4. Collect all concerns from all providers
    """

    def __init__(
        self,
        providers: Optional[List[PreTradeAdvisoryInterface]] = None,
        timeout_ms: float = 200.0,
    ):
        self.providers = providers or []
        self.timeout_ms = timeout_ms
        self._lock = __import__('threading').RLock()

    def add_provider(self, provider: PreTradeAdvisoryInterface):
        """Add an advisory provider."""
        with self._lock:
            self.providers.append(provider)

    def remove_provider(self, provider_id: str) -> bool:
        """Remove a provider by ID."""
        with self._lock:
            for i, p in enumerate(self.providers):
                if p.provider_id == provider_id:
                    self.providers.pop(i)
                    return True
            return False

    def review(self, context: AdvisoryContext) -> AggregatedVerdict:
        """
        Run all providers and aggregate their verdicts.

        Fail-open: if all providers are unavailable or error,
        returns REVIEW_UNAVAILABLE and order proceeds.
        """
        start_time = time.perf_counter()
        results: List[AdvisoryResult] = []

        with self._lock:
            providers = list(self.providers)

        if not providers:
            # No providers configured - approve by default (fail-open)
            return AggregatedVerdict(
                final_verdict=AdvisoryVerdict.APPROVE,
                all_results=[],
                all_concerns=[],
                provider_count=0,
                approving_count=0,
                rejecting_count=0,
                unavailable_count=0,
            )

        for provider in providers:
            try:
                # Run with timeout
                result = self._run_with_timeout(provider, context)
                results.append(result)
            except Exception as e:
                logger.warning(f"Advisory provider {provider.provider_id} failed: {e}")
                # Fail-open: record as unavailable
                results.append(AdvisoryResult(
                    provider_id=provider.provider_id,
                    verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                    concerns=[f"Provider error: {e}"],
                    summary="Advisory provider unavailable",
                    confidence=0.0,
                    latency_ms=0.0,
                    metadata={'error': str(e)},
                ))

        # Aggregate using worst-case logic
        final_verdict = self._aggregate_verdicts(results)
        all_concerns = []
        for r in results:
            all_concerns.extend(r.concerns)

        approving = sum(1 for r in results if r.verdict == AdvisoryVerdict.APPROVE)
        rejecting = sum(1 for r in results if r.verdict == AdvisoryVerdict.REJECT)
        unavailable = sum(1 for r in results if r.verdict == AdvisoryVerdict.REVIEW_UNAVAILABLE)
        concerns_count = sum(1 for r in results if r.verdict == AdvisoryVerdict.APPROVE_WITH_CONCERNS)

        return AggregatedVerdict(
            final_verdict=final_verdict,
            all_results=results,
            all_concerns=all_concerns,
            provider_count=len(providers),
            approving_count=approving + concerns_count,
            rejecting_count=rejecting,
            unavailable_count=unavailable,
        )

    def _run_with_timeout(
        self,
        provider: PreTradeAdvisoryInterface,
        context: AdvisoryContext,
    ) -> AdvisoryResult:
        """Run provider review with timeout."""
        # Simple timeout via threading - in production could use asyncio or process pool
        import threading
        import queue

        result_queue = queue.Queue(maxsize=1)

        def worker():
            try:
                result = provider.review(context)
                result_queue.put(('success', result))
            except Exception as e:
                result_queue.put(('error', e))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=self.timeout_ms / 1000.0)

        if thread.is_alive():
            # Timeout - return unavailable
            return AdvisoryResult(
                provider_id=provider.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=[f"Provider timeout ({self.timeout_ms}ms)"],
                summary="Advisory provider timed out",
                confidence=0.0,
                latency_ms=self.timeout_ms,
                metadata={'timeout': True},
            )

        try:
            status, result = result_queue.get_nowait()
            if status == 'error':
                raise result
            return result
        except queue.Empty:
            return AdvisoryResult(
                provider_id=provider.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=["No result returned"],
                summary="Advisory provider returned no result",
                confidence=0.0,
                latency_ms=self.timeout_ms,
            )

    def _aggregate_verdicts(self, results: List[AdvisoryResult]) -> AdvisoryVerdict:
        """Aggregate multiple verdicts using worst-case logic."""
        if not results:
            return AdvisoryVerdict.APPROVE  # Fail-open default

        # If any REJECT -> final REJECT
        if any(r.verdict == AdvisoryVerdict.REJECT for r in results):
            return AdvisoryVerdict.REJECT

        # If any APPROVE_WITH_CONCERNS -> final APPROVE_WITH_CONCERNS
        if any(r.verdict == AdvisoryVerdict.APPROVE_WITH_CONCERNS for r in results):
            return AdvisoryVerdict.APPROVE_WITH_CONCERNS

        # If all APPROVE -> final APPROVE
        if all(r.verdict == AdvisoryVerdict.APPROVE for r in results):
            return AdvisoryVerdict.APPROVE

        # If all UNAVAILABLE -> final REVIEW_UNAVAILABLE
        if all(r.verdict == AdvisoryVerdict.REVIEW_UNAVAILABLE for r in results):
            return AdvisoryVerdict.REVIEW_UNAVAILABLE

        # Mixed with some unavailable - conservative: if any approve, approve with concerns
        if any(r.verdict == AdvisoryVerdict.APPROVE for r in results):
            return AdvisoryVerdict.APPROVE_WITH_CONCERNS

        # Default to unavailable (fail-open)
        return AdvisoryVerdict.REVIEW_UNAVAILABLE
"""
HTTP Advisory Provider - External Pre-Trade Advisory Service

Implements Vibe-Trading's InvinoveritasAdvisory pattern:
- Calls external `/review` endpoint via HTTP
- Broker-agnostic, capital-scale-aware
- Fail-open on any error (timeout, network, HTTP error)
- x402 payment support (USDC on Base, Lightning)
- Recomputable verdicts for audit trail
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .advisory import (
    PreTradeAdvisoryInterface,
    AdvisoryVerdict,
    AdvisoryContext,
    AdvisoryResult,
)

logger = logging.getLogger(__name__)


@dataclass
class HTTPAdvisoryConfig:
    """Configuration for HTTP advisory provider."""
    endpoint: str                    # e.g., "https://api.example.com/mcp"
    provider_id: str                 # Unique provider identifier
    api_key: Optional[str] = None    # Optional API key for auth
    timeout_ms: float = 150.0        # Request timeout
    payment_header: Optional[str] = None  # x402 payment header if needed
    verify_ssl: bool = True          # SSL verification


class HTTPAdvisoryProvider(PreTradeAdvisoryInterface):
    """
    HTTP-based pre-trade advisory provider.

    Calls external `/review` endpoint with AdvisoryContext.
    Returns signed, recomputable verdict.

    Expected endpoint contract:
    POST /review
    Body: AdvisoryContext (JSON)
    Response: {
        "verdict": "approve" | "approve_with_concerns" | "reject" | "review_unavailable",
        "concerns": ["concern1", "concern2"],
        "summary": "Human-readable summary",
        "confidence": 0.95,
        "signature": "cryptographic_signature_for_audit",
        "metadata": {}
    }
    """

    def __init__(self, config: HTTPAdvisoryConfig):
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': f'VibeTrading-Advisory/{config.provider_id}',
        })
        if config.api_key:
            self._session.headers['Authorization'] = f'Bearer {config.api_key}'
        if config.payment_header:
            self._session.headers['X-Payment'] = config.payment_header

    @property
    def provider_id(self) -> str:
        return self.config.provider_id

    def review(self, context: AdvisoryContext) -> AdvisoryResult:
        start_time = time.perf_counter()
        request_id = str(uuid.uuid4())[:8]

        try:
            # Prepare request payload
            payload = context.to_dict()
            payload['request_id'] = request_id

            # Make request
            response = self._session.post(
                f"{self.config.endpoint.rstrip('/')}/review",
                json=payload,
                timeout=self.config.timeout_ms / 1000.0,
                verify=self.config.verify_ssl,
            )

            latency_ms = (time.perf_counter() - start_time) * 1000

            # Parse response
            if response.status_code == 200:
                data = response.json()
                return self._parse_response(data, latency_ms)

            # Non-200 -> fail-open
            logger.warning(
                f"Advisory {self.config.provider_id} returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            return AdvisoryResult(
                provider_id=self.config.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=[f"HTTP {response.status_code}"],
                summary="Advisory service returned error",
                confidence=0.0,
                latency_ms=latency_ms,
                metadata={'status_code': response.status_code},
            )

        except requests.Timeout:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(f"Advisory {self.config.provider_id} timeout ({self.config.timeout_ms}ms)")
            return AdvisoryResult(
                provider_id=self.config.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=[f"Timeout ({self.config.timeout_ms}ms)"],
                summary="Advisory service timed out",
                confidence=0.0,
                latency_ms=latency_ms,
                metadata={'timeout': True},
            )

        except requests.RequestException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(f"Advisory {self.config.provider_id} request error: {e}")
            return AdvisoryResult(
                provider_id=self.config.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=[f"Request error: {type(e).__name__}"],
                summary="Advisory service unreachable",
                confidence=0.0,
                latency_ms=latency_ms,
                metadata={'error': str(e)},
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Advisory {self.config.provider_id} unexpected error: {e}")
            return AdvisoryResult(
                provider_id=self.config.provider_id,
                verdict=AdvisoryVerdict.REVIEW_UNAVAILABLE,
                concerns=[f"Unexpected error: {type(e).__name__}"],
                summary="Advisory provider error",
                confidence=0.0,
                latency_ms=latency_ms,
                metadata={'error': str(e)},
            )

    def _parse_response(self, data: Dict, latency_ms: float) -> AdvisoryResult:
        """Parse and validate advisory response."""
        # Validate verdict
        verdict_str = data.get('verdict', '').lower()
        try:
            verdict = AdvisoryVerdict(verdict_str)
        except ValueError:
            logger.warning(f"Invalid verdict '{verdict_str}' from {self.config.provider_id}")
            verdict = AdvisoryVerdict.REVIEW_UNAVAILABLE

        # Extract concerns
        concerns = data.get('concerns', [])
        if not isinstance(concerns, list):
            concerns = [str(concerns)]

        # Extract summary
        summary = data.get('summary', '')
        if not summary and verdict != AdvisoryVerdict.APPROVE:
            summary = f"Advisory verdict: {verdict.value}"

        # Extract confidence
        confidence = data.get('confidence', 1.0)
        if not isinstance(confidence, (int, float)):
            confidence = 1.0
        confidence = max(0.0, min(1.0, float(confidence)))

        # Verify signature if present (for audit trail)
        signature = data.get('signature')
        if signature:
            # In production, verify cryptographic signature
            # This ensures the verdict is recomputable and tamper-evident
            pass

        return AdvisoryResult(
            provider_id=self.config.provider_id,
            verdict=verdict,
            concerns=concerns,
            summary=summary,
            confidence=confidence,
            latency_ms=latency_ms,
            metadata={
                'signature': signature,
                'raw_response': data,
            },
        )

    def health_check(self) -> bool:
        """Check if advisory service is reachable."""
        try:
            response = self._session.get(
                f"{self.config.endpoint.rstrip('/')}/health",
                timeout=5.0,
                verify=self.config.verify_ssl,
            )
            return response.status_code == 200
        except Exception:
            return False


class HTTPAdvisoryProviderFactory:
    """Factory for creating HTTP advisory providers from config."""

    @staticmethod
    def create_from_env(prefix: str = "ADVISORY_") -> List[HTTPAdvisoryProvider]:
        """Create providers from environment variables.

        Expected format:
        ADVISORY_1_ENDPOINT=https://api.example.com/mcp
        ADVISORY_1_PROVIDER_ID=invinoveritas
        ADVISORY_1_API_KEY=xxx
        ADVISORY_1_TIMEOUT_MS=150

        Multiple providers: ADVISORY_2_*, ADVISORY_3_*, etc.
        """
        import os
        providers = []

        for i in range(1, 10):  # Support up to 9 providers
            endpoint = os.getenv(f"{prefix}{i}_ENDPOINT")
            if not endpoint:
                continue

            provider_id = os.getenv(f"{prefix}{i}_PROVIDER_ID", f"advisory_{i}")
            api_key = os.getenv(f"{prefix}{i}_API_KEY")
            timeout_ms = float(os.getenv(f"{prefix}{i}_TIMEOUT_MS", "150"))
            payment_header = os.getenv(f"{prefix}{i}_PAYMENT_HEADER")
            verify_ssl = os.getenv(f"{prefix}{i}_VERIFY_SSL", "true").lower() == "true"

            config = HTTPAdvisoryConfig(
                endpoint=endpoint,
                provider_id=provider_id,
                api_key=api_key,
                timeout_ms=timeout_ms,
                payment_header=payment_header,
                verify_ssl=verify_ssl,
            )
            providers.append(HTTPAdvisoryProvider(config))
            logger.info(f"Created HTTP advisory provider: {provider_id} -> {endpoint}")

        return providers
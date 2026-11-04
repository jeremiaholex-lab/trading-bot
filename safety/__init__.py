"""
Safety Infrastructure Module

Vibe-Trading inspired safety primitives for institutional-grade trading:
- MandateGate: Structural pre-trade compliance (universe, size, leverage, daily cap, kill switch)
- KillSwitch: Filesystem sentinel for emergency halt with audit trail
- AuditLedger: Hash-chained immutable audit log for every trade decision
- PreTradeAdvisoryInterface: Broker-agnostic advisory layer ("is this wise?")
- PaperLiveBoundary: Structural paper/live separation with fail-closed enforcement
- HTTPAdvisoryProvider: External advisory service integration
"""

from .mandate_gate import MandateGate, MandateViolation, MandateViolationType, MandateContext
from .kill_switch import KillSwitch, KillSwitchState, KillSwitchEvent
from .audit_ledger import AuditLedger, AuditEntry, AuditEntryType
from .advisory import (
    PreTradeAdvisoryInterface,
    AdvisoryVerdict,
    AdvisoryContext,
    AdvisoryResult,
    AdvisoryOrchestrator,
    AggregatedVerdict,
    MockAdvisory,
)
from .paper_live_boundary import (
    PaperLiveBoundary,
    AccountMode,
    ModeMismatchError,
    SizingHook,
    ConnectorPaperLiveBoundary,
)
from .http_advisory import HTTPAdvisoryProvider, HTTPAdvisoryConfig, HTTPAdvisoryProviderFactory

__all__ = [
    "MandateGate",
    "MandateViolation",
    "MandateViolationType",
    "MandateContext",
    "KillSwitch",
    "KillSwitchState",
    "KillSwitchEvent",
    "AuditLedger",
    "AuditEntry",
    "AuditEntryType",
    "PreTradeAdvisoryInterface",
    "AdvisoryVerdict",
    "AdvisoryContext",
    "AdvisoryResult",
    "AdvisoryOrchestrator",
    "AggregatedVerdict",
    "MockAdvisory",
    "PaperLiveBoundary",
    "AccountMode",
    "ModeMismatchError",
    "SizingHook",
    "ConnectorPaperLiveBoundary",
    "HTTPAdvisoryProvider",
    "HTTPAdvisoryConfig",
    "HTTPAdvisoryProviderFactory",
]
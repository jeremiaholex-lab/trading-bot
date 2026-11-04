"""
Kill Switch - Emergency Halt with Audit Trail

Implements Vibe-Trading's kill switch pattern:
- Filesystem sentinel for instant activation
- Survives process restarts (persisted to disk)
- Blocks ALL new order entries immediately
- Allows order cancellations (flatten positions)
- Full audit trail of activation/deactivation
- Flatten latch prevents double-flatten on restart
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List

from config import Config

logger = logging.getLogger(__name__)


class KillSwitchState(Enum):
    """Kill switch states."""
    INACTIVE = "inactive"
    ACTIVE = "active"
    FLATTENING = "flattening"
    FLATTENED = "flattened"


@dataclass
class KillSwitchEvent:
    """Record of a kill switch state change."""
    timestamp: str
    previous_state: KillSwitchState
    new_state: KillSwitchState
    reason: str
    triggered_by: str  # user, system, api
    details: Dict[str, Any] = field(default_factory=dict)


class KillSwitch:
    """
    Emergency trading halt mechanism.

    Features:
    - Filesystem sentinel (atomic write) for instant activation
    - Survives process restarts - state persisted to disk
    - Blocks ALL new entries, allows cancellations
    - Flatten mode: cancels all orders, closes all positions
    - Flatten latch prevents double-flatten on restart
    - Full audit trail of every activation/deactivation
    - Thread-safe for concurrent access
    """

    def __init__(
        self,
        sentinel_path: Optional[Path] = None,
        audit_ledger: Optional['AuditLedger'] = None,
        state_path: Optional[Path] = None,
    ):
        self.sentinel_path = sentinel_path or getattr(Config, 'KILL_SWITCH_SENTINEL_PATH', None)
        if self.sentinel_path is None:
            self.sentinel_path = Path(Config.BASE_DIR) / "kill_switch.sentinel"

        self.state_path = state_path or Path(Config.BASE_DIR) / "kill_switch_state.json"
        self.audit_ledger = audit_ledger

        self._state = KillSwitchState.INACTIVE
        self._flatten_latch = False  # Prevents double-flatten on restart
        self._lock = threading.RLock()
        self._activation_time: Optional[str] = None
        self._activation_reason: Optional[str] = None

        # Load persisted state
        self._load_state()

        logger.info(f"KillSwitch initialized: state={self._state.value}, "
                   f"flatten_latch={self._flatten_latch}")

    def _load_state(self):
        """Load kill switch state from disk."""
        # Check sentinel file first (authoritative for active state)
        if self.sentinel_path.exists():
            try:
                sentinel_data = json.loads(self.sentinel_path.read_text(encoding='utf-8'))
                if sentinel_data.get('active', False):
                    self._state = KillSwitchState.ACTIVE
                    self._activation_time = sentinel_data.get('activated_at')
                    self._activation_reason = sentinel_data.get('reason', 'Unknown')
                    logger.warning(f"Kill switch ACTIVE from sentinel: "
                                  f"activated at {self._activation_time}, reason: {self._activation_reason}")
            except Exception as e:
                logger.error(f"Could not read kill switch sentinel: {e}")
                # Fail-safe: assume active if sentinel exists but unreadable
                self._state = KillSwitchState.ACTIVE

        # Load detailed state (flatten latch, history)
        if self.state_path.exists():
            try:
                state_data = json.loads(self.state_path.read_text(encoding='utf-8'))
                self._flatten_latch = state_data.get('flatten_latch', False)
                if 'state' in state_data:
                    self._state = KillSwitchState(state_data['state'])
                logger.info(f"Loaded kill switch state: "
                           f"state={self._state.value}, flatten_latch={self._flatten_latch}")
            except Exception as e:
                logger.warning(f"Could not load kill switch state: {e}")

    def _save_state(self):
        """Persist kill switch state to disk."""
        try:
            state_data = {
                'state': self._state.value,
                'flatten_latch': self._flatten_latch,
                'activation_time': self._activation_time,
                'activation_reason': self._activation_reason,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(state_data, indent=2), encoding='utf-8')
            tmp.replace(self.state_path)
        except Exception as e:
            logger.error(f"Failed to save kill switch state: {e}")

    def _write_sentinel(self, active: bool, reason: str = ""):
        """Write/remove the filesystem sentinel atomically."""
        try:
            if active:
                sentinel_data = {
                    'active': True,
                    'activated_at': datetime.now(timezone.utc).isoformat(),
                    'reason': reason,
                }
                tmp = self.sentinel_path.with_suffix('.tmp')
                tmp.write_text(json.dumps(sentinel_data, indent=2), encoding='utf-8')
                tmp.replace(self.sentinel_path)
                logger.warning(f"Kill switch SENTINEL CREATED: {reason}")
            else:
                if self.sentinel_path.exists():
                    self.sentinel_path.unlink()
                    logger.info("Kill switch SENTINEL REMOVED")
        except Exception as e:
            logger.error(f"Failed to write kill switch sentinel: {e}")

    def _audit_event(self, event: KillSwitchEvent):
        """Record kill switch event to audit ledger."""
        if self.audit_ledger:
            try:
                self.audit_ledger.log_kill_switch_event(event)
            except Exception as e:
                logger.error(f"Failed to audit kill switch event: {e}")

    def activate(self, reason: str = "Manual activation", triggered_by: str = "user") -> bool:
        """
        Activate the kill switch - blocks all new entries immediately.

        Returns True if state changed, False if already active.
        """
        with self._lock:
            if self._state == KillSwitchState.ACTIVE:
                logger.warning(f"Kill switch already active, ignoring activation: {reason}")
                return False

            previous_state = self._state
            self._state = KillSwitchState.ACTIVE
            self._activation_time = datetime.now(timezone.utc).isoformat()
            self._activation_reason = reason

            # Write sentinel (atomic, survives restart)
            self._write_sentinel(True, reason)
            self._save_state()

            # Audit
            event = KillSwitchEvent(
                timestamp=self._activation_time,
                previous_state=previous_state,
                new_state=KillSwitchState.ACTIVE,
                reason=reason,
                triggered_by=triggered_by,
            )
            self._audit_event(event)

            logger.critical(f"KILL SWITCH ACTIVATED: {reason} (by {triggered_by})")
            return True

    def deactivate(self, reason: str = "Manual deactivation", triggered_by: str = "user") -> bool:
        """
        Deactivate the kill switch - allows new entries again.

        Returns True if state changed, False if already inactive.
        """
        with self._lock:
            if self._state == KillSwitchState.INACTIVE:
                logger.info(f"Kill switch already inactive, ignoring deactivation: {reason}")
                return False

            previous_state = self._state
            self._state = KillSwitchState.INACTIVE
            deactivation_time = datetime.now(timezone.utc).isoformat()

            # Remove sentinel
            self._write_sentinel(False)
            self._save_state()

            # Audit
            event = KillSwitchEvent(
                timestamp=deactivation_time,
                previous_state=previous_state,
                new_state=KillSwitchState.INACTIVE,
                reason=reason,
                triggered_by=triggered_by,
                details={
                    'was_active_since': self._activation_time,
                    'activation_reason': self._activation_reason,
                }
            )
            self._audit_event(event)

            self._activation_time = None
            self._activation_reason = None

            logger.info(f"KILL SWITCH DEACTIVATED: {reason} (by {triggered_by})")
            return True

    def start_flatten(self, reason: str = "Flatten on halt", triggered_by: str = "system") -> bool:
        """
        Initiate position flattening - cancel all orders, close all positions.

        Returns True if flatten started, False if already flattening/flattened
        or if flatten latch is set (prevents double-flatten on restart).
        """
        with self._lock:
            if self._flatten_latch:
                logger.warning("Flatten latch is SET - refusing to flatten again. "
                              "This prevents double-flatten on restart.")
                return False

            if self._state in (KillSwitchState.FLATTENING, KillSwitchState.FLATTENED):
                logger.warning(f"Already in flatten state: {self._state.value}")
                return False

            previous_state = self._state
            self._state = KillSwitchState.FLATTENING
            self._flatten_latch = True
            self._save_state()

            event = KillSwitchEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                previous_state=previous_state,
                new_state=KillSwitchState.FLATTENING,
                reason=reason,
                triggered_by=triggered_by,
            )
            self._audit_event(event)

            logger.critical(f"KILL SWITCH FLATTENING STARTED: {reason}")
            return True

    def complete_flatten(self, reason: str = "Flatten completed", triggered_by: str = "system"):
        """Mark flattening as complete."""
        with self._lock:
            previous_state = self._state
            self._state = KillSwitchState.FLATTENED
            self._save_state()

            event = KillSwitchEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                previous_state=previous_state,
                new_state=KillSwitchState.FLATTENED,
                reason=reason,
                triggered_by=triggered_by,
            )
            self._audit_event(event)

            logger.critical(f"KILL SWITCH FLATTEN COMPLETED: {reason}")

    def is_active(self) -> bool:
        """Check if kill switch is active (blocks new entries)."""
        return self._state in (KillSwitchState.ACTIVE, KillSwitchState.FLATTENING, KillSwitchState.FLATTENED)

    def allows_new_entries(self) -> bool:
        """Check if new order entries are allowed."""
        return self._state == KillSwitchState.INACTIVE

    def allows_cancellations(self) -> bool:
        """Check if order cancellations are allowed (always True)."""
        return True  # Cancellations always allowed for safety

    def get_state(self) -> KillSwitchState:
        """Get current kill switch state."""
        return self._state

    def get_status(self) -> Dict[str, Any]:
        """Get detailed status for reporting."""
        return {
            'state': self._state.value,
            'allows_new_entries': self.allows_new_entries(),
            'allows_cancellations': self.allows_cancellations(),
            'flatten_latch': self._flatten_latch,
            'activation_time': self._activation_time,
            'activation_reason': self._activation_reason,
            'sentinel_exists': self.sentinel_path.exists(),
        }

    def reset_flatten_latch(self, reason: str = "Manual latch reset", triggered_by: str = "user"):
        """Reset the flatten latch (use with caution - allows re-flattening)."""
        with self._lock:
            if not self._flatten_latch:
                logger.info("Flatten latch already reset")
                return

            self._flatten_latch = False
            self._save_state()

            event = KillSwitchEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                previous_state=self._state,
                new_state=self._state,
                reason=reason,
                triggered_by=triggered_by,
                details={'action': 'flatten_latch_reset'}
            )
            self._audit_event(event)

            logger.warning(f"KILL SWITCH FLATTEN LATCH RESET: {reason} (by {triggered_by})")

    def force_inactive(self, reason: str = "Force inactive", triggered_by: str = "user"):
        """Emergency override to force inactive state (use with extreme caution)."""
        with self._lock:
            logger.critical(f"KILL SWITCH FORCE INACTIVE: {reason} (by {triggered_by})")
            self._state = KillSwitchState.INACTIVE
            self._write_sentinel(False)
            self._save_state()

            event = KillSwitchEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                previous_state=self._state,
                new_state=KillSwitchState.INACTIVE,
                reason=reason,
                triggered_by=triggered_by,
                details={'action': 'force_inactive'}
            )
            self._audit_event(event)
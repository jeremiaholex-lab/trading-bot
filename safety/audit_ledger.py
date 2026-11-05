"""
Audit Ledger - Hash-Chained Immutable Audit Trail

Implements Vibe-Trading's audit ledger pattern:
- Every trade decision, mandate check, kill switch event logged
- Hash-chained entries for tamper evidence
- Structured entries with full context for reconstruction
- Date-based partitioning for efficient queries
- Atomic writes with crash-safe durability
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)


class AuditEntryType(Enum):
    """Types of audit entries."""
    ORDER_SUBMITTED = "order_submitted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    TRADE_FILLED = "trade_filled"
    TRADE_REJECTED = "trade_rejected"
    MANDATE_DECISION = "mandate_decision"
    MANDATE_VIOLATION = "mandate_violation"
    KILL_SWITCH_EVENT = "kill_switch_event"
    ADVISORY_REVIEW = "advisory_review"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_MODIFIED = "position_modified"
    RISK_EVENT = "risk_event"
    SYSTEM_EVENT = "system_event"
    ERROR = "error"


@dataclass
class AuditEntry:
    """Immutable audit entry with hash chain."""
    # Core identity
    entry_id: str  # UUID or sequential ID
    entry_type: AuditEntryType
    timestamp: str  # ISO 8601 UTC

    # Hash chain for tamper evidence
    previous_hash: str  # Hash of previous entry
    entry_hash: str     # Hash of this entry (computed after creation)

    # Context
    symbol: Optional[str] = None
    side: Optional[str] = None  # buy/sell
    notional_usd: Optional[float] = None
    qty: Optional[int] = None
    price: Optional[float] = None

    # Decision details
    allowed: Optional[bool] = None
    violations: List[Dict] = field(default_factory=list)
    verdict: Optional[str] = None

    # Full context snapshot (for reconstruction)
    context: Dict[str, Any] = field(default_factory=dict)

    # Metadata
    source: str = "trading_bot"  # Component that generated this entry
    version: int = 1


class AuditLedger:
    """
    Hash-chained immutable audit ledger for all trading decisions.

    Features:
    - Every entry cryptographically linked to previous (hash chain)
    - Tamper-evident: modifying any entry breaks the chain
    - Date-partitioned storage for efficient queries
    - Atomic writes with crash-safe durability
    - Structured queries by date, symbol, entry type
    - Full context snapshots for forensic reconstruction
    """

    def __init__(
        self,
        ledger_dir: Optional[Path] = None,
        max_entries_per_file: int = 10000,
    ):
        self.ledger_dir = ledger_dir or Path(Config.BASE_DIR) / "audit_ledger"
        self.max_entries_per_file = max_entries_per_file

        self._lock = threading.RLock()
        self._current_file: Optional[Path] = None
        self._current_file_entries: int = 0
        self._last_hash: str = "0" * 64  # Genesis hash

        # Ensure directory exists
        self.ledger_dir.mkdir(parents=True, exist_ok=True)

        # Initialize by finding the last entry hash
        self._initialize_chain()

        logger.info(f"AuditLedger initialized: dir={self.ledger_dir}, "
                   f"last_hash={self._last_hash[:16]}...")

    def _initialize_chain(self):
        """Find the last entry in the chain to continue from."""
        try:
            # Find all ledger files sorted by date
            files = sorted(self.ledger_dir.glob("ledger_*.jsonl"))
            if not files:
                self._last_hash = "0" * 64
                return

            # Read the last file backwards to find the last entry
            last_file = files[-1]
            with open(last_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if lines:
                    last_entry = json.loads(lines[-1])
                    self._last_hash = last_entry.get('entry_hash', '0' * 64)
                    logger.info(f"Resumed chain from {last_file.name}: "
                               f"last_hash={self._last_hash[:16]}...")
        except Exception as e:
            logger.warning(f"Could not initialize audit chain: {e}")
            self._last_hash = "0" * 64

    def _get_current_file(self) -> Path:
        """Get or create the current ledger file for today."""
        today = datetime.now(timezone.utc).date().isoformat()
        expected_file = self.ledger_dir / f"ledger_{today}.jsonl"

        with self._lock:
            if self._current_file != expected_file:
                self._current_file = expected_file
                self._current_file_entries = 0
                if expected_file.exists():
                    # Count existing entries
                    with open(expected_file, 'r', encoding='utf-8') as f:
                        self._current_file_entries = sum(1 for _ in f)

            # Rotate if file is too large
            if self._current_file_entries >= self.max_entries_per_file:
                # Add sequence number
                seq = 1
                while True:
                    seq_file = self.ledger_dir / f"ledger_{today}_{seq:03d}.jsonl"
                    if not seq_file.exists():
                        self._current_file = seq_file
                        self._current_file_entries = 0
                        break
                    seq += 1

            return self._current_file

    def _compute_hash(self, entry_dict: Dict) -> str:
        """Compute SHA-256 hash of an entry (excluding the entry_hash field)."""
        # Create a copy without the hash field, use the entry's previous_hash
        hashable = {k: v for k, v in entry_dict.items() if k != 'entry_hash'}
        # Ensure previous_hash is set from the entry dict (not runtime state)
        # This makes the hash deterministic and verifiable independently
        content = json.dumps(hashable, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def _create_entry(
        self,
        entry_type: AuditEntryType,
        **kwargs
    ) -> AuditEntry:
        """Create a new audit entry with hash chain."""
        entry_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{os.getpid()}"
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build entry dict for hashing - include ALL fields that will be serialized
        # including defaults that AuditEntry dataclass provides
        entry_dict = {
            'entry_id': entry_id,
            'entry_type': entry_type.value,
            'timestamp': timestamp,
            'previous_hash': self._last_hash,
            'violations': kwargs.get('violations', []),
            'verdict': kwargs.get('verdict'),
            'context': kwargs.get('context', {}),
            'source': kwargs.get('source', 'trading_bot'),
            'version': kwargs.get('version', 1),
            **{k: v for k, v in kwargs.items() if k not in ('violations', 'verdict', 'context', 'source', 'version')},
        }

        # Compute hash
        entry_hash = self._compute_hash(entry_dict)
        entry_dict['entry_hash'] = entry_hash

        # Create entry object with same values
        entry = AuditEntry(
            entry_id=entry_id,
            entry_type=entry_type,
            timestamp=timestamp,
            previous_hash=self._last_hash,
            entry_hash=entry_hash,
            violations=entry_dict['violations'],
            verdict=entry_dict['verdict'],
            context=entry_dict['context'],
            source=entry_dict['source'],
            version=entry_dict['version'],
            **{k: v for k, v in kwargs.items() if k not in ('violations', 'verdict', 'context', 'source', 'version')},
        )

        # Update chain
        self._last_hash = entry_hash

        return entry

    def log(self, entry: AuditEntry):
        """Write an entry to the ledger atomically (append)."""
        file_path = self._get_current_file()

        try:
            # Serialize entry
            entry_dict = asdict(entry)
            # Convert enums to values
            entry_dict['entry_type'] = entry.entry_type.value

            line = json.dumps(entry_dict, separators=(',', ':')) + '\n'

            # Atomic append: read existing content, append new line, write to tmp, replace
            tmp = file_path.with_suffix('.tmp')
            existing_content = ''
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_content = f.read()

            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(existing_content)
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(file_path)

            with self._lock:
                self._current_file_entries += 1

        except Exception as e:
            logger.error(f"Failed to write audit entry: {e}")
            raise

    def log_order_submitted(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        qty: int,
        price: float,
        order_id: str,
        context: Dict[str, Any],
    ):
        """Log an order submission."""
        entry = self._create_entry(
            entry_type=AuditEntryType.ORDER_SUBMITTED,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            qty=qty,
            price=price,
            allowed=True,
            context={
                'order_id': order_id,
                **context,
            },
        )
        self.log(entry)

    def log_order_rejected(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        qty: int,
        reason: str,
        violations: List[Dict],
        context: Dict[str, Any],
    ):
        """Log an order rejection."""
        entry = self._create_entry(
            entry_type=AuditEntryType.ORDER_REJECTED,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            qty=qty,
            allowed=False,
            violations=violations,
            context={
                'rejection_reason': reason,
                **context,
            },
        )
        self.log(entry)

    def log_trade_filled(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        qty: int,
        price: float,
        order_id: str,
        context: Dict[str, Any],
    ):
        """Log a trade fill."""
        entry = self._create_entry(
            entry_type=AuditEntryType.TRADE_FILLED,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            qty=qty,
            price=price,
            allowed=True,
            context={
                'order_id': order_id,
                **context,
            },
        )
        self.log(entry)

    def log_mandate_decision(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        allowed: bool,
        violations: List,
        context: Dict[str, Any],
    ):
        """Log a mandate gate decision."""
        violation_dicts = []
        for v in violations:
            if hasattr(v, 'violation_type'):
                violation_dicts.append({
                    'type': v.violation_type.value if hasattr(v.violation_type, 'value') else str(v.violation_type),
                    'message': v.message,
                    'details': v.details,
                    'timestamp': v.timestamp,
                })
            else:
                violation_dicts.append(v)

        # Convert context to dict if it's a dataclass or other non-dict object
        if hasattr(context, '__dataclass_fields__'):
            from dataclasses import asdict as dataclass_asdict
            context = dataclass_asdict(context)
        elif not isinstance(context, dict):
            context = vars(context) if hasattr(context, '__dict__') else str(context)

        # Convert any sets to lists for JSON serialization
        def _json_safe(obj):
            if isinstance(obj, set):
                return list(obj)
            if isinstance(obj, dict):
                return {k: _json_safe(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_json_safe(v) for v in obj]
            if isinstance(obj, tuple):
                return [_json_safe(v) for v in obj]
            return obj

        context = _json_safe(context)

        entry = self._create_entry(
            entry_type=AuditEntryType.MANDATE_DECISION,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            allowed=allowed,
            violations=violation_dicts,
            context=context,
        )
        self.log(entry)

    def log_kill_switch_event(self, event):
        """Log a kill switch state change."""
        from .kill_switch import KillSwitchEvent
        if isinstance(event, KillSwitchEvent):
            entry = self._create_entry(
                entry_type=AuditEntryType.KILL_SWITCH_EVENT,
                allowed=None,
                context={
                    'previous_state': event.previous_state.value,
                    'new_state': event.new_state.value,
                    'reason': event.reason,
                    'triggered_by': event.triggered_by,
                    'details': event.details,
                },
            )
        else:
            entry = self._create_entry(
                entry_type=AuditEntryType.KILL_SWITCH_EVENT,
                allowed=None,
                context=event if isinstance(event, dict) else {},
            )
        self.log(entry)

    def log_advisory_review(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        verdict: str,
        concerns: List[str],
        provider_id: str,
        context: Dict[str, Any],
    ):
        """Log an advisory review result."""
        entry = self._create_entry(
            entry_type=AuditEntryType.ADVISORY_REVIEW,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            allowed=verdict in ('APPROVE', 'APPROVE_WITH_CONCERNS'),
            verdict=verdict,
            context={
                'concerns': concerns,
                'provider_id': provider_id,
                **context,
            },
        )
        self.log(entry)

    def log_risk_event(
        self,
        event_type: str,
        message: str,
        symbol: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Log a risk management event (circuit breaker, drawdown, etc)."""
        entry = self._create_entry(
            entry_type=AuditEntryType.RISK_EVENT,
            symbol=symbol,
            allowed=None,
            context={
                'event_type': event_type,
                'message': message,
                **(context or {}),
            },
        )
        self.log(entry)

    def log_system_event(self, event_type: str, message: str, context: Optional[Dict] = None):
        """Log a system event (startup, shutdown, config change, etc)."""
        entry = self._create_entry(
            entry_type=AuditEntryType.SYSTEM_EVENT,
            allowed=None,
            context={
                'event_type': event_type,
                'message': message,
                **(context or {}),
            },
        )
        self.log(entry)

    def log_error(self, error: Exception, context: Optional[Dict] = None):
        """Log an error."""
        entry = self._create_entry(
            entry_type=AuditEntryType.ERROR,
            allowed=None,
            context={
                'error_type': type(error).__name__,
                'error_message': str(error),
                **(context or {}),
            },
        )
        self.log(entry)

    def get_entries_for_date(self, date: str) -> List[AuditEntry]:
        """Get all entries for a specific date (YYYY-MM-DD)."""
        entries = []
        # Check primary file
        primary_file = self.ledger_dir / f"ledger_{date}.jsonl"
        if primary_file.exists():
            entries.extend(self._read_file(primary_file))

        # Check sequence files
        for seq_file in sorted(self.ledger_dir.glob(f"ledger_{date}_*.jsonl")):
            entries.extend(self._read_file(seq_file))

        return entries

    def get_entries_for_symbol(self, symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[AuditEntry]:
        """Get all entries for a symbol within date range."""
        entries = []
        for file_path in sorted(self.ledger_dir.glob("ledger_*.jsonl")):
            # Quick date filter from filename
            try:
                filename = file_path.name
                if filename.startswith("ledger_"):
                    file_date = filename[7:17]  # ledger_YYYY-MM-DD
                    if start_date and file_date < start_date:
                        continue
                    if end_date and file_date > end_date:
                        continue
            except Exception:
                pass

            entries.extend(self._read_file_for_symbol(file_path, symbol))

        return entries

    def get_entries_by_type(
        self,
        entry_type: AuditEntryType,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[AuditEntry]:
        """Get all entries of a specific type within date range."""
        entries = []
        for file_path in sorted(self.ledger_dir.glob("ledger_*.jsonl")):
            try:
                filename = file_path.name
                if filename.startswith("ledger_"):
                    file_date = filename[7:17]
                    if start_date and file_date < start_date:
                        continue
                    if end_date and file_date > end_date:
                        continue
            except Exception:
                pass

            entries.extend(self._read_file_for_type(file_path, entry_type))

        return entries

    def _read_file(self, file_path: Path) -> List[AuditEntry]:
        """Read all entries from a ledger file."""
        entries = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = self._dict_to_entry(data)
                        entries.append(entry)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"Could not read audit file {file_path}: {e}")
        return entries

    def _read_file_for_symbol(self, file_path: Path, symbol: str) -> List[AuditEntry]:
        """Read entries for a specific symbol from a file."""
        entries = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get('symbol', '').upper() == symbol.upper():
                            entries.append(self._dict_to_entry(data))
                    except Exception:
                        continue
        except Exception:
            pass
        return entries

    def _read_file_for_type(self, file_path: Path, entry_type: AuditEntryType) -> List[AuditEntry]:
        """Read entries of a specific type from a file."""
        entries = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get('entry_type') == entry_type.value:
                            entries.append(self._dict_to_entry(data))
                    except Exception:
                        continue
        except Exception:
            pass
        return entries

    def _dict_to_entry(self, data: Dict) -> AuditEntry:
        """Convert dict to AuditEntry."""
        return AuditEntry(
            entry_id=data.get('entry_id', ''),
            entry_type=AuditEntryType(data.get('entry_type', 'system_event')),
            timestamp=data.get('timestamp', ''),
            previous_hash=data.get('previous_hash', ''),
            entry_hash=data.get('entry_hash', ''),
            symbol=data.get('symbol'),
            side=data.get('side'),
            notional_usd=data.get('notional_usd'),
            qty=data.get('qty'),
            price=data.get('price'),
            allowed=data.get('allowed'),
            violations=data.get('violations', []),
            verdict=data.get('verdict'),
            context=data.get('context', {}),
            source=data.get('source', 'trading_bot'),
            version=data.get('version', 1),
        )

    def verify_chain(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Verify the integrity of the hash chain.

        Returns dict with verification results.
        """
        results = {
            'valid': True,
            'entries_checked': 0,
            'first_hash': None,
            'last_hash': None,
            'broken_at': None,
            'errors': [],
        }

        previous_hash = "0" * 64
        first = True

        for file_path in sorted(self.ledger_dir.glob("ledger_*.jsonl")):
            try:
                filename = file_path.name
                if filename.startswith("ledger_"):
                    file_date = filename[7:17]
                    if start_date and file_date < start_date:
                        continue
                    if end_date and file_date > end_date:
                        continue
            except Exception:
                pass

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            entry_hash = data.get('entry_hash', '')
                            stored_prev_hash = data.get('previous_hash', '')

                            # Verify previous hash links correctly
                            if stored_prev_hash != previous_hash:
                                results['valid'] = False
                                results['broken_at'] = {
                                    'file': str(file_path),
                                    'line': line_num,
                                    'expected_prev_hash': previous_hash,
                                    'stored_prev_hash': stored_prev_hash,
                                }
                                results['errors'].append(
                                    f"Hash chain broken at {file_path}:{line_num}"
                                )
                                break

                            # Verify entry hash
                            hashable = {k: v for k, v in data.items() if k != 'entry_hash'}
                            hashable['previous_hash'] = previous_hash
                            computed_hash = hashlib.sha256(
                                json.dumps(hashable, sort_keys=True, separators=(',', ':')).encode('utf-8')
                            ).hexdigest()

                            if computed_hash != entry_hash:
                                results['valid'] = False
                                results['broken_at'] = {
                                    'file': str(file_path),
                                    'line': line_num,
                                    'computed_hash': computed_hash,
                                    'stored_hash': entry_hash,
                                }
                                results['errors'].append(
                                    f"Entry hash mismatch at {file_path}:{line_num}"
                                )
                                break

                            if first:
                                results['first_hash'] = entry_hash
                                first = False
                            results['last_hash'] = entry_hash
                            previous_hash = entry_hash
                            results['entries_checked'] += 1

                        except Exception as e:
                            results['errors'].append(f"Parse error at {file_path}:{line_num}: {e}")

            except Exception as e:
                results['errors'].append(f"File read error {file_path}: {e}")

            if not results['valid']:
                break

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get ledger statistics."""
        total_entries = 0
        total_files = 0
        total_size = 0
        date_range = {'earliest': None, 'latest': None}

        for file_path in sorted(self.ledger_dir.glob("ledger_*.jsonl")):
            total_files += 1
            total_size += file_path.stat().st_size
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    count = sum(1 for _ in f)
                    total_entries += count
            except Exception:
                pass

        return {
            'total_entries': total_entries,
            'total_files': total_files,
            'total_size_bytes': total_size,
            'ledger_dir': str(self.ledger_dir),
            'last_hash': self._last_hash,
        }
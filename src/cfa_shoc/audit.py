"""
Audit hash chain: one definition shared by durable storage and the in-memory ledger.

Each transaction has its own chain. Event `n` stores `prior_hash` = event `n-1`'s
`event_hash` (None for the first event), and `event_hash` = SHA-256 over the canonical JSON
of the event's content, which includes `prior_hash` and the per-transaction `sequence`.
Editing, deleting or reordering any stored event breaks the chain at that point.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _iso_utc(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no spaces, UTF-8 kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def event_content(
    *,
    event_id: Any,
    transaction_id: Any,
    sequence: int,
    event_type: str,
    from_state: Optional[str],
    to_state: Optional[str],
    actor_type: str,
    actor_id: str,
    payload: Any,
    created_at: Any,
    prior_hash: Optional[str],
) -> Dict[str, Any]:
    """The fields an event hash covers."""
    return {
        "event_id": str(event_id),
        "transaction_id": str(transaction_id),
        "sequence": int(sequence),
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "payload": payload,
        "created_at": _iso_utc(created_at),
        "prior_hash": prior_hash,
    }


def compute_event_hash(content: Dict[str, Any]) -> str:
    """SHA-256 hex of the canonical event content."""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()

"""
audit.py - tamper-evident, hash-chained audit trail.

Chain rule
    row_hash(n) = SHA-256( row_hash(n-1) + "\\n" + canonical_json(fields of row n) )
    row 1 chains from config.AUDIT_GENESIS_HASH.
    audit_head holds (last_id, last_hash) = the chain tip.

Protection layers
    1. No UPDATE/DELETE code path exists in the app.
    2. SQLite triggers ABORT any UPDATE/DELETE on audit_log (database.TRIGGERS).
    3. Hash chain makes edits, deletions and insertions DETECTABLE by verify_chain(),
       even if someone drops the triggers and edits app.db directly.
    4. backup.py records the chain tip in every backup manifest, so truncation is
       detectable even if the tip row is also altered.
Honest limit: someone with file-system access can still rewrite the whole chain consistently;
compare against a backup manifest / printed tip to catch that.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
from database import AuditHead, AuditLog, RuleViolation, read_scope, utcnow

log = config.get_logger("audit")

_REDACT_KEYS = {"password", "password_hash", "new_password", "current_password"}


class AuditError(RuleViolation):
    pass


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k in _REDACT_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def _dump(obj: Any) -> Optional[str]:
    return None if obj is None else canonical_json(_redact(obj))


def _hash_fields(r: AuditLog) -> dict:
    return {
        "id": r.id, "ts_utc": r.ts_utc, "user_id": r.user_id, "username": r.username,
        "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
        "old_value": r.old_value, "new_value": r.new_value, "reason": r.reason,
    }


def compute_row_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + "\n" + canonical_json(fields)).encode("utf-8")).hexdigest()


def log_event(session: Session, *, user, action: str, entity_type: str,
              entity_id: Any = None, old: Any = None, new: Any = None,
              reason: Optional[str] = None) -> AuditLog:
    """Append one event. MUST run inside database.write_scope(): that scope holds the write lock,
    so reading the chain tip and appending is atomic with the business change it describes.
    `user` is any object with .id/.username (SessionUser, User) or None for system events."""
    if not session.info.get("write_locked"):
        raise AuditError("log_event() must be called inside database.write_scope()")
    head = session.get(AuditHead, 1)
    if head is None:
        raise AuditError("audit_head missing: run `python database.py init`")
    new_id = head.last_id + 1
    row = AuditLog(
        id=new_id,
        ts_utc=utcnow().isoformat(timespec="microseconds") + "Z",
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None) or "SYSTEM",
        action=action, entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        old_value=_dump(old), new_value=_dump(new), reason=reason,
        prev_hash=head.last_hash,
    )
    row.row_hash = compute_row_hash(row.prev_hash, _hash_fields(row))
    session.add(row)
    head.last_id = new_id
    head.last_hash = row.row_hash
    return row


# ------------------------------------------------------------------ snapshot helpers
def model_snapshot(obj, exclude=("password_hash", "legacy_data")) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.key in exclude:
            continue
        v = getattr(obj, col.key)
        out[col.key] = v.isoformat() if isinstance(v, (datetime, date)) else v
    return out


def diff_snapshots(old: dict, new: dict) -> tuple[dict, dict]:
    """Only the fields that changed -> compact, readable audit entries."""
    keys = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
    return {k: old.get(k) for k in keys}, {k: new.get(k) for k in keys}


# ------------------------------------------------------------------ verification
@dataclass
class ChainReport:
    ok: bool
    checked: int
    first_bad_id: Optional[int]
    message: str


def verify_chain(batch_size: int = 2000) -> ChainReport:
    """Recompute every hash. Safe to run while users are writing."""
    with read_scope() as s:
        head = s.get(AuditHead, 1)
        if head is None:
            return ChainReport(False, 0, None, "audit_head row is missing")
        tip_id = head.last_id
        prev, expected, checked = config.AUDIT_GENESIS_HASH, 1, 0
        stmt = (select(AuditLog).where(AuditLog.id <= tip_id).order_by(AuditLog.id)
                .execution_options(yield_per=batch_size))
        for r in s.execute(stmt).scalars():
            if r.id != expected:
                return ChainReport(False, checked, expected, f"Gap in sequence: expected id {expected}, found {r.id} (row deleted?)")
            if r.prev_hash != prev:
                return ChainReport(False, checked, r.id, f"Row {r.id}: prev_hash does not match previous row")
            if compute_row_hash(r.prev_hash, _hash_fields(r)) != r.row_hash:
                return ChainReport(False, checked, r.id, f"Row {r.id}: content does not match its hash (edited?)")
            prev, expected, checked = r.row_hash, expected + 1, checked + 1
        if checked != tip_id:
            return ChainReport(False, checked, expected, f"Chain has {checked} rows but audit_head says {tip_id} (rows removed?)")
        tip_hash = s.execute(select(AuditHead.last_hash).where(AuditHead.id == 1)).scalar_one()
        max_id = s.execute(select(func.max(AuditLog.id))).scalar() or 0
        head2 = s.execute(select(AuditHead.last_id)).scalar_one()
        if max_id > head2:
            return ChainReport(False, checked, head2 + 1, "Rows exist beyond the chain tip (rows injected?)")
        if head2 == tip_id and tip_hash != prev:
            return ChainReport(False, checked, tip_id, "audit_head hash does not match last row")
    return ChainReport(True, checked, None, f"Audit chain intact: {checked} entries verified")


def get_entity_trail(entity_type: str, entity_id: Any, limit: int = 500) -> list[dict]:
    with read_scope() as s:
        rows = s.scalars(select(AuditLog)
                         .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == str(entity_id))
                         .order_by(AuditLog.id.desc()).limit(limit)).all()
        return [{"id": r.id, "ts_utc": r.ts_utc, "user": r.username, "action": r.action,
                 "old": r.old_value, "new": r.new_value, "reason": r.reason} for r in rows]

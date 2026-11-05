"""
database.py - SQLite engine (WAL), SQLAlchemy models, append-only triggers, init.

Session helpers
    read_scope()   plain session for reads; never holds the write lock.
    write_scope()  takes the SQLite write lock FIRST (so read-then-write can never hit
                   SQLITE_BUSY_SNAPSHOT), commits on success, rolls back on any error.
                   The audit chain also relies on this lock (see audit.py).

CLI:  python database.py init        (create/upgrade schema, seed, bootstrap admin)
      python database.py health
"""
from __future__ import annotations

import re
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        create_engine, event, inspect, select, text, update)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            relationship, sessionmaker, validates)

import config

log = config.get_logger("database")


class DatabaseBusy(Exception):
    """Raised when SQLite stays locked longer than the busy timeout."""


class RuleViolation(Exception):
    """Expected business-rule failures (bad password, permission denied, ...). The transaction is
    rolled back but NOT logged as a system error, so error.log only holds real faults."""


def utcnow() -> datetime:
    """Naive UTC. All timestamps are stored UTC; convert with to_ist() for display."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(config.IST)


# =========================================================================== engine
def _build_engine() -> Engine:
    config.ensure_dirs()
    for w in config.storage_warnings():
        log.warning(w)
    eng = create_engine(
        config.DATABASE_URL,
        connect_args={"timeout": config.SQLITE_BUSY_TIMEOUT_MS / 1000, "check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):  # runs for every new pooled connection
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")        # durable commits (small DB: cost is negligible)
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
        cur.close()

    return eng


engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


@contextmanager
def read_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    except Exception:
        s.rollback()
        log.exception("Database read failed")
        raise
    finally:
        s.close()


@contextmanager
def write_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        # No-op UPDATE that acquires the write lock immediately (waits up to busy_timeout).
        s.execute(text("UPDATE audit_head SET last_id = last_id WHERE id = 1"))
        s.info["write_locked"] = True
        yield s
        s.commit()
    except RuleViolation:
        s.rollback()
        raise
    except OperationalError as exc:
        s.rollback()
        log.exception("Database write failed (rolled back)")
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise DatabaseBusy("The database is busy. Please retry in a moment.") from exc
        raise
    except Exception:
        s.rollback()
        log.exception("Database write failed (rolled back)")
        raise
    finally:
        s.close()


# =========================================================================== helpers
_SUFFIX_TOKENS = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "plc", "gmbh", "pte", "sa", "bv", "ag", "opc",
}


def normalize_name(name: Optional[str]) -> str:
    """'A.B.C. Private Limited' and 'ABC Pvt Ltd' both -> 'abc'. Used for indexed + fuzzy matching."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    s = s.replace("&", " and ").replace(".", "").replace("'", "")
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def normalize_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = re.sub(r"\s+", "", str(value)).upper()
    return v or None


_ID_PATTERNS = {
    "pan": re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$"),
    "gstin": re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"),
    "cin": re.compile(r"^([LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}|[A-Z]{3}-[0-9]{4})$"),  # CIN or LLPIN
}


def validate_identifier(kind: str, value: Optional[str]) -> bool:
    """True if empty or well-formed. tax_id (foreign) is free text, so always True."""
    v = normalize_id(value)
    if not v or kind not in _ID_PATTERNS:
        return True
    return bool(_ID_PATTERNS[kind].match(v))


# =========================================================================== models
class Base(DeclarativeBase):
    pass


_ALLOWED = {
    "scope": config.SCOPES,
    "party_type": config.PARTY_TYPES,
    "conflict_status": config.CONFLICT_STATUSES,
    "decision": config.DECISIONS,
    "acceptance_status": config.ACCEPTANCE_STATUSES,
}


def _check_enum(key: str, value):
    if value is not None and value not in _ALLOWED[key]:
        raise ValueError(f"Invalid {key}: {value!r}. Allowed: {', '.join(_ALLOWED[key])}")
    return value


class ServiceLine(Base):
    __tablename__ = "service_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True)       # permanent: appears in reference numbers
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30))
    is_partner: Mapped[bool] = mapped_column(Boolean, default=False)   # eligible to be an "aligned partner"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @validates("role")
    def _v_role(self, _k, value):
        if value not in config.ROLES:
            raise ValueError(f"Invalid role: {value!r}")
        return value


class UserServiceLine(Base):
    """Which service lines a Service Line Member may see (other roles are unrestricted)."""
    __tablename__ = "user_service_lines"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    service_line_id: Mapped[int] = mapped_column(ForeignKey("service_lines.id"), primary_key=True)


class ConflictCheck(Base):
    __tablename__ = "conflict_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference_no: Mapped[str] = mapped_column(String(40), unique=True)
    service_line_id: Mapped[int] = mapped_column(ForeignKey("service_lines.id"))
    party_type: Mapped[str] = mapped_column(String(10), default="Client")
    scope: Mapped[str] = mapped_column(String(10))                    # Domestic / Global
    party_name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), default="")
    pan: Mapped[Optional[str]] = mapped_column(String(10))
    gstin: Mapped[Optional[str]] = mapped_column(String(15))
    cin: Mapped[Optional[str]] = mapped_column(String(21))            # CIN or LLPIN
    tax_id: Mapped[Optional[str]] = mapped_column(String(50))         # foreign / global registration ID
    country: Mapped[Optional[str]] = mapped_column(String(60))
    engagement_description: Mapped[Optional[str]] = mapped_column(Text)

    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conflict_status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    decision: Mapped[Optional[str]] = mapped_column(String(30))       # APPROVED / PARTNER_ALIGNMENT_NEEDED
    decided_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    decision_notes: Mapped[Optional[str]] = mapped_column(Text)

    aligned_partner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    aligned_partner_name: Mapped[Optional[str]] = mapped_column(String(120))  # legacy imports: name only
    alignment_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    alignment_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    alignment_notes: Mapped[Optional[str]] = mapped_column(Text)
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    acceptance_status: Mapped[str] = mapped_column(String(20), default="LOCKED")
    accepted_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    acceptance_notes: Mapped[Optional[str]] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(20), default="app")    # app / legacy_import
    legacy_file: Mapped[Optional[str]] = mapped_column(String(255))
    legacy_row: Mapped[Optional[int]] = mapped_column(Integer)
    legacy_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    legacy_data: Mapped[Optional[str]] = mapped_column(Text)          # full original row as JSON

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # soft delete only
    version: Mapped[int] = mapped_column(Integer, nullable=False)     # optimistic locking
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    related_parties: Mapped[list["RelatedParty"]] = relationship(back_populates="check")
    proofs: Mapped[list["ProofDocument"]] = relationship(back_populates="check")
    service_line: Mapped[ServiceLine] = relationship()

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        Index("ix_cc_reference", "reference_no"),
        Index("ix_cc_norm_name", "normalized_name"),
        Index("ix_cc_status", "conflict_status"),
        Index("ix_cc_scope", "scope"),
        Index("ix_cc_created", "created_at"),
        Index("ix_cc_sl_status", "service_line_id", "conflict_status"),
        Index("ix_cc_pan", "pan"), Index("ix_cc_gstin", "gstin"),
        Index("ix_cc_cin", "cin"), Index("ix_cc_tax_id", "tax_id"),
        Index("ix_cc_deleted", "is_deleted"),
    )

    @validates("scope", "party_type", "conflict_status", "decision", "acceptance_status")
    def _v_enums(self, key, value):
        return _check_enum(key, value)

    @validates("party_name")
    def _v_party_name(self, _k, value):
        self.normalized_name = normalize_name(value)
        return value.strip() if value else value

    @validates("pan", "gstin", "cin", "tax_id")
    def _v_ids(self, _k, value):
        return normalize_id(value)


class RelatedParty(Base):
    """Parent / subsidiaries / promoters / counterparties - where real conflicts usually hide."""
    __tablename__ = "related_parties"
    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("conflict_checks.id"))
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), default="")
    relationship_type: Mapped[Optional[str]] = mapped_column(String(60))
    identifier: Mapped[Optional[str]] = mapped_column(String(50))
    check: Mapped[ConflictCheck] = relationship(back_populates="related_parties")
    __table_args__ = (Index("ix_rp_check", "check_id"), Index("ix_rp_norm", "normalized_name"))

    @validates("name")
    def _v_name(self, _k, value):
        self.normalized_name = normalize_name(value)
        return value


class ProofDocument(Base):
    __tablename__ = "proof_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("conflict_checks.id"))
    doc_type: Mapped[str] = mapped_column(String(40))                 # KYC / Supporting / Approval Certificate / Alignment Proof / Legacy Proof Link
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))
    stored_relpath: Mapped[Optional[str]] = mapped_column(String(500))  # RELATIVE to config.PROOF_ROOT
    external_path: Mapped[Optional[str]] = mapped_column(String(1000))  # legacy links we could not copy
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    uploaded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    check: Mapped[ConflictCheck] = relationship(back_populates="proofs")
    __table_args__ = (Index("ix_proof_check", "check_id"),)


class ReferenceCounter(Base):
    __tablename__ = "reference_counters"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    sl_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(Base):
    """Append-only, hash-chained. Never touched except through audit.log_event()."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ts_utc: Mapped[str] = mapped_column(String(32))                   # ISO text: hashed exactly as stored
    user_id: Mapped[Optional[int]] = mapped_column(Integer)           # deliberately NOT a FK
    username: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[Optional[str]] = mapped_column(String(40))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_ts", "ts_utc"),
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_action", "action"),
    )


class AuditHead(Base):
    """Single row = chain tip. Also the lock target for write_scope(). Detects tail truncation."""
    __tablename__ = "audit_head"
    id: Mapped[int] = mapped_column(primary_key=True)
    last_id: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), default=config.AUDIT_GENESIS_HASH)


def next_reference_no(session: Session, sl_code: str, year: Optional[int] = None) -> str:
    """CC/<year>/<SL>/<0001>. Call inside write_scope(): the write lock makes it race-free."""
    y = year or datetime.now(config.IST).year
    session.execute(sqlite_insert(ReferenceCounter).values(year=y, sl_code=sl_code, last_value=0)
                    .on_conflict_do_nothing(index_elements=["year", "sl_code"]))
    session.execute(update(ReferenceCounter)
                    .where(ReferenceCounter.year == y, ReferenceCounter.sl_code == sl_code)
                    .values(last_value=ReferenceCounter.last_value + 1))
    n = session.execute(select(ReferenceCounter.last_value)
                        .where(ReferenceCounter.year == y, ReferenceCounter.sl_code == sl_code)).scalar_one()
    return f"CC/{y}/{sl_code}/{n:04d}"


# =========================================================================== triggers
TRIGGERS = {
    "trg_audit_log_no_update":
        "CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE blocked'); END;",
    "trg_audit_log_no_delete":
        "CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete BEFORE DELETE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: DELETE blocked'); END;",
    "trg_audit_head_no_delete":
        "CREATE TRIGGER IF NOT EXISTS trg_audit_head_no_delete BEFORE DELETE ON audit_head "
        "BEGIN SELECT RAISE(ABORT, 'audit_head cannot be deleted'); END;",
    "trg_audit_head_monotonic":
        "CREATE TRIGGER IF NOT EXISTS trg_audit_head_monotonic BEFORE UPDATE ON audit_head "
        "WHEN NEW.last_id < OLD.last_id "
        "BEGIN SELECT RAISE(ABORT, 'audit_head can only move forward'); END;",
    "trg_checks_no_delete":
        "CREATE TRIGGER IF NOT EXISTS trg_checks_no_delete BEFORE DELETE ON conflict_checks "
        "BEGIN SELECT RAISE(ABORT, 'conflict_checks rows are never deleted: use is_deleted'); END;",
    "trg_proofs_no_delete":
        "CREATE TRIGGER IF NOT EXISTS trg_proofs_no_delete BEFORE DELETE ON proof_documents "
        "BEGIN SELECT RAISE(ABORT, 'proof_documents rows are never deleted: use is_deleted'); END;",
}


def install_triggers(conn: Connection) -> None:
    """Idempotent. Call again at the end of ANY Alembic migration that rebuilds these tables."""
    for sql in TRIGGERS.values():
        conn.exec_driver_sql(sql)


def missing_triggers(conn: Connection) -> list[str]:
    have = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return [n for n in TRIGGERS if n not in have]


# =========================================================================== init / alembic
def _alembic_cfg():
    from alembic.config import Config
    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "alembic").replace("%", "%%"))
    return cfg


def _needs_upgrade() -> bool:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(_alembic_cfg()).get_current_head()
    with engine.connect() as c:
        return MigrationContext.configure(c).get_current_revision() != head


def init_db() -> None:
    """Fresh DB: create_all + triggers + stamp Alembic head.
    Existing DB: automatic backup, `alembic upgrade head`, re-assert triggers. Then seed."""
    from alembic import command
    config.ensure_dirs()
    fresh = not inspect(engine).has_table("audit_log")
    if fresh:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            install_triggers(conn)
        command.stamp(_alembic_cfg(), "head")
        log.info("Created new database at %s", config.DB_PATH)
    else:
        if _needs_upgrade():
            import backup
            backup.create_backup(label="pre-upgrade")
            command.upgrade(_alembic_cfg(), "head")
            log.info("Database upgraded to Alembic head")
        with engine.begin() as conn:
            install_triggers(conn)
    _seed()


def _seed() -> None:
    added: list[str] = []
    with SessionLocal() as s:
        if s.get(AuditHead, 1) is None:
            s.add(AuditHead(id=1, last_id=0, last_hash=config.AUDIT_GENESIS_HASH))
        if s.scalar(select(ServiceLine.id).limit(1)) is None:   # seed only into an empty table
            for code, name in config.DEFAULT_SERVICE_LINES:
                s.add(ServiceLine(code=code, name=name))
                added.append(code)
        s.commit()
    if added:
        import audit
        with write_scope() as s:
            audit.log_event(s, user=None, action="SYSTEM_SEED", entity_type="service_lines", new={"added": added})


def health_check() -> dict:
    """Used by the Part 2 Diagnostics page."""
    checks: dict[str, dict] = {}

    def rec(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    try:
        with engine.connect() as c:
            rec("integrity_check", c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok")
            mode = str(c.exec_driver_sql("PRAGMA journal_mode").scalar())
            rec("wal_mode", mode.lower() == "wal", mode)
            rec("foreign_keys", c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1)
            miss = missing_triggers(c)
            rec("append_only_triggers", not miss, ", ".join(miss))
        warns = config.storage_warnings()
        rec("storage_location", not warns, " | ".join(warns))
    except Exception as exc:  # never let diagnostics crash
        log.exception("health_check failed")
        rec("health_check_runtime", False, str(exc))
    return {"ok": all(v["ok"] for v in checks.values()), "checks": checks}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "help"
    if cmd == "init":
        init_db()
        import auth
        auth.bootstrap_admin()
        print("Database ready:", config.DB_PATH)
        return 0
    if cmd == "health":
        import json
        rep = health_check()
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 1
    print("Usage: python database.py [init|health]")
    return 2


if __name__ == "__main__":
    # Re-import under the proper module name so models/engine exist exactly once.
    import database as _db
    sys.exit(_db.main(sys.argv[1:]))

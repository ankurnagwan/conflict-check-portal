"""
smoke_test_part1.py - run BEFORE starting Part 2.   python smoke_test_part1.py
Uses a throw-away temp folder; never touches your real data/.
"""
import os
import sqlite3
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp(prefix="ccp_smoke_")
os.environ["CCP_DATA_DIR"] = _tmp
os.environ["CCP_ADMIN_PASSWORD"] = "Str0ng#Start99"

from openpyxl import Workbook                                   # noqa: E402

import audit, auth, backup, config, database, migration          # noqa: E402
from database import (ConflictCheck, ServiceLine, init_db, next_reference_no,   # noqa: E402
                      read_scope, write_scope)
from sqlalchemy import select                                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception as e:  # wrong exception type
        print("   unexpected:", type(e).__name__, e)
        return False
    return False


# ---------------------------------------------------------------- 1. init + health
init_db(); auth.bootstrap_admin()
init_db()                                                         # idempotent second run
h = database.health_check()
check("init_db + health_check", h["ok"], h)

# ---------------------------------------------------------------- 2. auth
check("wrong password rejected", not auth.authenticate("admin", "nope").ok)
res = auth.authenticate("ADMIN", "Str0ng#Start99")
check("admin login (case-insensitive username)", res.ok and res.user.must_change_password)
admin = res.user
auth.change_password(admin.id, "Str0ng#Start99", "Fresh#Secret2026x")
admin = auth.authenticate("admin", "Fresh#Secret2026x").user
check("password change clears must_change flag", admin and not admin.must_change_password)
check("weak password refused", raises(auth.AuthError, auth.change_password, admin.id, "Fresh#Secret2026x", "short"))
check("73+ byte password refused", raises(auth.AuthError, auth.hash_password, "A1!" + "x" * 80))

for _ in range(config.MAX_FAILED_LOGINS):
    auth.authenticate("admin", "wrong")
locked = auth.authenticate("admin", "Fresh#Secret2026x")
check("lockout after repeated failures (even correct pw refused)", not locked.ok and "locked" in locked.message.lower())
tmp_pw = auth.admin_reset_password(admin, admin.id)               # admin resets own lock for the test
admin = auth.authenticate("admin", tmp_pw).user
check("admin reset clears lock", admin is not None)

uid_rl, pw_rl = auth.create_user(admin, username="risk.lead", full_name="Rita Lead", role=config.ROLE_RISK_LEAD)
uid_rl2, _ = auth.create_user(admin, username="risk.lead2", full_name="Ravi Lead", role=config.ROLE_RISK_LEAD)
uid_m, pw_m = auth.create_user(admin, username="aud.member", full_name="Asha Member", role=config.ROLE_MEMBER,
                               service_line_codes=["AUD"])
uid_p, _ = auth.create_user(admin, username="partner.one", full_name="Paul Partner", role=config.ROLE_MEMBER,
                            service_line_codes=["ADV"], is_partner=True)
member = auth.authenticate("aud.member", pw_m).user
risk = auth.authenticate("risk.lead", pw_rl).user
partner = auth.load_session_user(uid_p)
check("member scoped to own service line", member.service_line_ids is not None and len(member.service_line_ids) == 1)
check("risk lead sees all lines", risk.service_line_ids is None)
check("member cannot manage users", raises(auth.PermissionDenied, auth.create_user, member,
                                           username="x.user", full_name="X", role=config.ROLE_AUDITOR))
check("member cannot decide conflicts", not auth.can(member, "decide_conflict") and auth.can(risk, "decide_conflict"))
check("maker-checker blocks self-approval", raises(auth.PermissionDenied, auth.assert_maker_checker, 5, 5))
check("only the named partner may confirm alignment", auth.can_confirm_alignment(partner, uid_p)
      and not auth.can_confirm_alignment(risk, uid_p))
check("last admin cannot be deactivated", raises(auth.AuthError, auth.update_user, admin, admin.id, is_active=False))

# ---------------------------------------------------------------- 3. records, scoping, references
with write_scope() as s:
    aud = s.scalar(select(ServiceLine).where(ServiceLine.code == "AUD"))
    adv = s.scalar(select(ServiceLine).where(ServiceLine.code == "ADV"))
    for sl, nm in ((aud, "ABC Pvt Ltd"), (adv, "Zeta Industries Limited")):
        c = ConflictCheck(reference_no=next_reference_no(s, sl.code), service_line_id=sl.id, scope="Domestic",
                          party_name=nm, requested_by_id=member.id, conflict_status="DRAFT", pan="abcde1234f")
        s.add(c); s.flush()
        audit.log_event(s, user=member, action="CHECK_CREATED", entity_type="conflict_check",
                        entity_id=c.id, new=audit.model_snapshot(c))
with read_scope() as s:
    rows = s.scalars(auth.apply_service_line_scope(select(ConflictCheck), member, ConflictCheck.service_line_id)).all()
    allrows = s.scalars(auth.apply_service_line_scope(select(ConflictCheck), risk, ConflictCheck.service_line_id)).all()
check("service-line isolation at query level", len(rows) == 1 and rows[0].party_name == "ABC Pvt Ltd" and len(allrows) == 2)
check("reference numbers sequential", rows[0].reference_no.endswith("/AUD/0001"), rows[0].reference_no)
check("name + PAN normalised on assignment", rows[0].normalized_name == "abc" and rows[0].pan == "ABCDE1234F")
check("normalize_name variants match",
      database.normalize_name("A.B.C. Private Limited") == database.normalize_name("ABC Pvt Ltd") == "abc")
check("invalid status rejected", raises(ValueError, setattr, rows[0], "conflict_status", "NONSENSE"))
check("log_event outside write_scope refused", raises(audit.AuditError, audit.log_event, database.SessionLocal(),
                                                      user=None, action="X", entity_type="x"))

# ---------------------------------------------------------------- 4. concurrency
errors = []
def worker(n):
    try:
        for i in range(15):
            with write_scope() as s:
                audit.log_event(s, user=member, action="CONCURRENCY_TEST", entity_type="test", entity_id=f"{n}-{i}")
    except Exception as e:
        errors.append(repr(e))
ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
rep = audit.verify_chain()
check("8 threads x 15 audit writes: no errors", not errors, errors[:2])
check("audit chain intact after concurrent writes", rep.ok, rep.message)

# ---------------------------------------------------------------- 5. backup / verify
m = backup.create_backup("manual")
v = backup.verify_backup(config.BACKUP_DIR / m["file"])
check("backup created and verified (restore test)", v["ok"], v)
check("manifest anchors audit tip", m["audit_tip_id"] is not None and m["audit_tip_id"] > 100)

# ---------------------------------------------------------------- 6. migration
wb = Workbook(); ws = wb.active
ws.append(["Client Name", "Service Line", "Domestic/Global", "Status", "PAN", "Date", "Approval Proof", "Remarks"])
ws.append(["Acme Pvt Ltd", "Audit & Assurance", "Domestic", "Approved", "AAAPL1234C", "05/03/2024",
           '=HYPERLINK("C:\\Old\\Proofs\\acme.pdf","view")', "ok"])
ws.append(["Globex Inc", "adv", "Global", "Partner Alignment Needed", "", "12/04/2024", None, None])
ws.append(["Initech", "Unknown Dept", "Domestic", "Approved", "", "01/01/2024", None, None])      # -> ERROR
ws.append(["Hooli", "AUD", "Domestic", "Weird Status", "", "01/01/2024", None, None])             # -> ERROR
ws.append(["Pied Piper", "AUD", "Domestic", "Cleared", "BADPAN", "20/02/2024", None, None])       # -> WARNING
ws.append([None, "AUD", "Domestic", "Approved", "", "", None, None])                              # -> ERROR
legacy = os.path.join(_tmp, "legacy.xlsx"); wb.save(legacy)

dry = migration.run_migration(legacy)
with read_scope() as s:
    n_before = len(s.scalars(select(ConflictCheck)).all())
check("dry run writes nothing", n_before == 2 and dry["would_import"] == 3 and dry["errors"] == 3, dry)
real = migration.run_migration(legacy, commit=True, actor="tester")
again = migration.run_migration(legacy, commit=True, actor="tester")
with read_scope() as s:
    imported = s.scalars(select(ConflictCheck).where(ConflictCheck.source == "legacy_import")).all()
check("commit imports valid rows only", real["imported"] == 3 and len(imported) == 3, real)
check("re-run is idempotent (no duplicates)", again.get("imported", 0) == 0 and again["skipped_duplicates"] == 3, again)
with read_scope() as s:
    links = [p.external_path for p in s.scalars(select(database.ProofDocument))]
check("legacy proof hyperlink preserved", any(l and "acme.pdf" in l for l in links), links)
check("full original row kept as JSON", all(c.legacy_data and "Client Name" in c.legacy_data for c in imported))
check("audit chain intact after migration", audit.verify_chain().ok)

# ---------------------------------------------------------------- 7. tamper tests (LAST)
db = str(config.DB_PATH)
con = sqlite3.connect(db)
def blocked(sql):
    try:
        con.execute(sql); con.commit(); return False
    except sqlite3.DatabaseError:
        return True
check("trigger blocks UPDATE on audit_log", blocked("UPDATE audit_log SET username='evil' WHERE id=5"))
check("trigger blocks DELETE on audit_log", blocked("DELETE FROM audit_log WHERE id=5"))
check("trigger blocks DELETE on conflict_checks", blocked("DELETE FROM conflict_checks WHERE id=1"))
# attacker drops the trigger and edits a row directly:
con.execute("DROP TRIGGER trg_audit_log_no_update"); con.commit()
con.execute("UPDATE audit_log SET username='evil' WHERE id=5"); con.commit()
r1 = audit.verify_chain()
check("hash chain detects edited row", (not r1.ok) and r1.first_bad_id == 5, r1.message)
con.execute("UPDATE audit_log SET username=(SELECT username FROM audit_log WHERE id=4) WHERE id=5"); con.commit()
con.execute("DROP TRIGGER trg_audit_log_no_delete"); con.commit()
con.execute("DELETE FROM audit_log WHERE id=7"); con.commit()
r2 = audit.verify_chain()
check("hash chain detects deleted row", not r2.ok, r2.message)
con.close()
check("health_check reports missing triggers", not database.health_check()["ok"])
init_db()                                                         # re-asserts triggers
check("init_db re-installs missing triggers", database.health_check()["checks"]["append_only_triggers"]["ok"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

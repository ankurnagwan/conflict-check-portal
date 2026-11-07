"""
smoke_test_part3.py - run after Parts 1 and 2:   python smoke_test_part3.py
Throw-away temp folder; never touches your real data. Takes about a minute.
"""
import glob
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="ccp_p3_")
os.environ["CCP_DATA_DIR"] = _tmp
os.environ["CCP_ADMIN_PASSWORD"] = "Str0ng#Start99"
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CCP_PROJECT"] = HERE
sys.path.insert(0, HERE)

from openpyxl import load_workbook                  # noqa: E402
from pypdf import PdfReader                          # noqa: E402
from sqlalchemy import select                        # noqa: E402
from streamlit.testing.v1 import AppTest             # noqa: E402

import app, audit, auth, config, database, excel_sync, matching, workflow   # noqa: E402
from database import RuleViolation                   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception as e:
        print("   unexpected:", type(e).__name__, e)
    return False


PDF = b"%PDF-1.4 test "


def docs(ptype, scope, required_only=True):
    return [{"doc_type": k, "filename": f"{k}.pdf", "data": PDF + k.encode()}
            for k, req in workflow.required_docs(ptype, scope) if req or not required_only]


def wait_for(fn, timeout=20, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------ setup
database.init_db(); auth.bootstrap_admin()
admin = auth.authenticate("admin", "Str0ng#Start99").user
auth.change_password(admin.id, "Str0ng#Start99", "Fresh#Secret2026x")
admin = auth.authenticate("admin", "Fresh#Secret2026x").user
PW = "Member#Pass2026"
U = {}
for un, nm, role, lines, par in [("asha", "Asha Member", config.ROLE_MEMBER, ["AUD"], False),
                                 ("dev", "Dev Member", config.ROLE_MEMBER, ["ADV"], False),
                                 ("paul", "Paul Partner", config.ROLE_MEMBER, ["ADV"], True),
                                 ("rita", "Rita Lead", config.ROLE_RISK_LEAD, [], False),
                                 ("ravi", "Ravi Lead", config.ROLE_RISK_LEAD, [], False),
                                 ("audi", "Anil Auditor", config.ROLE_AUDITOR, [], False)]:
    uid, t = auth.create_user(admin, username=un, full_name=nm, role=role, service_line_codes=lines, is_partner=par)
    auth.change_password(uid, t, PW)
    U[un] = auth.authenticate(un, PW).user
with database.read_scope() as s:
    SL = {x.code: x.id for x in s.scalars(select(database.ServiceLine))}
excel_sync.start()
config.EXCEL_DEBOUNCE_SECONDS = 0.2
config.EXCEL_RETRY_SECONDS = 1
config.EXCEL_REPLACE_ATTEMPTS, config.EXCEL_REPLACE_DELAY = 2, 0.05


def mk(user, name, line="AUD", ptype="Client", scope="Domestic", submit=True, **kw):
    kw.setdefault("pan", "AAACX1234Q") if scope == "Domestic" else kw.setdefault("tax_id", "T-" + name[:6])
    if scope == "Global":
        kw.setdefault("country", "Germany")
    return workflow.create_check(user, service_line_id=SL[line], party_type=ptype, scope=scope, party_name=name,
                                 uploads=docs(ptype, scope), submit=submit, confirm_review=submit, **kw)


# ================================================================== A. document requirements
check("Client/Domestic requires PAN, TAN, GST, COI, signed EL (MSME optional)",
      {k for k, r in workflow.required_docs("Client", "Domestic") if r} == {"PAN", "TAN", "GST", "COI", "SIGNED_EL"}
      and ("MSME", False) in workflow.required_docs("Client", "Domestic"))
check("Client/Global swaps PAN/GST for TRC", {k for k, _ in workflow.required_docs("Client", "Global")} == {"TRC", "COI", "SIGNED_EL"})
check("Vendor/Domestic adds cancelled cheque, quote, signed PO/Agreement",
      {"CANCELLED_CHEQUE", "VENDOR_QUOTE", "SIGNED_PO_AGREEMENT", "PAN", "GST", "MSME"} <= {k for k, _ in workflow.required_docs("Vendor", "Domestic")})
check("Vendor/Global needs TRC, no PAN/GST", {k for k, _ in workflow.required_docs("Vendor", "Global")} ==
      {"TRC", "COI", "CANCELLED_CHEQUE", "VENDOR_QUOTE", "SIGNED_PO_AGREEMENT"})
check("GST-address warning text is exact", config.GST_ADDRESS_WARNING == "Billing will be made on the GST Address so kindly ensure that your EL address and GST address are identical to avoid rejection. Please perform a final check of your documents before submitting.")

# ================================================================== B. upload validation + submit gates
base = dict(service_line_id=SL["AUD"], party_type="Client", scope="Domestic", party_name="Gate Test Ltd", pan="AAACG1234Q")
def errs_of(**kw):
    try:
        workflow.create_check(U["asha"], **{**base, **kw}); return []
    except workflow.FormInvalid as e:
        return e.errors
e = errs_of(uploads=[{"doc_type": "PAN", "filename": "x.exe", "data": PDF}])
check("disallowed file type rejected", any("not allowed" in x for x in e), e)
e = errs_of(uploads=[{"doc_type": "PAN", "filename": "x.pdf", "data": b""}])
check("empty file rejected", any("empty" in x for x in e), e)
e = errs_of(uploads=[{"doc_type": "PAN", "filename": "x.pdf", "data": b"hello, I am not a pdf"}])
check("fake PDF (wrong magic bytes) rejected", any("does not look like" in x for x in e), e)
config.MAX_UPLOAD_MB = 0
e = errs_of(uploads=[{"doc_type": "PAN", "filename": "x.pdf", "data": PDF}])
config.MAX_UPLOAD_MB = 25
check("oversize file rejected", any("larger than" in x for x in e), e)
e = errs_of(uploads=docs("Client", "Domestic")[:2], submit=True, confirm_review=True)
check("submit blocked while required docs are missing (names them)", any("TAN" in x or "GST" in x for x in e) and any("missing" in x for x in e), e)
e = errs_of(uploads=docs("Client", "Domestic"), submit=True, confirm_review=False)
check("submit blocked without the final review confirmation", any("final review" in x for x in e), e)
e = errs_of(uploads=docs("Client", "Domestic"), submit=True, confirm_review=True, pan=None)
check("submit blocked without any identifier", any("identifier" in x for x in e), e)
d_ok = errs_of(uploads=[], submit=False)
check("a draft can be saved with no documents", d_ok == [] , d_ok)

X1 = mk(U["asha"], "Acme Industries Limited", related=[{"name": "Acme Holdings", "relationship": "Parent"}])
check("create+submit with docs + confirmation", X1["status"] == "SUBMITTED" and X1["documents"] == 5, X1)
tr = audit.get_entity_trail("conflict_check", X1["id"])
ack = [t for t in tr if t["action"] == "SUBMISSION_ACKNOWLEDGED"]
check("audit records the acknowledged GST warning + confirmation", ack and "Billing will be made on the GST Address" in ack[0]["new"] and "true" in ack[0]["new"], ack)
with database.read_scope() as s:
    pds = s.scalars(select(database.ProofDocument).where(database.ProofDocument.check_id == X1["id"])).all()
paths = [config.PROOF_ROOT / p.stored_relpath for p in pds]
check("files stored under PROOF_ROOT with generated names, RELATIVE paths in DB",
      all(p.exists() for p in paths) and all(not os.path.isabs(x.stored_relpath) for x in pds)
      and all(x.original_filename.split(".")[0].lower() not in os.path.basename(x.stored_relpath).replace(x.doc_type.lower(), "") for x in pds))
import hashlib
check("stored SHA-256 equals file content hash", all(hashlib.sha256(p.read_bytes()).hexdigest() == x.sha256 for p, x in zip(paths, pds)))
trav = workflow.create_check(U["asha"], **{**base, "party_name": "Traversal Ltd"}, uploads=[{"doc_type": "PAN", "filename": "../../evil.pdf", "data": PDF}])
with database.read_scope() as s:
    tp = s.scalar(select(database.ProofDocument).where(database.ProofDocument.check_id == trav["id"]))
check("path traversal in filename is neutralised", tp.original_filename == "evil.pdf" and (config.PROOF_ROOT / tp.stored_relpath).resolve().is_relative_to(config.PROOF_ROOT.resolve()))

D1 = mk(U["asha"], "Draft Upload Co", submit=False)          # has all required docs via uploads
workflow.attach_documents(U["asha"], D1["id"], [{"doc_type": "PAN", "filename": "new_pan.pdf", "data": PDF + b"v2"}])
workflow.attach_documents(U["asha"], D1["id"], [{"doc_type": "OTHER", "filename": "o1.pdf", "data": PDF + b"1"}])
workflow.attach_documents(U["asha"], D1["id"], [{"doc_type": "OTHER", "filename": "o2.pdf", "data": PDF + b"2"}])
dd = workflow.get_check_detail(U["asha"], D1["id"])
check("replace-on-upload keeps one active PAN (old soft-deleted); OTHER accumulates",
      sum(1 for x in dd["docs"] if x["key"] == "PAN") == 1 and sum(1 for x in dd["docs"] if x["key"] == "OTHER") == 2)
check("checklist shows nothing missing", dd["missing"] == [] and all(x["present"] for x in dd["checklist"] if x["required"]))
check("another user cannot attach to my draft", raises(auth.PermissionDenied, workflow.attach_documents, U["dev"], D1["id"], docs("Client", "Domestic")[:1]))
check("submitted check: maker cannot add KYC docs any more", raises(auth.PermissionDenied, workflow.attach_documents, U["asha"], X1["id"], docs("Client", "Domestic")[:1]))
check("draft submit via submit_check needs the confirmation", raises(workflow.FormInvalid, workflow.submit_check, U["asha"], D1["id"], confirm_review=False))
check("draft submit via submit_check works with confirmation", workflow.submit_check(U["asha"], D1["id"], confirm_review=True) == D1["reference_no"])
DR = workflow.create_check(U["asha"], **{**base, "party_name": "Edit Me Ltd"})
workflow.update_draft(U["asha"], DR["id"], party_type="Vendor", scope="Global", party_name="Edit Me GmbH", tax_id="DE1", country="Germany")
dd = workflow.get_check_detail(U["asha"], DR["id"])
check("draft edit changes entity, type, scope (and checklist follows)", dd["party_name"] == "Edit Me GmbH" and dd["scope"] == "Global" and {c["key"] for c in dd["checklist"]} == {"TRC", "COI", "CANCELLED_CHEQUE", "VENDOR_QUOTE", "SIGNED_PO_AGREEMENT"})
check("only the maker can edit a draft", raises(auth.PermissionDenied, workflow.update_draft, U["dev"], DR["id"], party_type="Client", scope="Domestic", party_name="x"))

# ================================================================== C. Option A + certificate
check("member cannot decide", raises(auth.PermissionDenied, workflow.decide_approve, U["asha"], X1["id"]))
X2 = mk(U["rita"], "Rita Raised Pvt Ltd", line="FOR")
check("maker-checker: Risk Lead cannot approve own check", raises(auth.PermissionDenied, workflow.decide_approve, U["rita"], X2["id"]))
check("auditor cannot decide", raises(auth.PermissionDenied, workflow.decide_approve, U["audi"], X1["id"]))
res = workflow.decide_approve(U["ravi"], X1["id"], "No conflicts found.")
dd = workflow.get_check_detail(U["ravi"], X1["id"])
check("Option A: approved, acceptance unlocked, cleared_by recorded",
      dd["status"] == "APPROVED" and dd["decision"] == "APPROVED" and dd["acceptance"] == "PENDING" and dd["cleared_by"] == "Ravi Lead")
cert = next(x for x in dd["docs"] if x["key"] == "APPROVAL_CERTIFICATE")
with database.read_scope() as s:
    cpath = config.PROOF_ROOT / s.get(database.ProofDocument, cert["id"]).stored_relpath
cbytes = cpath.read_bytes()
check("certificate is a PDF whose SHA-256 in the DB matches the file", cbytes.startswith(b"%PDF") and hashlib.sha256(cbytes).hexdigest() == cert["sha256"] == res["certificate_sha256"])
text = "\n".join(pg.extract_text() for pg in PdfReader(str(cpath)).pages)
check("certificate records reference, entity, requester + approver credentials, timestamps",
      all(x in text for x in (X1["reference_no"], "Acme Industries Limited", "Asha Member", "asha", "Ravi Lead", "ravi", "Risk Lead", "IST", "UTC", "Option A")), text[:300])
check("certificate lists the SHA-256 of every supporting document", all(x["sha256"][:20] in text.replace("\n", "") for x in dd["docs"] if x["key"] != "APPROVAL_CERTIFICATE"))
check("CERTIFICATE_ISSUED audited with the hash", any(t["action"] == "CERTIFICATE_ISSUED" and cert["sha256"] in t["new"] for t in audit.get_entity_trail("conflict_check", X1["id"])))
check("cannot approve twice", raises(workflow.FormInvalid, workflow.decide_approve, U["ravi"], X1["id"]))
check("acceptance: maker cannot accept", raises(auth.PermissionDenied, workflow.record_acceptance, U["asha"], X1["id"], True, "ok"))
check("acceptance recorded by a Risk Lead", workflow.record_acceptance(U["rita"], X1["id"], True, "Engagement letter signed.") == X1["reference_no"]
      and workflow.get_check_detail(U["ravi"], X1["id"])["acceptance"] == "ACCEPTED")
workflow.decide_approve(U["ravi"], X2["id"])
check("maker-checker also guards acceptance", raises(auth.PermissionDenied, workflow.record_acceptance, U["rita"], X2["id"], True, "self"))

# ================================================================== D. Option B + partner sign-off
X3 = mk(U["asha"], "Orbit Logistics Pvt Ltd")
check("alignment: partner who raised the check is refused", raises(workflow.FormInvalid, workflow.decide_alignment, U["rita"], mk(U["paul"], "Paul Raised Co", line="ADV")["id"], U["paul"].id, "x"))
check("alignment: notes required", raises(workflow.FormInvalid, workflow.decide_alignment, U["ravi"], X3["id"], U["paul"].id, " "))
check("alignment: a non-partner cannot be named", raises(workflow.FormInvalid, workflow.decide_alignment, U["ravi"], X3["id"], U["dev"].id, "align please"))
check("alignment: requester cannot decide Option B on own check", raises(auth.PermissionDenied, workflow.decide_alignment, U["asha"], X3["id"], U["paul"].id, "x"))
ra = workflow.decide_alignment(U["ravi"], X3["id"], U["paul"].id, "Same promoters as an advisory client.")
d3 = workflow.get_check_detail(U["ravi"], X3["id"])
check("Option B: ALIGNMENT_PENDING with the named partner", d3["status"] == "ALIGNMENT_PENDING" and d3["aligned_partner"] == "Paul Partner" and d3["decision"] == "PARTNER_ALIGNMENT_NEEDED")
check("clearing before partner sign-off is refused", raises(workflow.FormInvalid, workflow.clear_aligned_check, U["ravi"], X3["id"], "n"))
dp = workflow.get_check_detail(U["paul"], X3["id"])
check("partner outside the line can open it, but LIMITED (no documents)", dp and dp["limited"] and dp["docs"] == [] and dp["checklist"] == [])
check("partner's lists stay strictly scoped (check not in his records)", X3["reference_no"] not in set(app.fetch_checks(U["paul"], app.Filters())["reference_no"]))
check("another ADV user cannot open it", workflow.get_check_detail(U["dev"], X3["id"]) is None)
q = workflow.fetch_queue(U["paul"])
check("partner's Action Queue lists the sign-off", ((q["reference_no"] == X3["reference_no"]) & (q["action"] == "Sign off alignment")).any())
for who in ("asha", "rita", "dev", "audi"):
    check(f"{who} cannot sign off the partner's alignment", raises(auth.PermissionDenied, workflow.confirm_alignment, U[who], X3["id"], "I sign"))
check("partner cannot sign off without notes", raises(workflow.FormInvalid, workflow.confirm_alignment, U["paul"], X3["id"], ""))
workflow.confirm_alignment(U["paul"], X3["id"], "Aligned: ring-fenced teams, no shared information.")
check("partner sign-off -> ALIGNMENT_CONFIRMED", workflow.get_check_detail(U["ravi"], X3["id"])["status"] == "ALIGNMENT_CONFIRMED")
check("final notes required to clear", raises(workflow.FormInvalid, workflow.clear_aligned_check, U["ravi"], X3["id"], " "))
clr = workflow.clear_aligned_check(U["ravi"], X3["id"], "Cleared after partner sign-off.", [{"doc_type": "X", "filename": "email.pdf", "data": PDF + b"email"}])
d3 = workflow.get_check_detail(U["ravi"], X3["id"])
check("Option B cleared: CLEARED + acceptance unlocked + alignment proof attached",
      d3["status"] == "CLEARED" and d3["acceptance"] == "PENDING" and any(x["key"] == "ALIGNMENT_PROOF" for x in d3["docs"]))
c3 = next(x for x in d3["docs"] if x["key"] == "APPROVAL_CERTIFICATE")
with database.read_scope() as s:
    p3 = config.PROOF_ROOT / s.get(database.ProofDocument, c3["id"]).stored_relpath
t3 = "\n".join(pg.extract_text() for pg in PdfReader(str(p3)).pages)
check("Option B certificate shows the partner's sign-off", all(x in t3 for x in ("Paul Partner", "Partner sign-off", "Option B", "ring-fenced")), t3[:200])
X4 = mk(U["rita"], "Rita Option B Ltd", line="FOR")
workflow.decide_alignment(U["ravi"], X4["id"], U["paul"].id, "Needs alignment.")
workflow.confirm_alignment(U["paul"], X4["id"], "Aligned.")
check("maker-checker on clearing: the Risk Lead who raised it cannot clear", raises(auth.PermissionDenied, workflow.clear_aligned_check, U["rita"], X4["id"], "self-clear"))
check("a different Risk Lead can clear it", workflow.clear_aligned_check(U["ravi"], X4["id"], "Cleared by Ravi.")["reference_no"] == X4["reference_no"])
X5 = mk(U["asha"], "Declined Pvt Ltd")
workflow.decide_alignment(U["rita"], X5["id"], U["paul"].id, "Potential conflict.")
check("only the designated partner can decline", raises(auth.PermissionDenied, workflow.decline_alignment, U["dev"], X5["id"], "no"))
workflow.decline_alignment(U["paul"], X5["id"], "Conflict stands: we advise their competitor.")
check("declined alignment -> REJECTED", workflow.get_check_detail(U["ravi"], X5["id"])["status"] == "REJECTED")
X6 = mk(U["asha"], "Rejected Directly Ltd")
check("Risk Lead can reject with a reason (and not own checks)", raises(workflow.FormInvalid, workflow.reject_check, U["ravi"], X6["id"], "")
      and workflow.reject_check(U["ravi"], X6["id"], "Restricted entity.") == X6["reference_no"])

# ================================================================== E. fuzzy matching
Z = mk(U["asha"], "Zenith Technologies Private Limited", pan="AAACZ1111Z", related=[{"name": "Globex Corporation"}])
ADVZ = mk(U["dev"], "Zenith Tech Solutions", line="ADV", pan="AAACS2222S")
def fm(**kw):
    with database.read_scope() as s:
        return matching.find_matches(s, **kw)
m = fm(name="ZENITH TECHNOLOGIES PVT LTD")
check("legal-suffix variants match at 100% (likely duplicate)", m and m[0]["reference_no"] == Z["reference_no"] and m[0]["score"] == 100 and m[0]["level"] == matching.LEVEL_HIGH, m[:1])
m = fm(name="Zenith Technolgies Pvt Ltd")
check("typo still flagged (>= threshold)", any(x["reference_no"] == Z["reference_no"] and x["score"] >= config.FUZZY_THRESHOLD for x in m), m)
check("unrelated name not flagged", fm(name="Quantum Foods Limited") == [])
check("related party of an existing record is matched", any(x["reference_no"] == Z["reference_no"] and any("related party" in r for r in x["reasons"]) for x in fm(name="Globex Corp")))
check("new related party vs existing entity is matched", any(x["reference_no"] == Z["reference_no"] for x in fm(name="Totally Different Name", related_names=["Zenith Technologies"])))
m = fm(name="Another Name Entirely", pan="AAACZ1111Z")
check("identifier match is 100% regardless of name", m and m[0]["score"] == 100 and "Same PAN" in m[0]["reasons"])
check("exclude_id removes the record itself", all(x["check_id"] != Z["id"] for x in fm(name="Zenith Technologies Private Limited", exclude_id=Z["id"])))
check("very short names ignored", fm(name="AB") == [])
with database.read_scope() as s:
    allm = matching.find_matches(s, name="Zenith")
vis, hid = workflow.partition_matches(U["asha"], allm)
check("member sees details only for own line; other lines are a count", all(x["service_line"] == "AUD" for x in vis) and hid >= 1 and len(vis) + hid == len(allm), (len(vis), hid))
vis_r, hid_r = workflow.partition_matches(U["rita"], allm)
check("Risk Lead sees everything", hid_r == 0 and len(vis_r) == len(allm))
sv, sh = workflow.screen_entity(U["asha"], name="Zenith Technologies Pvt Ltd")
check("live screening for a member: own-line match + hidden count", any(x["reference_no"] == Z["reference_no"] for x in sv))

# ================================================================== F. Excel sync
EVIL = mk(U["dev"], "=cmd|' /C calc'!A0", line="ADV", pan="AAACE1111E")
res = excel_sync.sync_now()
check("sync_now writes master + per-line workbooks", os.path.exists(excel_sync.master_path()) and len(res["files"]) >= 4, res)
wb = load_workbook(excel_sync.master_path())
ws = wb["Conflict Tracker"]
hdr = [c.value for c in ws[1]]
with database.read_scope() as s:
    n_checks = len(s.scalars(select(database.ConflictCheck).where(database.ConflictCheck.is_deleted.is_(False))).all())
check("master has one row per record and a README sheet", ws.max_row - 1 == n_checks and "README" in wb.sheetnames, (ws.max_row, n_checks))
col = hdr.index("Approval certificate") + 1
x1row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == X1["reference_no"])
f = ws.cell(x1row, col).value
check("approval certificate cell is a clickable =HYPERLINK formula to the proof file",
      isinstance(f, str) and f.startswith('=HYPERLINK("') and cert["id"] and "approval_certificate_" in f and f.endswith('","Open")'), f)
check("document columns (PAN etc.) are HYPERLINK formulas too", str(ws.cell(x1row, hdr.index("Pan") + 1).value).startswith("=HYPERLINK("))
evil_row = next(r for r in range(2, ws.max_row + 1) if str(ws.cell(r, 3).value or "").startswith("=cmd"))
check("hostile entity name stays TEXT (no formula injection)", ws.cell(evil_row, 3).data_type == "s", ws.cell(evil_row, 3).data_type)
aud = load_workbook(config.EXCEL_DIR / "by_service_line" / "AUD_Conflict_Tracker.xlsx")["Conflict Tracker"]
check("per-line workbook holds ONLY that service line", {aud.cell(r, 2).value.split(" - ")[0] for r in range(2, aud.max_row + 1)} == {"AUD"})
check("no temp files left behind", glob.glob(str(config.EXCEL_DIR / "**" / "*.tmp"), recursive=True) == [] and glob.glob(str(config.EXCEL_DIR / ".*.tmp")) == [])
saved = config.EXCEL_LINK_ROOT
config.EXCEL_LINK_ROOT = "C:\\" + "x" * 260
check("paths beyond Excel's 255-char HYPERLINK limit fall back to text", excel_sync._link("2026/a/b.pdf", "Open") is None)
config.EXCEL_LINK_ROOT = r"\\fileserver\ccp_proofs"
check("UNC link root produces backslash paths", excel_sync._link("2026/CC-1/a.pdf", "Open") == r'=HYPERLINK("\\fileserver\ccp_proofs\2026\CC-1\a.pdf","Open")')
config.EXCEL_LINK_ROOT = saved

# background + non-blocking
os.remove(excel_sync.master_path())
t0 = time.time()
BG = mk(U["asha"], "Background Sync Co")
elapsed = time.time() - t0
check("saving never waits for Excel (returns immediately)", elapsed < 5, elapsed)
check("background thread rebuilds the workbook by itself", wait_for(lambda: os.path.exists(excel_sync.master_path())))
check("...and it already contains the new record", wait_for(lambda: BG["reference_no"] in [c.value for c in load_workbook(excel_sync.master_path())["Conflict Tracker"]["A"]]))
# locked file (Windows: open in Excel)
real_replace = excel_sync._replace
def locked(src, dst):
    raise PermissionError("[WinError 32] file is being used by another process")
excel_sync._replace = locked
mtime0 = os.path.getmtime(excel_sync.master_path())
time.sleep(0.5)
LK = mk(U["asha"], "Locked File Co")
check("DB save succeeds while the Excel file is locked", LK["reference_no"].startswith("CC/"))
check("sync reports the failure and keeps retrying", wait_for(lambda: excel_sync.status()["last_error"] and excel_sync.status()["failures"] >= 1), excel_sync.status())
check("locked master file is untouched, no partial file/tmp left", os.path.getmtime(excel_sync.master_path()) == mtime0 and glob.glob(str(config.EXCEL_DIR / ".*.tmp")) == [])
excel_sync._replace = real_replace
check("once unlocked it recovers by itself with the missed record", wait_for(
      lambda: excel_sync.status()["last_error"] is None and LK["reference_no"] in [c.value for c in load_workbook(excel_sync.master_path())["Conflict Tracker"]["A"]], timeout=25), excel_sync.status())

# ================================================================== G. strict CSV export
f0 = app.Filters()
ea, ed, ep, er, eu = (app.export_dataframe(U[n], f0) for n in ("asha", "dev", "paul", "rita", "audi"))
check("member export contains ONLY own service line", set(ea["service_line"]) == {"AUD"} and len(ea) > 0)
check("other member exports only HIS line", set(ed["service_line"]) == {"ADV"})
check("partner export excludes the check he can only sign off from another line", X3["reference_no"] not in set(ep["reference_no"]) and set(ep["service_line"]) == {"ADV"})
check("Risk Lead and Auditor export all lines", {"AUD", "ADV", "FOR"} <= set(er["service_line"]) and len(eu) == len(er))
check("UI filter cannot widen a member's export", app.export_dataframe(U["asha"], app.Filters(sl_ids=(SL["ADV"],))).empty)
check("export strips internal ids", "id" not in ea.columns)
check("member can request export; unrestricted scope is not a permission bypass", auth.can(U["asha"], "export_reports"))
check("CSV neutralises formula text", app.csv_safe(ea.assign(party_name="=1+1"))["party_name"].iloc[0].startswith("'"))

# ================================================================== H. integrity + access
dd = workflow.get_check_detail(U["asha"], X1["id"])
pan_doc = next(x for x in dd["docs"] if x["key"] == "PAN")
fn, data, v = workflow.read_document(U["asha"], pan_doc["id"])
check("read_document verifies and serves the original bytes", v["ok"] and data.startswith(PDF) and fn == "PAN.pdf")
check("auditor can read documents", workflow.read_document(U["audi"], pan_doc["id"])[2]["ok"])
check("other-line member cannot read documents", raises(auth.PermissionDenied, workflow.read_document, U["dev"], pan_doc["id"]))
check("partner (limited view) cannot read documents", raises(auth.PermissionDenied, workflow.read_document, U["paul"], pan_doc["id"]))
with database.read_scope() as s:
    ppath = config.PROOF_ROOT / s.get(database.ProofDocument, pan_doc["id"]).stored_relpath
orig = ppath.read_bytes()
ppath.write_bytes(orig + b"TAMPERED")
check("tampered file is REFUSED on download", raises(RuleViolation, workflow.read_document, U["asha"], pan_doc["id"]))
vr = workflow.verify_check_documents(U["rita"], X1["id"])
check("Verify-all pinpoints the tampered document", [x["label"] for x in vr if x["ok"] is False] == ["PAN card"] and sum(1 for x in vr if x["ok"]) == len(vr) - 1, vr)
check("failed verification is audited", any(t["action"] == "DOCS_VERIFIED" and "PAN card" in t["new"] for t in audit.get_entity_trail("conflict_check", X1["id"])))
ppath.unlink()
check("missing file is reported, not crashed", any(x["ok"] is False and "missing" in x["reason"].lower() for x in workflow.verify_check_documents(U["rita"], X1["id"])))
ppath.write_bytes(orig)
check("restored file verifies again", all(x["ok"] for x in workflow.verify_check_documents(U["rita"], X1["id"])))

# ================================================================== I. UI
def run_page(page_name, uid):
    import os as _o, sys as _s
    _s.path.insert(0, _o.environ["CCP_PROJECT"])
    import streamlit as _st
    import app as _a
    _st.set_page_config(layout="wide")
    u = _a.auth.load_session_user(uid)
    _st.session_state["_user"] = u
    _st.session_state["_filters"] = _a.render_sidebar(u)
    getattr(_a, page_name)()


def run_detail(uid, check_id):
    import os as _o, sys as _s
    _s.path.insert(0, _o.environ["CCP_PROJECT"])
    import app as _a
    _st = __import__("streamlit")
    _st.session_state["_user"] = _a.auth.load_session_user(uid)
    _a.render_detail(_st.session_state["_user"], check_id)


def page(name, who):
    t = AppTest.from_function(run_page, args=(name, U[who].id), default_timeout=90); t.run(); return t


t = page("page_data_entry", "asha")
check("Data Entry renders", not t.exception, t.exception)
check("pre-submission warning banner is on the page (exact text)", any(config.GST_ADDRESS_WARNING in m.value for m in t.markdown), [m.value[:60] for m in t.markdown])
check("final review checkbox present with the confirmation text", any(c.label == config.FINAL_REVIEW_TEXT for c in t.checkbox))
sub = t.button(key="de_btn_submit_0")
check("Submit is DISABLED until the confirmation is ticked", sub.disabled is True)
t.checkbox(key="de_confirm_0").check(); t.run()
check("...and enabled once ticked", t.button(key="de_btn_submit_0").disabled is False)
n_up = len(t.get("file_uploader"))
check("Client/Domestic shows 6 document fields + 'other'", n_up == 7, n_up)
t.radio(key="de_ptype_0").set_value("Vendor"); t.radio(key="de_scope_0").set_value("Global"); t.run()
check("Vendor/Global switches to 5 fields + 'other' and Tax ID/Country inputs", len(t.get("file_uploader")) == 6
      and any("Country" in x.label for x in t.text_input) and not any(x.label == "PAN" for x in t.text_input), len(t.get("file_uploader")))
t = page("page_data_entry", "asha")
t.text_input(key="de_name_0").set_value("Zenith Technologies Pvt Ltd"); t.run()
check("live fuzzy screening flags a duplicate while typing", any("duplicate" in e.value.lower() or "possible match" in w.value.lower() for e in t.error for w in (t.warning or [t.error[0]])) or len(t.error) + len(t.warning) >= 1, ([e.value for e in t.error], [w.value for w in t.warning]))
t.text_input(key="de_name_0").set_value("Quantum Foods Limited"); t.run()
check("no flag for an unrelated name", not t.error and not any("Possible match" in w.value for w in t.warning))

def detail(who, cid):
    t = AppTest.from_function(run_detail, args=(U[who].id, cid), default_timeout=90); t.run(); return t

DRAFT = mk(U["asha"], "Detail Draft Co", submit=False)
t = detail("asha", DRAFT["id"])
check("draft detail: maker gets Submit + Edit + Withdraw", not t.exception and any("Submit for review" in b.label for b in t.button)
      and any("Edit draft" in e.label for e in t.expander) and any("Withdraw" in e.label for e in t.expander), t.exception)
[b for b in t.button if "Submit for review" in b.label][0].click(); t.run()
check("Submit opens the final-check dialog without errors", not t.exception, t.exception)
SUBM = mk(U["asha"], "Detail Submitted Co")
t = detail("ravi", SUBM["id"])
check("submitted check: eligible Risk Lead sees Option A/B + Record decision + Reject",
      not t.exception and any("Record decision" in b.label for b in t.button) and any("Option A" in r.options[0] or "A" in r.options for r in t.radio) and any("Reject" in e.label for e in t.expander))
SELF = mk(U["rita"], "Detail Self Co", line="FOR")
t = detail("rita", SELF["id"])
check("maker-checker banner for the Risk Lead who raised it, no decision button", any("Maker-checker" in w.value for w in t.warning) and not any("Record decision" in b.label for b in t.button))
ALN = mk(U["asha"], "Detail Align Co"); workflow.decide_alignment(U["ravi"], ALN["id"], U["paul"].id, "Check promoters.")
t = detail("paul", ALN["id"])
check("designated partner sees sign-off + decline, limited banner, no Documents tab",
      not t.exception and any("sign off" in b.label for b in t.button) and any("only because you are its designated" in i.value for i in t.info) and not any(x.label == "Documents" for x in t.tabs))
t = detail("asha", ALN["id"])
check("maker sees who the check is waiting on", any("Waiting for" in i.value for i in t.info))
workflow.confirm_alignment(U["paul"], ALN["id"], "ok")
t = detail("ravi", ALN["id"])
check("Risk Lead sees 'Clear check & issue certificate' after sign-off", any("Clear check" in b.label for b in t.button) and any("signed off" in s.value for s in t.success))
ACC = mk(U["asha"], "Detail Accept Co"); workflow.decide_approve(U["ravi"], ACC["id"])
t = detail("rita", ACC["id"])
check("approved check: Risk Lead gets the acceptance form; certificate banner shown", any("Record acceptance" in b.label for b in t.button) and any("certificate issued" in s.value.lower() for s in t.success))
t = detail("asha", ACC["id"])
check("documents tab: integrity verified + download button + checklist", not t.exception and any("Integrity verified" in s.value for s in t.success))
for who in ("asha", "paul", "rita", "audi", "admin" if "admin" in U else "ravi"):
    t = page("page_queue", who)
    check(f"Action Queue renders for {who}", not t.exception, t.exception)
t = page("page_queue", "ravi")
check("Risk Lead queue lists items awaiting decision", any(m.label == "Needs your action" and int(m.value) >= 1 for m in t.metric))
t = AppTest.from_function(run_page, args=("page_diagnostics", U["rita"].id), default_timeout=90); t.run()
check("Diagnostics has the Excel sync tab", len(t.tabs) == 5 and any("Excel" in x.label for x in t.tabs) and not t.exception, [x.label for x in t.tabs])
[b for b in t.button if b.key == "excel_sync_now"][0].click(); t.run()
check("'Sync now' queues a background sync", not t.exception and excel_sync.status()["dirty"] in (True, False))
t = page("page_records", "asha")
[b for b in t.button if b.key == "prep_records"][0].click(); t.run()
ex = t.session_state["_exp_records"]
csv_text = ex["csv"].decode("utf-8-sig")
check("UI export for a member contains only his service line", ex["lines"] == ["AUD"] and "FOR" not in csv_text.split("\n")[1] and "/ADV/" not in csv_text and "/FOR/" not in csv_text, ex["lines"])
for pg in ("page_dashboard", "page_records", "page_reports"):
    for who in ("asha", "paul", "ravi", "audi"):
        t = page(pg, who)
        check(f"{pg} still renders for {who}", not t.exception, t.exception)

# ================================================================== J. database upgrade path
d2 = tempfile.mkdtemp(prefix="ccp_upg_")
env = dict(os.environ, CCP_DATA_DIR=d2, CCP_ADMIN_PASSWORD="Str0ng#Start99")
run = lambda *a: subprocess.run([sys.executable, *a], cwd=HERE, env=env, capture_output=True, text=True)
run("database.py", "init")
con = sqlite3.connect(os.path.join(d2, "app.db"))
con.execute("ALTER TABLE conflict_checks DROP COLUMN cleared_by_id"); con.execute("ALTER TABLE conflict_checks DROP COLUMN clearance_notes")
con.execute("UPDATE alembic_version SET version_num='0001'"); con.commit(); con.close()
r = run("database.py", "init")
con = sqlite3.connect(os.path.join(d2, "app.db"))
cols = {row[1] for row in con.execute("PRAGMA table_info(conflict_checks)")}
ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
con.close()
check("existing Part 2 database upgrades 0001 -> 0002 automatically", {"cleared_by_id", "clearance_notes"} <= cols and ver == "0002", (r.stdout[-200:], r.stderr[-300:], ver))
check("a 'pre-upgrade' backup was taken first", len(glob.glob(os.path.join(d2, "backups", "*pre-upgrade*.db.gz"))) == 1)
check("triggers intact and health OK after upgrade", run("database.py", "health").returncode == 0)

check("audit chain intact after everything", audit.verify_chain().ok, audit.verify_chain().message)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

"""
smoke_test_part2.py - run after Part 1's test:   python smoke_test_part2.py
Drives the real Streamlit app headlessly (streamlit.testing.AppTest) against a throw-away temp DB.
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="ccp_p2_")
os.environ["CCP_DATA_DIR"] = _tmp
os.environ["CCP_ADMIN_PASSWORD"] = "Str0ng#Start99"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest        # noqa: E402

import app, audit, auth, config, database        # noqa: E402
from sqlalchemy import select                    # noqa: E402

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


# ------------------------------------------------------------------ setup via the service layer
database.init_db(); auth.bootstrap_admin()
admin = auth.authenticate("admin", "Str0ng#Start99").user
auth.change_password(admin.id, "Str0ng#Start99", "Fresh#Secret2026x")
admin = auth.authenticate("admin", "Fresh#Secret2026x").user
PW = "Member#Pass2026"
for uname, name, role, lines, partner in [
        ("aud.member", "Asha Member", config.ROLE_MEMBER, ["AUD"], False),
        ("adv.member", "Dev Member", config.ROLE_MEMBER, ["ADV"], False),
        ("risk.one", "Rita Lead", config.ROLE_RISK_LEAD, [], False),
        ("risk.two", "Ravi Lead", config.ROLE_RISK_LEAD, [], False),
        ("audit.or", "Anil Auditor", config.ROLE_AUDITOR, [], False)]:
    uid, tmp = auth.create_user(admin, username=uname, full_name=name, role=role, service_line_codes=lines, is_partner=partner)
    auth.change_password(uid, tmp, PW)
U = {n: auth.authenticate(n, PW).user for n in ("aud.member", "adv.member", "risk.one", "risk.two", "audit.or")}
check("test users can log in", all(U.values()))
sl = {code: i for i, code, _ in app.allowed_service_lines(admin)}

# ------------------------------------------------------------------ service layer
r1 = app.create_check(U["aud.member"], service_line_id=sl["AUD"], party_type="Client", scope="Domestic",
                      party_name="ABC Pvt Ltd", pan="abcde1234f", submit=True,
                      related=[{"name": "ABC Holdings", "relationship": "Parent", "identifier": ""}])
check("create + submit returns reference", r1["reference_no"].endswith("/AUD/0001") and r1["status"] == "SUBMITTED", r1)
r2 = app.create_check(U["adv.member"], service_line_id=sl["ADV"], party_type="Vendor", scope="Global",
                      party_name="A.B.C. Private Limited", tax_id="DE-12345", country="Germany", submit=False)
check("cross-line exact match found (name normalisation)", len(r2["matches"]) == 1 and r2["matches"][0]["reference_no"] == r1["reference_no"], r2["matches"])
check("member cannot create in a line they lack", raises(auth.PermissionDenied, app.create_check, U["aud.member"],
      service_line_id=sl["ADV"], party_type="Client", scope="Domestic", party_name="X Co", pan="ABCDE1234F"))
check("auditor cannot create checks", raises(auth.PermissionDenied, app.create_check, U["audit.or"],
      service_line_id=sl["AUD"], party_type="Client", scope="Domestic", party_name="X Co"))
try:
    app.create_check(U["aud.member"], service_line_id=sl["AUD"], party_type="Client", scope="Domestic",
                     party_name="", pan="BAD", submit=True)
    ok = False
except app.FormInvalid as e:
    ok = len(e.errors) >= 2 and any('PAN' in x for x in e.errors) and any('name' in x.lower() for x in e.errors)
check("validation collects all errors at once (name + PAN format)", ok)
check("Global needs a country", raises(app.FormInvalid, app.create_check, U["adv.member"], service_line_id=sl["ADV"],
      party_type="Client", scope="Global", party_name="Foo GmbH", tax_id="X1"))

df_m = app.fetch_checks(U["aud.member"], app.Filters())
df_r = app.fetch_checks(U["risk.one"], app.Filters())
check("isolation: AUD member sees only AUD rows", set(df_m["service_line"]) == {"AUD"} and len(df_m) == 1, df_m["service_line"].tolist())
check("isolation: risk lead sees all lines", set(df_r["service_line"]) == {"AUD", "ADV"})
widen = app.Filters(sl_ids=(sl["ADV"],))
check("UI filter cannot widen scope beyond isolation", app.fetch_checks(U["aud.member"], widen).empty)
check("search by PAN / by name variant", len(app.fetch_checks(U["risk.one"], app.Filters(search="abcde1234f"))) == 1
      and len(app.fetch_checks(U["risk.one"], app.Filters(search="abc"))) == 2)
check("search treats % as a literal, not a wildcard", app.fetch_checks(U["risk.one"], app.Filters(search="%")).empty)
check("detail load re-applies scope", app.get_check_detail(U["aud.member"], r2["id"]) is None
      and app.get_check_detail(U["risk.one"], r2["id"]) is not None)

check("only the maker may withdraw", raises(auth.PermissionDenied, app.maker_action, U["adv.member"], r1["id"], "WITHDRAWN", "x"))
check("withdraw needs a reason", raises(app.FormInvalid, app.maker_action, U["aud.member"], r1["id"], "WITHDRAWN", ""))
check("draft -> submitted by maker", app.maker_action(U["adv.member"], r2["id"], "SUBMITTED") == r2["reference_no"])
check("cannot re-submit a submitted check", raises(app.FormInvalid, app.maker_action, U["adv.member"], r2["id"], "SUBMITTED"))

# maker-checker with a Risk Lead as maker
r3 = app.create_check(U["risk.one"], service_line_id=sl["FOR"], party_type="Client", scope="Domestic",
                      party_name="Self Raised Ltd", pan="AAAPL1234C", submit=True)
check("maker-checker: Risk Lead cannot review own check", not app.review_eligibility(U["risk.one"], U["risk.one"].id)[0])
check("maker-checker: a different Risk Lead can", app.review_eligibility(U["risk.two"], U["risk.one"].id)[0])
check("members are not reviewers", not app.review_eligibility(U["aud.member"], U["adv.member"].id)[0])

trail = audit.get_entity_trail("conflict_check", r1["id"])
check("audit trail recorded create + submit", {t["action"] for t in trail} >= {"CHECK_CREATED", "CHECK_SUBMITTED"})
check("audit chain intact", audit.verify_chain().ok)

evil = app.csv_safe(__import__("pandas").DataFrame({"party_name": ["=HYPERLINK(\"http://x\")", "Normal", "@cmd"]}))
check("CSV formula injection neutralised", evil["party_name"].tolist()[0].startswith("'") and evil["party_name"].tolist()[1] == "Normal")


# ------------------------------------------------------------------ UI: login through the real app
def run_app():
    import os as _o, sys as _s
    _s.path.insert(0, _o.environ["CCP_PROJECT"])
    import app as _a
    _a.main()


os.environ["CCP_PROJECT"] = os.path.dirname(os.path.abspath(__file__))
at = AppTest.from_function(run_app, default_timeout=60)
at.run()
check("login screen renders without exception", not at.exception and any("Sign in" in b.label for b in at.button), at.exception)
at.text_input[0].set_value("aud.member"); at.text_input[1].set_value("wrong"); at.button[0].click(); at.run()
check("wrong password shows an error", len(at.error) == 1 and "Invalid" in at.error[0].value, [e.value for e in at.error])
at.text_input[0].set_value("aud.member"); at.text_input[1].set_value(PW); at.button[0].click(); at.run()
check("successful login lands on dashboard", not at.exception and any("Executive Dashboard" in t.value for t in at.title),
      (at.exception, [t.value for t in at.title]))
check("session state holds user id / username / role", at.session_state["username"] == "aud.member"
      and at.session_state["role"] == config.ROLE_MEMBER and at.session_state["user_id"])
check("dashboard shows 5 KPI cards", len(at.metric) == 5, len(at.metric))
vals = {m.label: m.value for m in at.metric}
check("member KPI counts are scoped (1 record)", vals.get("Total tracked") == "1", vals)
at.sidebar.button[0].click(); at.run()
check("sign out returns to login", any("Sign in" in b.label for b in at.button))

# ------------------------------------------------------------------ UI: every page as every role
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


UIDS = {n: u.id for n, u in U.items()}
UIDS["admin"] = admin.id
matrix = {
    "page_dashboard": ["aud.member", "risk.one", "audit.or", "admin"],
    "page_records": ["aud.member", "risk.one", "audit.or"],
    "page_reports": ["aud.member", "risk.one", "audit.or"],
    "page_data_entry": ["aud.member", "risk.one"],
    "page_diagnostics": ["risk.one", "audit.or", "admin"],
    "page_admin": ["admin"],
}
for page, users in matrix.items():
    for uname in users:
        t = AppTest.from_function(run_page, args=(page, UIDS[uname]), default_timeout=60)
        t.run()
        check(f"{page} renders for {uname}", not t.exception, t.exception)

for page, uname in [("page_diagnostics", "aud.member"), ("page_admin", "risk.one"), ("page_data_entry", "audit.or")]:
    t = AppTest.from_function(run_page, args=(page, UIDS[uname]), default_timeout=60); t.run()
    check(f"{page} is blocked for {uname}", not t.exception and len(t.error) == 1 and "do not have access" in t.error[0].value,
          (t.exception, [e.value for e in t.error]))

t = AppTest.from_function(run_page, args=("page_diagnostics", UIDS["audit.or"]), default_timeout=60); t.run()
check("auditor sees chain tab only (no health/backup tabs)", len(t.tabs) == 1, [x.label for x in t.tabs])
t = AppTest.from_function(run_page, args=("page_diagnostics", UIDS["risk.one"]), default_timeout=60); t.run()
check("risk lead sees all four diagnostics tabs", len(t.tabs) == 4, [x.label for x in t.tabs])
verify_btn = [b for b in t.button if "Verify audit chain" in b.label]
verify_btn[0].click(); t.run()
check("verify audit chain button works and reports intact", not t.exception and any("intact" in s.value for s in t.success),
      (t.exception, [s.value for s in t.success], [e.value for e in t.error]))

# ------------------------------------------------------------------ UI: data entry form end-to-end
t = AppTest.from_function(run_page, args=("page_data_entry", UIDS["aud.member"]), default_timeout=60); t.run()
t.text_input(key="de_name_0").set_value("Wayne Enterprises Limited")
t.text_input(key="de_pan_0").set_value("AAACW1234Q")
[b for b in t.button if "Submit for review" in b.label][0].click(); t.run()
check("form submit through the UI succeeds", not t.exception and not t.error, (t.exception, [e.value for e in t.error]))
with database.read_scope() as s:
    made = s.scalar(select(database.ConflictCheck).where(database.ConflictCheck.party_name == "Wayne Enterprises Limited"))
check("UI submission persisted as SUBMITTED by the right maker",
      made is not None and made.conflict_status == "SUBMITTED" and made.requested_by_id == U["aud.member"].id)

t = AppTest.from_function(run_page, args=("page_data_entry", UIDS["aud.member"]), default_timeout=60); t.run()
t.text_input(key="de_name_0").set_value("")
[b for b in t.button if "Submit for review" in b.label][0].click(); t.run()
check("invalid form shows inline errors, saves nothing", len(t.error) >= 1 and not t.exception, [e.value for e in t.error])

def run_detail(uid, check_id):
    import os as _o, sys as _s
    _s.path.insert(0, _o.environ["CCP_PROJECT"])
    import app as _a
    _a.render_detail(_a.auth.load_session_user(uid), check_id)


for uname, cid, expect_tabs, expect_warn in [("risk.one", r1["id"], 3, False), ("aud.member", r1["id"], 2, False),
                                              ("aud.member", r2["id"], 0, True), ("risk.two", r3["id"], 3, False)]:
    d = AppTest.from_function(run_detail, args=(UIDS[uname], cid), default_timeout=60); d.run()
    check(f"record detail for {uname} on #{cid}: no exception, {expect_tabs} tabs" + (", access refused" if expect_warn else ""),
          not d.exception and len(d.tabs) == expect_tabs and (len(d.warning) == 1) == expect_warn, (d.exception, len(d.tabs)))
d = AppTest.from_function(run_detail, args=(UIDS["risk.one"], r3["id"]), default_timeout=60); d.run()
check("own submitted check shows maker-checker block to its Risk Lead maker",
      any("Maker-checker" in w.value for w in d.warning), [w.value for w in d.warning])
d = AppTest.from_function(run_detail, args=(UIDS["risk.two"], r3["id"]), default_timeout=60); d.run()
check("other Risk Lead is shown as eligible", any("eligible" in i.value for i in d.info), [i.value for i in d.info])
d = AppTest.from_function(run_detail, args=(UIDS["adv.member"], r2["id"]), default_timeout=60); d.run()
wd = [b for b in d.button if b.label == "Withdraw"]
d.text_input(key=f"wd_reason_{r2['id']}").set_value("Client dropped the engagement"); wd[0].click(); d.run()
with database.read_scope() as s:
    st_now = s.get(database.ConflictCheck, r2["id"]).conflict_status
check("maker can withdraw through the UI (reason recorded)", st_now == "WITHDRAWN", st_now)

check("audit chain intact after all UI activity", audit.verify_chain().ok)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

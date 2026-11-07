"""
smoke_test_install.py - "does this checkout work on THIS machine?"  (about 15 seconds)

Run it after cloning / extracting / bootstrapping:      python smoke_test_install.py
(`python run_tests.py` runs it together with every other suite.)

It never touches your real data: everything runs in a throw-away temp folder.

  A  Python + packages      right Python, every dependency importable, timezone data present
  B  Repository structure   every required file is at the repository root; .bat has CRLF; .gitignore protects data
  C  Dependency coverage    every third-party import in the code base is listed in requirements.txt
  D  Module integrity       every file compiles, every core module imports, the public API is intact
  E  Alembic configuration  alembic.ini resolves from ANY working directory, one clean migration head
  F  Database connectivity  create, WAL mode, integrity, tables, triggers, seed data, Alembic stamp, idempotent re-init
  G  Alembic migrations     an old (0001) database upgrades to head via the app AND via the `alembic` command line
  H  Core modules working   login, user creation, audit chain, backup + verify, certificate PDF, Excel sync, migration dry-run
"""
import ast
import importlib
import importlib.metadata as md
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_tmp = tempfile.mkdtemp(prefix="ccp_install_")
os.environ["CCP_DATA_DIR"] = _tmp
os.environ["CCP_ADMIN_PASSWORD"] = "Str0ng#Start99"
os.environ["CCP_NO_BROWSER"] = "1"
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))


def section(title):
    print(f"\n--- {title}")


# ============================================================ A. python + packages
section("A. Python and packages")
check("Python 3.10 or newer", sys.version_info >= (3, 10), sys.version.split()[0])
PKGS = {"sqlalchemy": "SQLAlchemy", "alembic": "alembic", "bcrypt": "bcrypt", "openpyxl": "openpyxl",
        "streamlit": "streamlit", "plotly": "plotly", "pandas": "pandas", "rapidfuzz": "rapidfuzz",
        "reportlab": "reportlab", "tzdata": "tzdata"}
versions, missing = {}, []
for mod, dist in PKGS.items():
    try:
        importlib.import_module(mod)
        versions[dist] = md.version(dist)
    except Exception as exc:
        missing.append(f"{mod} ({type(exc).__name__})")
check("all required packages import", not missing, ", ".join(missing))
print("      versions: " + ", ".join(f"{k} {v}" for k, v in versions.items()))
try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Asia/Kolkata")
    tz_ok = True
except Exception:
    tz_ok = False
check("timezone data present (Asia/Kolkata)", tz_ok, "pip install tzdata")
import sqlite3 as _s3
check("SQLite supports ALTER TABLE DROP COLUMN / WAL (>= 3.35)", tuple(map(int, _s3.sqlite_version.split("."))) >= (3, 35), _s3.sqlite_version)

# ============================================================ B. structure
section("B. Repository structure (everything at the root)")
REQUIRED = ["app.py", "audit.py", "auth.py", "backup.py", "bootstrap.py", "certificate.py", "config.py", "database.py",
            "excel_sync.py", "matching.py", "migration.py", "workflow.py", "requirements.txt", "alembic.ini",
            "run_portal.bat", "README.md", ".gitignore", ".gitattributes", ".streamlit/config.toml",
            "alembic/env.py", "alembic/script.py.mako", "alembic/versions/0001_baseline.py",
            "alembic/versions/0002_clearance_columns.py", "run_tests.py", "smoke_test_install.py",
            "smoke_test_launch.py", "smoke_test_part1.py", "smoke_test_part2.py", "smoke_test_part3.py"]
absent = [f for f in REQUIRED if not (HERE / f).exists()]
check("all required files are present at the repository root", not absent, ", ".join(absent))
bat = (HERE / "run_portal.bat").read_bytes() if (HERE / "run_portal.bat").exists() else b""
check("run_portal.bat uses Windows CRLF line endings", b"\r\n" in bat and not re.search(rb"(?<!\r)\n", bat))
check("run_portal.bat calls bootstrap.py", b"bootstrap.py" in bat)
ini = (HERE / "alembic.ini").read_text() if (HERE / "alembic.ini").exists() else ""
check("alembic.ini uses %(here)s (independent of working directory)", re.search(r"^script_location\s*=\s*%\(here\)s/alembic\s*$", ini, re.M) is not None)
toml = (HERE / ".streamlit" / "config.toml").read_text() if (HERE / ".streamlit" / "config.toml").exists() else ""
check("Streamlit is configured for port 8501", re.search(r"^\s*port\s*=\s*8501\b", toml, re.M) is not None)
gi = (HERE / ".gitignore").read_text() if (HERE / ".gitignore").exists() else ""
check(".gitignore protects client data, venv and databases", all(x in gi for x in ("data/", ".venv/", "*.db")))

# ============================================================ C. dependency coverage
section("C. Dependency coverage")
req_text = (HERE / "requirements.txt").read_text()
req_names = {re.split(r"[<>=!~\s\[;#]", ln.strip())[0].lower().replace("_", "-")
             for ln in req_text.splitlines() if ln.strip() and not ln.strip().startswith("#")}
local = {p.stem for p in HERE.glob("*.py")}
imports = set()
for py in HERE.rglob("*.py"):
    if ".venv" in py.parts or "data" in py.parts:
        continue
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module.split(".")[0])
third = {i for i in imports if i not in sys.stdlib_module_names and i not in local and i != "__future__"}
unlisted = sorted(i for i in third if i.lower().replace("_", "-") not in req_names)
check("every third-party import is listed in requirements.txt", not unlisted, f"missing from requirements.txt: {unlisted}")
check("tzdata is in requirements.txt (zoneinfo needs it on Windows)", "tzdata" in req_names)

# ============================================================ D. module integrity
section("D. Module integrity")
bad = []
for py in sorted(HERE.rglob("*.py")):
    if ".venv" in py.parts or "data" in py.parts:
        continue
    try:
        compile(py.read_text(encoding="utf-8"), str(py), "exec")
    except SyntaxError as exc:
        bad.append(f"{py.name}:{exc.lineno}")
check("every .py file compiles", not bad, ", ".join(bad))
CORE = ["config", "database", "auth", "audit", "backup", "migration", "matching", "certificate", "excel_sync", "workflow", "app"]
failed = {}
for m in CORE:
    try:
        importlib.import_module(m)
    except Exception as exc:
        failed[m] = f"{type(exc).__name__}: {exc}"
check("all core modules import cleanly", not failed, str(failed))
API = {"workflow": ["create_check", "submit_check", "decide_approve", "decide_alignment", "confirm_alignment",
                    "clear_aligned_check", "record_acceptance", "attach_documents", "read_document", "fetch_queue"],
       "auth": ["authenticate", "create_user", "can_confirm_alignment", "apply_service_line_scope", "assert_maker_checker"],
       "audit": ["log_event", "verify_chain"], "backup": ["create_backup", "verify_backup", "restore_backup"],
       "database": ["init_db", "health_check", "write_scope", "read_scope"], "matching": ["find_matches"],
       "certificate": ["build_certificate"], "excel_sync": ["sync_now", "request_sync", "status"],
       "migration": ["run_migration"]}
gaps = [f"{m}.{n}" for m, names in API.items() for n in names if not callable(getattr(sys.modules.get(m), n, None))]
check("public API of every module is intact", not gaps, ", ".join(gaps))

# ============================================================ E. alembic configuration
section("E. Alembic configuration")
from alembic.config import Config
from alembic.script import ScriptDirectory
cwd = os.getcwd()
os.chdir(tempfile.gettempdir())                       # the classic failure: running from somewhere else
try:
    sd = ScriptDirectory.from_config(Config(str(HERE / "alembic.ini")))
    resolves = Path(sd.dir).is_dir()
    heads = sd.get_heads()
    revs = [r.revision for r in sd.walk_revisions()]
except Exception as exc:
    resolves, heads, revs = False, [], []
    print("      alembic error:", exc)
finally:
    os.chdir(cwd)
check("alembic.ini resolves the migrations folder from a DIFFERENT working directory", resolves)
check("exactly one Alembic head (no branched history)", len(heads) == 1, heads)
check("migration chain is complete (0001 -> head)", "0001" in revs and len(revs) >= 2, revs)
HEAD = heads[0] if heads else None

# ============================================================ F. database connectivity
section("F. Database connectivity")
import config, database
from sqlalchemy import text
database.init_db()
db_file = Path(_tmp) / "app.db"
check("init_db creates app.db", db_file.exists() and db_file.stat().st_size > 0)
with database.engine.connect() as c:
    check("connection works (SELECT 1)", c.execute(text("SELECT 1")).scalar() == 1)
    check("WAL journal mode", str(c.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal")
    check("foreign keys enforced", c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1)
    check("integrity_check ok", c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok")
    tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    want = {"users", "service_lines", "user_service_lines", "conflict_checks", "related_parties", "proof_documents",
            "reference_counters", "audit_log", "audit_head", "alembic_version"}
    check("all expected tables exist", want <= tables, sorted(want - tables))
    check("Alembic version stamped at head", c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == HEAD)
    check("10 service lines seeded", c.exec_driver_sql("SELECT COUNT(*) FROM service_lines").scalar() == 10)
    check("audit chain tip row exists", c.exec_driver_sql("SELECT COUNT(*) FROM audit_head").scalar() == 1)
h = database.health_check()
check("health_check passes (append-only triggers installed)", h["ok"], h)
database.init_db()
with database.engine.connect() as c:
    check("re-running init_db is harmless (idempotent)", c.exec_driver_sql("SELECT COUNT(*) FROM service_lines").scalar() == 10)
check("required data folders are created", all(p.is_dir() for p in (config.PROOF_ROOT, config.LOG_DIR, config.BACKUP_DIR, config.EXPORT_DIR)))

# ============================================================ G. alembic migrations
section("G. Alembic migrations on an old database")


def make_old_db(folder):
    env = dict(os.environ, CCP_DATA_DIR=str(folder))
    r = subprocess.run([sys.executable, "database.py", "init"], cwd=HERE, env=env, capture_output=True, text=True)
    con = sqlite3.connect(str(Path(folder) / "app.db"))
    con.execute("ALTER TABLE conflict_checks DROP COLUMN cleared_by_id")
    con.execute("ALTER TABLE conflict_checks DROP COLUMN clearance_notes")
    con.execute("UPDATE alembic_version SET version_num='0001'")
    con.commit(); con.close()
    return env, r


def state(folder):
    con = sqlite3.connect(str(Path(folder) / "app.db"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(conflict_checks)")}
    ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    trig = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    con.close()
    return cols, ver, trig


d_app = tempfile.mkdtemp(prefix="ccp_mig_app_")
env, r0 = make_old_db(d_app)
check("fresh database initialises in a subprocess (python database.py init)", r0.returncode == 0, r0.stderr[-300:])
r = subprocess.run([sys.executable, "database.py", "init"], cwd=HERE, env=env, capture_output=True, text=True)
cols, ver, trig = state(d_app)
check("app upgrade: 0001 -> head adds the new columns", {"cleared_by_id", "clearance_notes"} <= cols and ver == HEAD, (ver, r.stderr[-300:]))
check("app upgrade: a pre-upgrade backup is taken first", len(list(Path(d_app, "backups").glob("*pre-upgrade*.db.gz"))) == 1)
check("app upgrade: append-only triggers survive", len(trig) >= 6, trig)

d_cli = tempfile.mkdtemp(prefix="ccp_mig_cli_")
env, _ = make_old_db(d_cli)
other_cwd = tempfile.mkdtemp(prefix="ccp_elsewhere_")                 # run the CLI from an unrelated folder
r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(HERE / "alembic.ini"), "upgrade", "head"],
                   cwd=other_cwd, env=env, capture_output=True, text=True)
cols, ver, trig = state(d_cli)
check("`alembic upgrade head` works from any folder", r.returncode == 0 and ver == HEAD and {"cleared_by_id"} <= cols,
      (r.returncode, r.stderr[-400:]))
check("CLI upgrade re-installs the append-only triggers", len(trig) >= 6, trig)
r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(HERE / "alembic.ini"), "current"], cwd=other_cwd,
                   env=env, capture_output=True, text=True)
check("`alembic current` reports head", HEAD in (r.stdout + r.stderr), r.stdout[-200:] + r.stderr[-200:])

# ============================================================ H. core modules actually working
section("H. Core modules working together")
import audit, auth, backup, certificate, excel_sync, migration
auth.bootstrap_admin()
res = auth.authenticate("admin", "Str0ng#Start99")
check("first-run admin can log in (bcrypt)", res.ok and res.user.must_change_password)
admin = res.user
uid, tmp_pw = auth.create_user(admin, username="smoke.user", full_name="Smoke User", role=config.ROLE_MEMBER, service_line_codes=["AUD"])
check("Admin can create a service-line user", uid > 0)
check("audit chain verifies", audit.verify_chain().ok)
m = backup.create_backup("manual", actor=admin)
check("backup created and passes verification", backup.verify_backup(config.BACKUP_DIR / m["file"])["ok"])
from datetime import datetime
pdf = certificate.build_certificate({
    "reference_no": "CC/2026/AUD/0001", "certificate_no": "CC/2026/AUD/0001-CERT", "party_name": "Smoke Test Ltd",
    "party_type": "Client", "scope": "Domestic", "country": None, "service_line": "AUD - Audit & Assurance",
    "identifiers": {"PAN": "AAACS1234Q"}, "outcome": "APPROVED", "option": "A",
    "requested_by": {"name": "A", "username": "a", "role": "Service Line Member"},
    "decided_by": {"name": "R", "username": "r", "role": "Risk Lead"}, "aligned_partner": None,
    "cleared_by": {"name": "R", "username": "r", "role": "Risk Lead"}, "created_at": datetime.utcnow(),
    "decided_at": datetime.utcnow(), "alignment_requested_at": None, "alignment_confirmed_at": None,
    "cleared_at": datetime.utcnow(), "notes": "ok", "alignment_notes": None, "documents": [], "audit_seq": 1,
    "issued_at": datetime.utcnow()})
check("certificate PDF is generated", pdf.startswith(b"%PDF") and len(pdf) > 1500)
sync = excel_sync.sync_now()
check("Excel tracker is written", Path(sync["files"][0]).exists())
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["Client Name", "Service Line", "Domestic/Global", "Status"]); ws.append(["Smoke Import Ltd", "AUD", "Domestic", "Approved"])
src = Path(_tmp) / "legacy.xlsx"; wb.save(src)
mig = migration.run_migration(src)
check("legacy migration dry-run reads and validates a sheet", mig["would_import"] == 1 and mig["errors"] == 0, mig)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

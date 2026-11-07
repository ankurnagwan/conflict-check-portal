#!/usr/bin/env python3
"""
bootstrap.py - one command to a running portal.  Standard library ONLY, so it works on a bare Python install.

    python bootstrap.py              # build/heal everything, then run the portal (auto-restarts on a crash)
    python bootstrap.py --check      # build/heal everything, do NOT launch (use this to test an install)
    python bootstrap.py --upgrade    # force `pip install --upgrade` even if requirements.txt did not change
    python bootstrap.py --reset-venv # delete and rebuild .venv from scratch
    python bootstrap.py --port 8600  # run on another port
    python bootstrap.py --no-restart # do not restart the portal if it crashes
    python bootstrap.py --no-browser # do not open the browser automatically (also: set CCP_NO_BROWSER=1)

What it does, in order (every step is idempotent and safe to repeat)
  1. Checks Python >= 3.10 and that the folder was really extracted (not run from inside a .zip).
  2. Creates the folders the app needs (data, proofs, exports, logs, backups, alembic/versions ...).
  3. Heals the Alembic setup: alembic.ini (script_location), alembic/env.py, script.py.mako and the two
     baseline migrations are re-created ONLY if missing. Existing files are never overwritten (except a
     wrong script_location line, which is fixed and the old file kept as alembic.ini.bak).
  4. Creates .venv. A .venv copied from another machine (broken absolute paths) is detected and rebuilt.
  5. Installs/upgrades requirements.txt inside .venv, only when it changed (hash stamp). If pip fails but the
     packages are already importable (e.g. no internet) it warns and carries on.
  6. Runs a SQLite quick_check on an existing app.db (it NEVER deletes or "repairs" your data: on a bad
     database it stops and tells you to restore a backup), then `database.py init` = create/upgrade schema.
  7. Launches `streamlit run app.py` with the .venv interpreter, opens http://localhost:8501 in your browser
     once the server answers, and supervises it (restarts it after a crash).

Output is ASCII-only so a Windows console can never crash on it. A copy goes to data/logs/bootstrap.log.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import threading
import traceback
import urllib.request
import venv
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
MIN_PY = (3, 10)
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"
DATA_DIR = Path(os.environ.get("CCP_DATA_DIR") or ROOT / "data").resolve()
PROOF_ROOT = Path(os.environ.get("CCP_PROOF_ROOT") or DATA_DIR / "proofs").resolve()
BACKUP_DIR = Path(os.environ.get("CCP_BACKUP_DIR") or DATA_DIR / "backups").resolve()
LOG_FILE = DATA_DIR / "logs" / "bootstrap.log"
IMPORT_CHECK = "import streamlit, sqlalchemy, alembic, bcrypt, openpyxl, pandas, plotly, rapidfuzz, reportlab, tzdata"
MAX_RESTARTS_PER_10_MIN = 5


class BootstrapError(Exception):
    """A problem we can explain to the user in plain words."""


def say(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now():%H:%M:%S}] {level:<5} {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
    except OSError:
        pass


def child_env() -> dict:
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
               STREAMLIT_BROWSER_GATHER_USAGE_STATS="false")
    return env


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], cwd=ROOT, env=child_env(), **kw)


# --------------------------------------------------------------------------- default (fallback) file contents
DEFAULT_REQUIREMENTS = """\
SQLAlchemy>=2.0,<2.1
alembic>=1.13,<2
bcrypt>=4.1,<6
openpyxl>=3.1,<4
tzdata
streamlit>=1.40,<2
plotly>=5.20,<7
pandas>=2.1,<3
rapidfuzz>=3.6,<4
reportlab>=4.0,<6
"""

DEFAULT_ALEMBIC_INI = """\
# Alembic configuration (re-created by bootstrap.py if missing).
# %(here)s is the folder that contains THIS file, so these paths work from any working
# directory and however the repository was extracted or moved.
[alembic]
script_location = %(here)s/alembic
prepend_sys_path = %(here)s
path_separator = os
version_path_separator = os
# sqlalchemy.url is intentionally absent: alembic/env.py uses the application's own engine.
"""

DEFAULT_STREAMLIT_TOML = """\
[server]
headless = true
address = "0.0.0.0"
port = 8501
maxUploadSize = 25
enableXsrfProtection = true

[browser]
gatherUsageStats = false

[client]
toolbarMode = "minimal"
"""

ALEMBIC_ENV = '''\
"""Alembic environment: uses the app's own engine (WAL, FK pragmas) and models."""
from alembic import context

import database

target_metadata = database.Base.metadata


def run_migrations_online() -> None:
    with database.engine.connect() as connection:
        # SQLite cannot ALTER most things in place: batch mode rebuilds the table.
        # FK enforcement must be off while a table is rebuilt.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            context.configure(connection=connection, target_metadata=target_metadata,
                              render_as_batch=True, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
            # A rebuilt table loses its triggers: always re-assert them.
            database.install_triggers(connection)
            connection.commit()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) mode is not supported for this project.")
run_migrations_online()
'''

ALEMBIC_MAKO = '''\
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''

MIGRATION_0001 = '''\
"""baseline: schema created by database.init_db() (create_all + triggers)

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass   # fresh installs are built from the models and stamped to head


def downgrade() -> None:
    pass
'''

MIGRATION_0002 = '''\
"""clearance columns: who cleared a check and why

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN: no table rebuild, so triggers and indexes are untouched.
    op.add_column("conflict_checks", sa.Column("cleared_by_id", sa.Integer(), nullable=True))
    op.add_column("conflict_checks", sa.Column("clearance_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conflict_checks", "clearance_notes")
    op.drop_column("conflict_checks", "cleared_by_id")
'''


# --------------------------------------------------------------------------- step 1: python + location
def step_python() -> None:
    if sys.version_info < MIN_PY:
        raise BootstrapError(
            f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required but this is {sys.version.split()[0]}. "
            "Install a current Python from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'.")
    p = str(ROOT)
    if re.search(r"\.zip[\\/]|Temp1_|Rar\$", p, re.I):
        raise BootstrapError("This folder is running from INSIDE a zip file. Right-click the zip, choose 'Extract All', "
                             "then start the portal from the extracted folder.")
    if "%" in p:
        say("The folder path contains a '%' character, which can confuse Alembic. Rename the folder if you see "
            "migration errors.", "WARN")
    say(f"Python {sys.version.split()[0]} | project folder: {ROOT}")


# --------------------------------------------------------------------------- step 2: folders
def step_dirs() -> None:
    wanted = [ROOT / "alembic", ROOT / "alembic" / "versions", ROOT / ".streamlit", DATA_DIR, PROOF_ROOT,
              DATA_DIR / "exports", DATA_DIR / "exports" / "by_service_line", DATA_DIR / "logs",
              BACKUP_DIR, DATA_DIR / "migration_reports"]
    made = []
    for d in wanted:
        if not d.exists():
            made.append(d)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BootstrapError(f"Cannot create folder {d}: {exc}. Check that you have write permission there.")
    probe = DATA_DIR / ".write_test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise BootstrapError(f"The data folder {DATA_DIR} is not writable: {exc}")
    say(f"Folders OK ({len(made)} created). Data folder: {DATA_DIR}")
    low = str(DATA_DIR).lower()
    if low.startswith("\\\\") or any(k in low for k in ("onedrive", "sharepoint", "dropbox", "google drive")):
        say("The data folder is on a network share / cloud-synced folder. SQLite can CORRUPT there. "
            "Set CCP_DATA_DIR to a LOCAL disk (e.g. C:\\CCP_Data) and point CCP_BACKUP_DIR at the cloud folder.", "WARN")


# --------------------------------------------------------------------------- step 3: alembic + config healing
def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    say(f"Re-created missing file: {path.relative_to(ROOT)}", "FIX")
    return True


def _heal_alembic_ini() -> None:
    ini = ROOT / "alembic.ini"
    if not ini.exists() or ini.stat().st_size == 0:
        _write_if_missing(ini, DEFAULT_ALEMBIC_INI)
        return
    text = ini.read_text(encoding="utf-8", errors="replace")
    good = re.search(r"^\s*script_location\s*=\s*%\(here\)s[\\/]+alembic\s*$", text, re.M | re.I)
    if "[alembic]" not in text or not good:
        shutil.copy2(ini, ini.with_name("alembic.ini.bak"))
        if "[alembic]" not in text:
            new = DEFAULT_ALEMBIC_INI
        elif re.search(r"^\s*script_location\s*=", text, re.M):
            new = re.sub(r"^\s*script_location\s*=.*$", "script_location = %(here)s/alembic", text, count=1, flags=re.M)
        else:
            new = text.replace("[alembic]", "[alembic]\nscript_location = %(here)s/alembic", 1)
        if not re.search(r"^\s*prepend_sys_path\s*=", new, re.M):
            new = new.replace("[alembic]", "[alembic]\nprepend_sys_path = %(here)s", 1)
        ini.write_text(new, encoding="utf-8", newline="\n")
        say("Fixed alembic.ini: script_location now points at the 'alembic' folder next to it "
            "(previous file saved as alembic.ini.bak).", "FIX")


def step_alembic() -> None:
    _heal_alembic_ini()
    ad = ROOT / "alembic"
    _write_if_missing(ad / "env.py", ALEMBIC_ENV)
    _write_if_missing(ad / "script.py.mako", ALEMBIC_MAKO)
    versions = ad / "versions"
    existing = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in versions.glob("*.py"))
    for rev, name, body in (("0001", "0001_baseline.py", MIGRATION_0001), ("0002", "0002_clearance_columns.py", MIGRATION_0002)):
        if not re.search(rf"^revision\s*=\s*[\"']{rev}[\"']", existing, re.M):
            _write_if_missing(versions / name, body)
    _write_if_missing(ROOT / ".streamlit" / "config.toml", DEFAULT_STREAMLIT_TOML)
    if not REQ.exists() or REQ.stat().st_size == 0:
        _write_if_missing(REQ, DEFAULT_REQUIREMENTS)
    missing_app = [f for f in ("app.py", "database.py", "config.py", "auth.py", "audit.py", "workflow.py") if not (ROOT / f).exists()]
    if missing_app:
        raise BootstrapError("These application files are missing: " + ", ".join(missing_app) +
                             ". The download/extract was incomplete - extract the full repository again.")
    say("Alembic and config files OK.")


# --------------------------------------------------------------------------- step 4/5: venv + dependencies
def _venv_healthy() -> bool:
    if not VPY.exists():
        return False
    try:
        r = run([VPY, "-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and int(r.stdout.strip()) >= MIN_PY[0] * 100 + MIN_PY[1]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _rmtree(path: Path) -> None:
    def onerr(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerr)


def step_venv(reset: bool) -> bool:
    """Returns True if the venv was (re)created."""
    if VENV.exists() and (reset or not _venv_healthy()):
        say("Removing a broken/old .venv (it may have been copied from another computer)." if not reset
            else "Removing .venv as requested.", "FIX")
        try:
            _rmtree(VENV)
        except OSError as exc:
            raise BootstrapError(f"Could not delete {VENV}: {exc}. Close any running portal/terminal using it and retry.")
    if VENV.exists():
        say(".venv OK.")
        return False
    say("Creating virtual environment in .venv (one-time, about a minute)...")
    try:
        venv.create(VENV, with_pip=True, clear=False, symlinks=not IS_WIN)
    except Exception as exc:
        raise BootstrapError(f"Could not create the virtual environment: {exc}. On Debian/Ubuntu run "
                             "'sudo apt install python3-venv' first.")
    if not _venv_healthy():
        raise BootstrapError("The new .venv does not work. Delete the .venv folder and try again.")
    return True


def _imports_ok() -> bool:
    return run([VPY, "-c", IMPORT_CHECK], capture_output=True, text=True).returncode == 0


def step_deps(created: bool, force: bool) -> None:
    py_ver = run([VPY, "-c", "import sys; print(sys.version)"], capture_output=True, text=True).stdout.strip()
    want = hashlib.sha256(REQ.read_bytes() + py_ver.encode()).hexdigest()
    have = STAMP.read_text().strip() if STAMP.exists() else ""
    if not force and not created and have == want and _imports_ok():
        say("Dependencies already up to date (requirements.txt unchanged).")
        return
    say("Upgrading pip inside .venv ...")
    pr = run([VPY, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True, text=True)
    if pr.returncode != 0:
        say("Could not upgrade pip (continuing with the installed version).", "WARN")
    say("Installing/upgrading dependencies from requirements.txt (first run can take a few minutes)...")
    rc = 1
    for attempt in (1, 2):
        rc = run([VPY, "-m", "pip", "install", "--upgrade", "-r", REQ]).returncode
        if rc == 0:
            break
        say(f"pip failed (attempt {attempt}/2)." + (" Retrying..." if attempt == 1 else ""), "WARN")
        time.sleep(3)
    if rc != 0:
        if _imports_ok():
            say("Could not reach the package index, but every required package is already installed. "
                "Continuing with the installed versions.", "WARN")
            return
        raise BootstrapError("Dependencies could not be installed (no internet, a proxy, or a blocked package index?). "
                             "Check the connection and run again. Output above shows the exact pip error.")
    if not _imports_ok():
        raise BootstrapError("pip finished but some packages still cannot be imported. Run: python bootstrap.py --reset-venv")
    STAMP.write_text(want)
    say("Dependencies OK.")


# --------------------------------------------------------------------------- step 6: database
def step_database() -> None:
    db = DATA_DIR / "app.db"
    if db.exists() and db.stat().st_size > 0:
        try:
            con = sqlite3.connect(str(db), timeout=30)
            try:
                res = con.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error as exc:
            res = f"unreadable: {exc}"
        if res != "ok":
            raise BootstrapError(
                f"The database {db} failed its integrity check ({res}). It was NOT touched. Restore the newest backup:\n"
                f"    .venv\\Scripts\\python backup.py list\n    .venv\\Scripts\\python backup.py restore <file>\n"
                f"Backups live in {BACKUP_DIR}.")
    say("Initialising / migrating the database (an automatic backup is taken first if a schema upgrade is needed)...")
    rc = run([VPY, "database.py", "init"]).returncode     # output streams live: first-run admin password is printed here
    if rc != 0:
        raise BootstrapError("Database initialisation failed (details above, and in data\\logs\\error.log). "
                             "The portal was not started to protect your data.")
    say("Database OK.")


# --------------------------------------------------------------------------- step 7: launch
def _configured_port() -> int:
    toml = ROOT / ".streamlit" / "config.toml"
    if toml.exists():
        m = re.search(r"^\s*port\s*=\s*(\d+)", toml.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return int(m.group(1))
    return 8501


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is sent; just selects the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _open_browser_when_ready(port: int) -> None:
    """Background helper: waits until Streamlit answers, then opens the default browser ONCE."""
    def worker() -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as r:
                    if r.read() == b"ok":
                        webbrowser.open(f"http://localhost:{port}")
                        return
            except Exception:
                pass
            time.sleep(1)
    threading.Thread(target=worker, name="ccp-open-browser", daemon=True).start()


def step_launch(port: int, restart: bool, open_browser: bool = True) -> int:
    if _port_in_use(port):
        raise BootstrapError(f"Port {port} is already in use - the portal is probably already running "
                             f"(open http://localhost:{port}). To run a second copy use: python bootstrap.py --port {port + 1}")
    cmd = [VPY, "-m", "streamlit", "run", "app.py", "--server.port", str(port)]
    say("=" * 62)
    say(f"Portal starting.   On this PC : http://localhost:{port}")
    say(f"                   On the LAN : http://{_lan_ip()}:{port}   (allow the port in Windows Firewall)")
    say("Press Ctrl+C to stop.")
    say("=" * 62)
    if open_browser:
        _open_browser_when_ready(port)
    starts: list[float] = []
    while True:
        try:
            rc = run(cmd).returncode
        except KeyboardInterrupt:
            say("Stopped by user.")
            return 0
        if rc == 0 or not restart:
            say(f"Portal exited (code {rc}).")
            return rc
        now = time.time()
        starts = [t for t in starts if now - t < 600] + [now]
        if len(starts) > MAX_RESTARTS_PER_10_MIN:
            say(f"The portal crashed {len(starts)} times in 10 minutes (last code {rc}); not restarting again. "
                "See data\\logs\\error.log.", "ERROR")
            return rc
        say(f"The portal stopped unexpectedly (code {rc}). Restarting in 5 seconds (Ctrl+C to cancel)...", "WARN")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            say("Stopped by user.")
            return 0


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build/heal the environment, then run the Conflict Check Portal.")
    ap.add_argument("--check", action="store_true", help="prepare everything but do not launch")
    ap.add_argument("--upgrade", action="store_true", help="force dependency upgrade")
    ap.add_argument("--reset-venv", action="store_true", help="delete and rebuild .venv")
    ap.add_argument("--no-restart", action="store_true", help="do not auto-restart on a crash")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    ap.add_argument("--port", type=int, help="port to serve on (default: .streamlit/config.toml, else 8501)")
    a = ap.parse_args(argv)
    try:
        step_python()
        step_dirs()
        step_alembic()
        created = step_venv(a.reset_venv)
        step_deps(created, a.upgrade)
        step_database()
        if a.check:
            say("Environment is ready (--check: not launching).")
            return 0
        return step_launch(a.port or _configured_port(), restart=not a.no_restart,
                           open_browser=not (a.no_browser or os.environ.get("CCP_NO_BROWSER")))
    except BootstrapError as exc:
        say(str(exc), "ERROR")
        return 1
    except KeyboardInterrupt:
        say("Cancelled.")
        return 130
    except Exception:
        say("Unexpected bootstrap failure:\n" + traceback.format_exc(), "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())

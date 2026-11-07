"""
smoke_test_launch.py - starts the REAL portal through bootstrap.py and proves it serves pages.

    python smoke_test_launch.py        (about 20 seconds)

Uses a throw-away data folder and a free port (so it never collides with a portal that is already running).
Checks: the server comes up healthy, serves the app page, bootstrap prints the URLs, opens the browser
(POSIX only - a fake browser records the URL), writes its log, shows no Traceback, and shuts down cleanly.
"""
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except Exception:
        return None, b""


data = Path(tempfile.mkdtemp(prefix="ccp_launch_"))
port = free_port()
log_file = data / "launch_output.log"
env = {k: v for k, v in os.environ.items() if not k.startswith("CCP_")}
env.update(CCP_DATA_DIR=str(data))      # no CCP_ADMIN_PASSWORD: exercise the real first-run path
cmd = [sys.executable, "bootstrap.py", "--port", str(port), "--no-restart"]
fake_out = data / "browser_opened.txt"
if IS_WIN:
    cmd.append("--no-browser")                                 # no way to fake a browser reliably on Windows CI
else:
    fake = data / "fakebrowser.sh"
    fake.write_text(f'#!/bin/sh\necho "$1" >> "{fake_out}"\n')
    fake.chmod(0o755)
    env["BROWSER"] = str(fake)

kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}
proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=open(log_file, "w", encoding="utf-8"), stderr=subprocess.STDOUT, **kw)
try:
    ok = False
    for _ in range(120):
        if proc.poll() is not None:
            break
        if get(f"http://127.0.0.1:{port}/_stcore/health")[1] == b"ok":
            ok = True
            break
        time.sleep(1)
    check("portal starts and reports healthy", ok, "bootstrap exited early: " + log_file.read_text(errors="replace")[-600:] if proc.poll() is not None else "timeout")
    status, body = get(f"http://localhost:{port}/")
    check("serves the app page over HTTP (200)", status == 200 and b"<html" in body.lower(), status)
    time.sleep(2)
    text = log_file.read_text(errors="replace")
    check("bootstrap printed the localhost URL", f"http://localhost:{port}" in text)
    check("first-run admin credentials were printed once", "FIRST-RUN ADMIN" in text)
    if not IS_WIN:
        end = time.time() + 15
        while time.time() < end and not fake_out.exists():
            time.sleep(0.5)
        check("browser is opened automatically at the portal URL",
              fake_out.exists() and f"http://localhost:{port}" in fake_out.read_text(), "BROWSER hook not called")
    check("bootstrap.log written under the data folder", (data / "logs" / "bootstrap.log").exists())
    check("proof/export/backup folders provisioned", all((data / d).is_dir() for d in ("proofs", "exports", "backups", "logs")))
finally:
    if proc.poll() is None:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGINT)
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        rc = None
        if not IS_WIN:
            os.killpg(proc.pid, signal.SIGKILL)

time.sleep(1)
text = log_file.read_text(errors="replace")
check("clean shutdown (exit code 0)" if not IS_WIN else "process stopped", IS_WIN or rc == 0, rc)
m = re.search(r"password:\s*(\S+)", text)
if m:
    probe = ("import auth; r = auth.authenticate('admin', %r); print('LOGIN_OK' if r.ok and r.user.must_change_password else 'LOGIN_FAIL')" % m.group(1))
    out = subprocess.run([sys.executable, "-c", probe], cwd=HERE, env=env, capture_output=True, text=True).stdout
    check("the printed first-run password really logs in (and forces a change)", "LOGIN_OK" in out, out[-200:])
else:
    check("the printed first-run password really logs in (and forces a change)", False, "no password found in output")
check("port is free again after shutdown", get(f"http://127.0.0.1:{port}/_stcore/health")[0] is None)
check("no Traceback in the launch output", "Traceback" not in text, text[text.find("Traceback"):][:400])
err = data / "logs" / "error.log"
check("error.log is empty (no warnings/errors during launch)", not err.exists() or err.read_text().strip() == "", err.read_text()[:300] if err.exists() else "")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

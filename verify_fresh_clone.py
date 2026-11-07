#!/usr/bin/env python3
"""
verify_fresh_clone.py - proves the repository works on a brand-new machine.

    python verify_fresh_clone.py                 # clone THIS repo, bootstrap it, run the quick tests
    python verify_fresh_clone.py --full          # ...and every test suite
    python verify_fresh_clone.py --source https://github.com/you/conflict-check-portal.git
    python verify_fresh_clone.py --keep          # keep the temp clone for inspection

Simulates a new PC: `git clone` into an empty temp folder (so anything that only exists on YOUR machine, such
as an ignored or untracked file, is missing), run `bootstrap.py --check` (creates .venv, installs packages,
provisions folders, migrates the database), then run the smoke tests with that clone's own .venv.
Standard library only. Needs `git` and internet access (for pip).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERBOSE_TAIL = 25


def sh(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="repo URL or path (default: this folder)")
    ap.add_argument("--full", action="store_true", help="run every suite, not just the quick ones")
    ap.add_argument("--keep", action="store_true", help="do not delete the temp clone")
    a = ap.parse_args()
    if shutil.which("git") is None:
        print("ERROR: git is not installed / not on PATH.")
        return 2
    source = a.source or str(HERE)
    if not a.source:
        st = sh(["git", "-C", HERE, "status", "--porcelain"], capture_output=True, text=True)
        if st.returncode != 0:
            print("ERROR: this folder is not a git repository. Run `git init`, commit, then retry (or pass --source).")
            return 2
        if st.stdout.strip():
            print("NOTE: you have uncommitted changes. A clone only contains COMMITTED files, so commit first to test them:\n"
                  + "\n".join("   " + ln for ln in st.stdout.strip().splitlines()[:10]) + "\n")
    tmp = Path(tempfile.mkdtemp(prefix="ccp_fresh_clone_"))
    clone = tmp / "conflict-check-portal"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCP_")}
    env.update(PYTHONUTF8="1", CCP_NO_BROWSER="1", CCP_ADMIN_PASSWORD="Verify#Clone2026x")   # chosen here, so nothing sensitive is printed
    code = 1
    try:
        print(f"[1/3] git clone {source}\n      -> {clone}")
        r = sh(["git", "clone", "--quiet", source, clone], capture_output=True, text=True)
        if r.returncode != 0:
            print("ERROR: clone failed:\n" + r.stderr[-800:])
            return 2
        files = sh(["git", "-C", clone, "ls-files"], capture_output=True, text=True).stdout.split()
        print(f"      {len(files)} files in the clone; data/, .venv/ and *.db are NOT among them: "
              f"{not any(f.startswith(('data/', '.venv/')) or f.endswith('.db') for f in files)}")

        print("[2/3] python bootstrap.py --check   (creates .venv, installs packages, folders, database)")
        r = sh([sys.executable, "bootstrap.py", "--check"], cwd=clone, env=env, capture_output=True, text=True,
               encoding="utf-8", errors="replace")
        tail = "\n".join(r.stdout.splitlines()[-VERBOSE_TAIL:])
        if r.returncode != 0:
            print("BOOTSTRAP FAILED:\n" + tail + "\n" + r.stderr[-800:])
            return 1
        print("      bootstrap OK. Last lines:\n" + "\n".join("        " + ln for ln in tail.splitlines()[-6:]))

        print(f"[3/3] run_tests.py {'(all suites)' if a.full else '--quick'}  using the clone's own .venv")
        vpy = clone / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        args = [vpy, "run_tests.py"] + ([] if a.full else ["--quick"])
        r = sh(args, cwd=clone, env=env, text=True, encoding="utf-8", errors="replace")
        code = r.returncode
        print("\nRESULT: " + ("fresh clone works end to end." if code == 0 else "the fresh clone has problems (see above)."))
        return code
    finally:
        if a.keep:
            print(f"(kept: {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

"""
run_tests.py - one command to run every automated check.

    python run_tests.py            # everything, ~300 checks (about 1-2 minutes): install, launch, part 1, part 2, part 3
    python run_tests.py --quick    # install + launch only (about 15 seconds)

If a .venv exists next to this file, the tests automatically run INSIDE it (so `python run_tests.py` from a
system Python still tests the real environment). Every suite uses a throw-away data folder.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VPY = HERE / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SUITES = [("install", "smoke_test_install.py", 300), ("launch", "smoke_test_launch.py", 300),
          ("part 1: database/auth/audit/backup", "smoke_test_part1.py", 900),
          ("part 2: Streamlit UI", "smoke_test_part2.py", 1500),
          ("part 3: workflow/documents/excel", "smoke_test_part3.py", 1800)]
QUICK = {"install", "launch"}


def main() -> int:
    if VPY.exists() and Path(sys.prefix).resolve() != (HERE / ".venv").resolve() and not os.environ.get("CCP_TESTS_REEXEC"):
        return subprocess.call([str(VPY), str(Path(__file__).resolve()), *sys.argv[1:]],
                               env=dict(os.environ, CCP_TESTS_REEXEC="1"), cwd=HERE)
    quick = "--quick" in sys.argv
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env = {k: v for k, v in env.items() if k not in ("CCP_DATA_DIR", "CCP_ADMIN_PASSWORD")}
    bad = 0
    print(f"Python {sys.version.split()[0]} | {HERE}\n")
    for name, script, limit in SUITES:
        if quick and name not in QUICK:
            continue
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, script], cwd=HERE, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=limit)
            out, rc = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as exc:
            out, rc = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""), -1
            out += f"\nTIMEOUT after {limit}s"
        m = re.findall(r"(\d+) passed, (\d+) failed", out)
        summary = f"{m[-1][0]} passed, {m[-1][1]} failed" if m else "no result line"
        ok = rc == 0 and m and m[-1][1] == "0"
        bad += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:<38} {summary:<22} {time.time() - t0:5.0f}s")
        if not ok:
            for line in [ln for ln in out.splitlines() if ln.startswith("FAIL") or "Traceback" in ln or "Error" in ln][:15]:
                print("        " + line[:200])
    print("\n" + ("ALL SUITES PASSED" if not bad else f"{bad} SUITE(S) FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

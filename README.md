# Enterprise Conflict Check & Client Acceptance Portal

Streamlit + SQLite portal for conflict checks and client/vendor acceptance: role-based access, service-line
isolation, a tamper-evident audit trail, document verification, partner alignment sign-off, PDF approval
certificates, fuzzy duplicate screening and a live Excel tracker.

## Quick start (Windows)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**.
2. Clone or extract this repository.
3. Double-click **`run_portal.bat`**.

That is all. The first run (a few minutes, needs internet) creates `.venv`, upgrades pip, installs
`requirements.txt`, creates the data folders, builds the database and starts the portal. Your browser opens at
**<http://localhost:8501>**. The console prints the LAN address for colleagues and **the first-run admin
password (shown once; copy it)**. Later starts take about two seconds.

Linux / macOS: `python3 bootstrap.py`

## What `bootstrap.py` does on every start (all steps are safe to repeat)

| Step | Behaviour |
|---|---|
| Python | Requires 3.10+, refuses to run from inside a zip |
| Folders | Creates `data/`, `data/proofs`, `data/exports`, `data/logs`, `data/backups`, `data/migration_reports` |
| Alembic | Repairs `alembic.ini` / `alembic/` if damaged or missing (never overwrites healthy files) |
| `.venv` | Creates it; rebuilds it if it is broken (e.g. copied from another PC) |
| Packages | Upgrades pip, installs `requirements.txt` when it changed; works offline if already installed |
| Database | Integrity check (never deletes data), then create/upgrade schema with an automatic backup first |
| Launch | Starts Streamlit from `.venv`, opens the browser, restarts it after a crash |

Options: `--check` (prepare only) · `--upgrade` · `--reset-venv` · `--port 8600` · `--no-restart` · `--no-browser`

## Folder layout

```
app.py  workflow.py  auth.py  audit.py  database.py  config.py     application
backup.py  migration.py  matching.py  certificate.py  excel_sync.py   application
bootstrap.py  run_portal.bat  requirements.txt                     one-click launcher
alembic.ini  alembic/                                              database migrations
.streamlit/config.toml                                             server settings (port 8501, 25 MB uploads)
run_tests.py  smoke_test_*.py  verify_fresh_clone.py               automated tests
.github/workflows/ci.yml                                           CI: Linux + Windows, Python 3.10 + 3.12
data/                                                              created at run time, NEVER committed
```

## Tests

```
python run_tests.py --quick     # install integrity + real launch (about 15 s)
python run_tests.py             # everything: ~300 checks (about 1-2 minutes)
python verify_fresh_clone.py    # git-clones this repo into a temp folder and proves it works from scratch
```

| Suite | Verifies |
|---|---|
| `smoke_test_install.py` | Python and packages, files at the repo root, dependency coverage, module integrity, Alembic config and migrations (app + CLI), database connectivity |
| `smoke_test_launch.py` | The real portal starts via `bootstrap.py`, serves HTTP, opens the browser, stops cleanly |
| `smoke_test_part1/2/3.py` | Database/auth/audit/backup, Streamlit UI, workflow/documents/certificates/Excel |

All tests use throw-away temp folders and never touch `data/`.

## Configuration (optional environment variables)

| Variable | Purpose |
|---|---|
| `CCP_DATA_DIR` | Where `app.db`, logs, proofs live. **Use a local disk**, never OneDrive or a network share |
| `CCP_BACKUP_DIR` | Backup target (a OneDrive/NAS folder is fine here) |
| `CCP_PROOF_ROOT` | Proof documents folder |
| `CCP_LINK_ROOT` | Path prefix that Excel users can open for proof hyperlinks (e.g. `\\server\ccp_proofs`) |
| `CCP_ADMIN_PASSWORD` | Choose the first admin's password instead of a generated one |
| `CCP_NO_BROWSER` / `CCP_NO_PAUSE` | Don't open the browser / don't pause at the end of `run_portal.bat` |

Open the firewall once so colleagues can connect (Administrator prompt):
`netsh advfirewall firewall add rule name="CCP Portal" dir=in action=allow protocol=TCP localport=8501`

## Security notes

* Keep this repository **private**. `.gitignore` excludes `data/`, `.venv/`, databases, spreadsheets and logs:
  client documents and the database must never be committed.
* All activity is recorded in a hash-chained audit log; verify it under *Diagnostics*.
* Backups: `python backup.py run | list | verify <file> | restore <file>` (stop the portal before restoring).

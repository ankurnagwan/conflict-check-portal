"""
config.py - single source of truth for paths, security settings, domain constants
and logging. Every other module imports from here; nothing else hard-codes a
path, role name or status string.

Override locations with environment variables (no code edits needed):
    CCP_DATA_DIR     where app.db, logs, proofs live   (LOCAL disk, never OneDrive/UNC)
    CCP_PROOF_ROOT   proof document root               (default: <DATA_DIR>/proofs)
    CCP_BACKUP_DIR   backup target                     (point this at OneDrive/NAS)
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "Enterprise Conflict Check & Client Acceptance Portal"
APP_VERSION = "1.0.0-part3"

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CCP_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "app.db"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"
PROOF_ROOT = Path(os.environ.get("CCP_PROOF_ROOT", DATA_DIR / "proofs")).resolve()
EXPORT_DIR = DATA_DIR / "exports"
REPORT_DIR = DATA_DIR / "migration_reports"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = Path(os.environ.get("CCP_BACKUP_DIR", DATA_DIR / "backups")).resolve()

IST = ZoneInfo("Asia/Kolkata")  # store UTC, display IST (needs `tzdata` on Windows)


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROOF_ROOT, EXPORT_DIR, REPORT_DIR, LOG_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def storage_warnings() -> list[str]:
    """Warn if app.db sits somewhere SQLite is known to corrupt."""
    p = str(DB_PATH).lower()
    out: list[str] = []
    if p.startswith("\\\\") or p.startswith("//"):
        out.append("app.db is on a network share (UNC path): SQLite file locking is unreliable there. Use a local disk.")
    if any(k in p for k in ("onedrive", "sharepoint", "dropbox", "google drive", "googledrive")):
        out.append("app.db is inside a cloud-synced folder: corruption risk. Keep it on a local disk; back up INTO the cloud folder.")
    return out


# --------------------------------------------------------------------------- database
SQLITE_BUSY_TIMEOUT_MS = 15_000

# --------------------------------------------------------------------------- security
BCRYPT_ROUNDS = 12
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_BYTES = 72          # bcrypt hard limit
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
SESSION_IDLE_MINUTES = 30        # enforced by the Part 2 UI
ALLOW_SELF_APPROVAL = False      # maker-checker: requester may never approve own check

# --------------------------------------------------------------------------- audit
AUDIT_GENESIS_HASH = "0" * 64

# --------------------------------------------------------------------------- roles
ROLE_ADMIN = "Admin"
ROLE_RISK_LEAD = "Risk Lead"
ROLE_MEMBER = "Service Line Member"
ROLE_AUDITOR = "Auditor"
ROLES = (ROLE_ADMIN, ROLE_RISK_LEAD, ROLE_MEMBER, ROLE_AUDITOR)
# Roles that see every service line. Only Service Line Members are restricted.
ALL_LINES_ROLES = frozenset({ROLE_ADMIN, ROLE_RISK_LEAD, ROLE_AUDITOR})

# --------------------------------------------------------------------------- domain
SCOPES = ("Domestic", "Global")
PARTY_TYPES = ("Client", "Vendor")
DECISIONS = ("APPROVED", "PARTNER_ALIGNMENT_NEEDED")

CONFLICT_STATUSES = (
    "DRAFT", "SUBMITTED", "APPROVED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED",
    "CLEARED", "REJECTED", "WITHDRAWN", "EXPIRED",
)
# Part 3 enforces this map. "APPROVED" and "CLEARED" both unlock Client Acceptance.
CONFLICT_TRANSITIONS = {
    "DRAFT": {"SUBMITTED", "WITHDRAWN"},
    "SUBMITTED": {"APPROVED", "ALIGNMENT_PENDING", "REJECTED", "WITHDRAWN", "DRAFT"},
    "ALIGNMENT_PENDING": {"ALIGNMENT_CONFIRMED", "REJECTED", "WITHDRAWN"},
    "ALIGNMENT_CONFIRMED": {"CLEARED", "REJECTED"},
    "APPROVED": {"EXPIRED"},
    "CLEARED": {"EXPIRED"},
    "REJECTED": set(),
    "WITHDRAWN": set(),
    "EXPIRED": set(),
}
ACCEPTANCE_UNLOCKED_STATES = frozenset({"APPROVED", "CLEARED"})
ACCEPTANCE_STATUSES = ("LOCKED", "PENDING", "ACCEPTED", "DECLINED", "NOT_TRACKED")  # NOT_TRACKED = legacy imports

# Seed only. Admin can rename/deactivate in the UI. Codes are permanent (used in reference numbers).
DEFAULT_SERVICE_LINES = [
    ("AUD", "Audit & Assurance"), ("DTX", "Direct Tax"), ("ITX", "Indirect Tax"),
    ("ADV", "Advisory"), ("FOR", "Forensic"), ("RSK", "Risk Advisory"),
    ("TXN", "Transaction Advisory"), ("VAL", "Valuation"), ("TPR", "Transfer Pricing"),
    ("OTH", "Other"),
]

# --------------------------------------------------------------------------- proofs
MAX_UPLOAD_MB = 25
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx", ".msg", ".eml"}

REQUIRE_DOCS_ON_SUBMIT = True     # all REQUIRED documents must be attached before a check can be submitted
REQUIRE_ID_ON_SUBMIT = True       # name-only matching is weak: need >=1 identifier to submit

# Document keys -> human label. APPROVAL_CERTIFICATE is system-generated only.
DOC_LABELS = {
    "PAN": "PAN card", "TAN": "TAN allotment letter", "GST": "GST registration certificate",
    "MSME": "MSME / Udyam certificate", "COI": "Certificate of Incorporation (COI)",
    "CANCELLED_CHEQUE": "Cancelled cheque", "VENDOR_QUOTE": "Vendor quotation",
    "TRC": "Tax Residency Certificate (TRC)", "SIGNED_EL": "Signed Engagement Letter (EL)",
    "SIGNED_PO_AGREEMENT": "Signed PO / Agreement", "ALIGNMENT_PROOF": "Partner alignment proof",
    "OTHER": "Other supporting document", "APPROVAL_CERTIFICATE": "Approval certificate (system)",
}
# (party_type, scope) -> [(doc_key, required)].  "Global" = international. EDIT TO MATCH FIRM POLICY.
DOC_REQUIREMENTS = {
    ("Client", "Domestic"): [("PAN", True), ("TAN", True), ("GST", True), ("MSME", False), ("COI", True), ("SIGNED_EL", True)],
    ("Client", "Global"):   [("TRC", True), ("COI", True), ("SIGNED_EL", True)],
    ("Vendor", "Domestic"): [("PAN", True), ("TAN", False), ("GST", True), ("MSME", False), ("COI", True),
                             ("CANCELLED_CHEQUE", True), ("VENDOR_QUOTE", True), ("SIGNED_PO_AGREEMENT", True)],
    ("Vendor", "Global"):   [("TRC", True), ("COI", True), ("CANCELLED_CHEQUE", True), ("VENDOR_QUOTE", True),
                             ("SIGNED_PO_AGREEMENT", True)],
}
MULTI_DOC_TYPES = {"OTHER", "ALIGNMENT_PROOF"}   # these may hold several files; every other type is replace-on-upload
GST_ADDRESS_WARNING = ("Billing will be made on the GST Address so kindly ensure that your EL address and GST address "
                       "are identical to avoid rejection. Please perform a final check of your documents before submitting.")
FINAL_REVIEW_TEXT = ("I have performed a final check of all details and documents, and confirm they are accurate, "
                     "complete and that the EL address and GST address are identical.")

# --------------------------------------------------------------------------- fuzzy matching (rapidfuzz)
FUZZY_THRESHOLD = 85      # % similarity that raises a "possible match"
FUZZY_HIGH = 95           # % at/above which it is flagged "likely duplicate"
FUZZY_MIN_LEN = 3         # ignore names shorter than this after normalisation

# --------------------------------------------------------------------------- Excel sync
EXCEL_MASTER_NAME = "Master_Conflict_Tracker.xlsx"
EXCEL_DIR = EXPORT_DIR
EXCEL_PER_LINE = True                         # also write one workbook per service line (isolation-friendly)
EXCEL_DEBOUNCE_SECONDS = 2.0                  # coalesce bursts of saves into one rebuild
EXCEL_RETRY_SECONDS = 30                      # retry delay when the file is locked (open in Excel)
EXCEL_REPLACE_ATTEMPTS = 4                    # quick in-line retries of the atomic swap before queueing a retry
EXCEL_REPLACE_DELAY = 1.0
# Path prefix that Excel users see for proof files (e.g. a shared folder \\server\ccp_proofs).
EXCEL_LINK_ROOT = os.environ.get("CCP_LINK_ROOT") or str(PROOF_ROOT)

# --------------------------------------------------------------------------- backups
BACKUP_DAILY_KEEP_DAYS = 14
BACKUP_WEEKLY_KEEP_WEEKS = 8
BACKUP_MONTHLY_KEEP_MONTHS = 24
BACKUP_PRE_EVENT_KEEP_DAYS = 180   # "pre-migration", "pre-upgrade", "pre-restore"
BACKUP_INTERVAL_HOURS = 24


# --------------------------------------------------------------------------- logging
_LOGGING_READY = False


def setup_logging() -> None:
    """Idempotent. error.log gets WARNING+, console gets INFO+."""
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("ccp")
    root.setLevel(logging.INFO)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "error.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(ch)
    _LOGGING_READY = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger("ccp." + name.split(".")[-1])

"""
excel_sync.py - keeps Master_Conflict_Tracker.xlsx (plus one workbook per service line) in step with the database.

Design rules
  * The DATABASE is the source of truth; the Excel files are read-only OUTPUT. A save never waits for
    Excel and never fails because of Excel: workflow code only calls request_sync(), which returns at once.
  * One daemon worker thread. Bursts of saves are coalesced (EXCEL_DEBOUNCE_SECONDS) into one rebuild.
  * ATOMIC: the workbook is written to a temp file in the same folder and swapped in with os.replace(),
    so readers never see a half-written file.
  * LOCK-TOLERANT: on Windows, os.replace fails while someone has the file open in Excel. We retry briefly,
    then keep the change queued and retry every EXCEL_RETRY_SECONDS. Status is visible in Diagnostics.
  * Proof documents are written as =HYPERLINK("<EXCEL_LINK_ROOT>\\relative\\path","label") formulas. The DB stores
    RELATIVE paths only, so moving the proofs folder just means changing CCP_LINK_ROOT.
  * Cell text is forced to string type, so an entity named "=cmd|..." can never become a formula.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

import config
from database import (ConflictCheck, ProofDocument, ServiceLine, User, read_scope, to_ist)

log = config.get_logger("excel_sync")

_lock = threading.Lock()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_state = {"dirty": False, "running": False, "last_ok": None, "last_error": None, "last_attempt": None,
          "next_retry": 0.0, "runs": 0, "failures": 0, "records": None, "reason": ""}

DOC_COLUMNS = ["PAN", "TAN", "GST", "MSME", "COI", "CANCELLED_CHEQUE", "VENDOR_QUOTE", "TRC", "SIGNED_EL",
               "SIGNED_PO_AGREEMENT", "ALIGNMENT_PROOF", "OTHER"]
FILLS = {"APPROVED": "D1FAE5", "CLEARED": "D1FAE5", "REJECTED": "FEE2E2", "WITHDRAWN": "E5E7EB", "EXPIRED": "E5E7EB",
         "SUBMITTED": "FEF3C7", "ALIGNMENT_PENDING": "FFEDD5", "ALIGNMENT_CONFIRMED": "DBEAFE", "DRAFT": "F3F4F6"}


# ------------------------------------------------------------------ public API
def master_path() -> Path:
    return config.EXCEL_DIR / config.EXCEL_MASTER_NAME


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["thread_alive"] = bool(_thread and _thread.is_alive())
    s["master_file"] = str(master_path())
    s["master_mtime"] = datetime.fromtimestamp(master_path().stat().st_mtime) if master_path().exists() else None
    return s


def start() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="ccp-excel-sync", daemon=True)
        _thread.start()


def request_sync(reason: str = "", immediate: bool = False) -> None:
    """Non-blocking. Safe to call from any request; NEVER raises."""
    try:
        with _lock:
            _state["dirty"] = True
            _state["reason"] = reason
            if immediate:
                _state["next_retry"] = 0.0
        start()
        _wake.set()
    except Exception:
        log.exception("request_sync failed (ignored: database save is unaffected)")


def sync_now() -> dict:
    """Synchronous rebuild (used by the worker and by tests). Raises on failure."""
    rows, docs = _load()
    config.EXCEL_DIR.mkdir(parents=True, exist_ok=True)
    written = [_write_workbook(master_path(), rows, docs, "Master (all service lines)")]
    if config.EXCEL_PER_LINE:
        by_line: dict[str, list] = {}
        for r in rows:
            by_line.setdefault(r["sl_code"], []).append(r)
        for code, lines in by_line.items():
            written.append(_write_workbook(config.EXCEL_DIR / "by_service_line" / f"{code}_Conflict_Tracker.xlsx",
                                           lines, docs, f"Service line {code}"))
    return {"records": len(rows), "files": [str(p) for p in written]}


# ------------------------------------------------------------------ worker
def _loop() -> None:
    while True:
        _wake.wait(timeout=5)
        _wake.clear()
        with _lock:
            due = _state["dirty"] and time.time() >= _state["next_retry"]
        if not due:
            continue
        time.sleep(max(0.0, config.EXCEL_DEBOUNCE_SECONDS))      # let a burst of saves finish
        with _lock:
            _state.update(dirty=False, running=True, last_attempt=datetime.now())
        try:
            res = sync_now()
            with _lock:
                _state.update(running=False, last_ok=datetime.now(), last_error=None, records=res["records"],
                              runs=_state["runs"] + 1)
        except Exception as exc:                                  # locked file, disk full, permissions...
            log.warning("Excel sync failed (will retry in %ss): %s", config.EXCEL_RETRY_SECONDS, exc)
            with _lock:
                _state.update(running=False, dirty=True, last_error=f"{type(exc).__name__}: {exc}",
                              failures=_state["failures"] + 1, next_retry=time.time() + config.EXCEL_RETRY_SECONDS)
        with _lock:
            if _state["dirty"] and time.time() >= _state["next_retry"]:
                _wake.set()                                       # more changes arrived while building


# ------------------------------------------------------------------ data
def _ist(dt):
    return to_ist(dt).replace(tzinfo=None) if dt else None


def _load():
    with read_scope() as s:
        names = {u: n for u, n in ((r[0], r[1]) for r in s.execute(select(User.id, User.full_name)))}
        rows = []
        q = (select(ConflictCheck, ServiceLine.code, ServiceLine.name)
             .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
             .where(ConflictCheck.is_deleted.is_(False)).order_by(ConflictCheck.created_at.desc()))
        for c, code, sname in s.execute(q):
            rows.append({
                "id": c.id, "ref": c.reference_no, "sl_code": code, "sl": f"{code} - {sname}", "name": c.party_name,
                "ptype": c.party_type, "scope": c.scope, "country": c.country, "pan": c.pan, "gstin": c.gstin,
                "cin": c.cin, "tax_id": c.tax_id, "status": c.conflict_status, "decision": c.decision,
                "acceptance": c.acceptance_status, "raised_by": names.get(c.requested_by_id),
                "created": _ist(c.created_at), "decided_by": names.get(c.decided_by_id), "decided_at": _ist(c.decided_at),
                "partner": names.get(c.aligned_partner_id) or c.aligned_partner_name,
                "align_conf": _ist(c.alignment_confirmed_at), "cleared_by": names.get(c.cleared_by_id),
                "cleared_at": _ist(c.cleared_at), "accepted_at": _ist(c.accepted_at), "source": c.source})
        docs: dict[int, dict] = {}
        for p in s.scalars(select(ProofDocument).where(ProofDocument.is_deleted.is_(False)).order_by(ProofDocument.id)):
            slot = docs.setdefault(p.check_id, {})
            if p.doc_type == "Legacy Proof Link":
                slot["LEGACY"] = p.external_path
            else:
                slot[p.doc_type] = p.stored_relpath          # latest wins; OTHER / ALIGNMENT_PROOF show the newest
    return rows, docs


def _link(rel: Optional[str], label: str) -> Optional[str]:
    """=HYPERLINK formula, or None when the 255-char Excel limit would be exceeded."""
    if not rel:
        return None
    root = config.EXCEL_LINK_ROOT.rstrip("\\/")
    sep = "\\" if ("\\" in root or re.match(r"^[A-Za-z]:", root)) else "/"
    path = root + sep + rel.replace("/", sep).replace("\\", sep)
    if len(path) > 255:
        return None
    return f'=HYPERLINK("{path.replace(chr(34), chr(34) * 2)}","{label}")'


def _write_workbook(target: Path, rows: list[dict], docs: dict, title: str) -> Path:
    wb = Workbook()
    info = wb.active
    info.title = "README"
    now = datetime.now(config.IST)
    for i, (k, v) in enumerate([
            ("AUTO-GENERATED - DO NOT EDIT", "Any edits are overwritten at the next sync. The database is the source of truth."),
            ("Contents", title), ("Records", len(rows)), ("Generated (IST)", f"{now:%d %b %Y, %H:%M:%S}"),
            ("App version", config.APP_VERSION), ("Proof links resolve against", config.EXCEL_LINK_ROOT),
            ("Note", "Links need access to the proofs share; files open from the portal's proof store.")], start=1):
        a = info.cell(i, 1, k); info.cell(i, 2, v)
        a.font = Font(bold=True, color="B91C1C" if i == 1 else "000000")
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 34, 90

    ws = wb.create_sheet("Conflict Tracker")
    cols = [("Reference", "ref", 20), ("Service line", "sl", 24), ("Entity", "name", 38), ("Type", "ptype", 9),
            ("Scope", "scope", 10), ("Country", "country", 14), ("PAN", "pan", 13), ("GSTIN", "gstin", 18),
            ("CIN / LLPIN", "cin", 24), ("Tax ID", "tax_id", 16), ("Conflict status", "status", 20),
            ("Decision", "decision", 24), ("Acceptance", "acceptance", 14), ("Raised by", "raised_by", 20),
            ("Created (IST)", "created", 18), ("Decided by", "decided_by", 20), ("Decided at (IST)", "decided_at", 18),
            ("Aligned partner", "partner", 20), ("Partner sign-off (IST)", "align_conf", 20),
            ("Cleared by", "cleared_by", 20), ("Cleared at (IST)", "cleared_at", 18), ("Accepted at (IST)", "accepted_at", 18),
            ("Source", "source", 14)]
    link_cols = [("Approval certificate", "APPROVAL_CERTIFICATE")] + [(k.replace("_", " ").title(), k) for k in DOC_COLUMNS] \
                + [("Legacy proof", "LEGACY")]
    head_fill = PatternFill("solid", fgColor="1E3A5F")
    for j, (label, _k, w) in enumerate(cols, start=1):
        c = ws.cell(1, j, label); c.font = Font(bold=True, color="FFFFFF"); c.fill = head_fill
        ws.column_dimensions[get_column_letter(j)].width = w
    for j, (label, _k) in enumerate(link_cols, start=len(cols) + 1):
        c = ws.cell(1, j, label); c.font = Font(bold=True, color="FFFFFF"); c.fill = head_fill
        ws.column_dimensions[get_column_letter(j)].width = 17
    unlinkable = 0
    for i, r in enumerate(rows, start=2):
        for j, (_l, key, _w) in enumerate(cols, start=1):
            v = r.get(key)
            if key == "status" or key == "decision" or key == "acceptance":
                v = v.replace("_", " ").title() if v else v
            c = ws.cell(i, j, v)
            if isinstance(v, str):
                c.data_type = "s"                              # never let user text become a formula
            if isinstance(v, datetime):
                c.number_format = "dd-mmm-yyyy hh:mm"
        ws.cell(i, 11).fill = PatternFill("solid", fgColor=FILLS.get(r["status"], "FFFFFF"))
        for j, (label, key) in enumerate(link_cols, start=len(cols) + 1):
            rel = docs.get(r["id"], {}).get(key)
            if not rel:
                continue
            f = _link(rel, "Open") if key != "LEGACY" else _legacy_link(rel)
            if f:
                c = ws.cell(i, j, f); c.font = Font(color="0563C1", underline="single")
            else:
                unlinkable += 1
                c = ws.cell(i, j, rel); c.data_type = "s"       # path too long for HYPERLINK: show as text
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols) + len(link_cols))}{max(len(rows) + 1, 2)}"
    ws.row_dimensions[1].alignment = Alignment(wrap_text=True)
    if unlinkable:
        log.warning("%d proof path(s) exceed Excel's 255-char HYPERLINK limit in %s; written as text", unlinkable, target.name)
    wb.move_sheet("Conflict Tracker", offset=-1)
    wb.active = 0

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        wb.save(tmp)
        _atomic_replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _legacy_link(path: str) -> Optional[str]:
    if len(path) > 255:
        return None
    return f'=HYPERLINK("{path.replace(chr(34), chr(34) * 2)}","Legacy link")'


def _replace(src: Path, dst: Path) -> None:        # indirection so tests can simulate a locked file
    os.replace(src, dst)


def _atomic_replace(src: Path, dst: Path) -> None:
    attempts, delay = config.EXCEL_REPLACE_ATTEMPTS, config.EXCEL_REPLACE_DELAY
    for n in range(attempts):
        try:
            _replace(src, dst)
            return
        except PermissionError:                     # Windows: target is open in Excel
            if n == attempts - 1:
                raise
            time.sleep(delay)

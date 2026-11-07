"""
migration.py - import legacy Excel conflict-check sheets into app.db without losing history.

SAFE BY DEFAULT: runs as a DRY RUN. Nothing is written unless you pass --commit.

    python migration.py legacy.xlsx                          # dry run + CSV report
    python migration.py legacy.xlsx --sheet "2024" --commit --actor ankur

What it guarantees
  * Row-level validation: bad rows are REJECTED and reported; good rows still import.
  * Idempotent: every row gets a content fingerprint (name, service line, scope, date, IDs,
    status, legacy ref). Re-running (even on an edited copy of the file) never duplicates.
  * Nothing is lost: the complete original row is kept as JSON in conflict_checks.legacy_data,
    plus legacy_file / legacy_row. Proof hyperlinks become proof_documents rows (external_path).
  * Every imported record writes an audit-chain entry; the whole commit is ONE transaction.
  * A backup (label "pre-migration") is taken automatically before a real run.
  * A CSV report is ALWAYS written to data/migration_reports/.

>>> EDIT THE "MAPPING CONFIG" SECTION BELOW TO MATCH YOUR LEGACY COLUMNS <<<
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from sqlalchemy import select

import audit
import config
from database import (ConflictCheck, ProofDocument, ServiceLine, User, next_reference_no,
                      normalize_id, normalize_name, read_scope, utcnow, validate_identifier, write_scope)

log = config.get_logger("migration")

# =====================================================================================
# MAPPING CONFIG  -  edit me
# =====================================================================================
HEADER_ROW = 1            # 1-based row number that holds the column headers
SHEET_NAME: Optional[str] = None   # None = the active/first sheet

# canonical field -> possible legacy header names (case-insensitive, first match wins)
COLUMN_MAP: dict[str, list[str]] = {
    "party_name":             ["client name", "name of client", "party name", "client/vendor name", "name"],
    "party_type":             ["type", "client/vendor", "party type"],
    "service_line":           ["service line", "service", "department", "practice"],
    "scope":                  ["domestic/global", "domestic / global", "scope"],
    "pan":                    ["pan", "pan no", "pan number"],
    "gstin":                  ["gstin", "gst no", "gst number"],
    "cin":                    ["cin", "llpin", "cin/llpin"],
    "tax_id":                 ["tax id", "foreign tax id", "registration no"],
    "engagement_description": ["engagement", "engagement description", "nature of work", "scope of work"],
    "request_date":           ["date", "request date", "date of request"],
    "requested_by":           ["requested by", "requestor", "team member"],
    "status":                 ["status", "conflict status"],
    "approver":               ["approved by", "risk lead", "approver"],
    "approval_date":          ["approval date", "approved on", "date of approval"],
    "aligned_partner":        ["aligned partner", "partner aligned", "partner name"],
    "notes":                  ["remarks", "notes", "comments"],
    "proof_link":             ["proof", "proof link", "approval proof", "link"],
    "legacy_ref":             ["ref no", "reference", "sr no", "sl no"],
}
REQUIRED_FIELDS = ("party_name", "service_line")

# legacy status text (lower-case) -> (conflict_status, decision)
STATUS_MAP: dict[str, tuple[str, Optional[str]]] = {
    "approved":                  ("APPROVED", "APPROVED"),
    "cleared":                   ("CLEARED", "APPROVED"),
    "partner alignment needed":  ("ALIGNMENT_PENDING", "PARTNER_ALIGNMENT_NEEDED"),
    "aligned":                   ("CLEARED", "PARTNER_ALIGNMENT_NEEDED"),
    "pending":                   ("SUBMITTED", None),
    "rejected":                  ("REJECTED", None),
}
DEFAULT_STATUS = ("SUBMITTED", None)        # used only when the status cell is BLANK (reported as warning)

SCOPE_MAP = {"domestic": "Domestic", "india": "Domestic", "global": "Global",
             "international": "Global", "overseas": "Global"}
DEFAULT_SCOPE = "Domestic"                  # blank scope -> this, with a warning
PARTY_TYPE_MAP = {"client": "Client", "vendor": "Vendor", "supplier": "Vendor"}
DEFAULT_PARTY_TYPE = "Client"

# legacy service-line text (lower-case) -> service line CODE. Codes and names already match automatically.
SERVICE_LINE_ALIASES: dict[str, str] = {
    # "statutory audit": "AUD",
}
# =====================================================================================

DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%d-%b-%y"]
_HYPERLINK_RE = re.compile(r'=\s*HYPERLINK\(\s*"([^"]+)"', re.I)


class MigrationConfigError(Exception):
    pass


@dataclass
class Issue:
    row: int
    severity: str       # ERROR (rejected) / WARNING (imported) / SKIPPED (already imported) / IMPORTED
    party: str
    message: str


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_date(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        try:
            return from_excel(v)
        except Exception:
            return None
    s = str(v).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _jsonable(v: Any):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _resolve_columns(headers: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    cols: dict[str, int] = {}
    for field, aliases in COLUMN_MAP.items():
        for a in aliases:
            if a.lower() in headers:
                cols[field] = headers[a.lower()]
                break
    missing = [f for f in REQUIRED_FIELDS if f not in cols]
    if missing:
        raise MigrationConfigError(
            f"Required field(s) {missing} not found. Headers present in the sheet: {sorted(headers)}. "
            f"Add the right header names to COLUMN_MAP in migration.py.")
    unmapped = [h for h, i in headers.items() if i not in cols.values()]
    return cols, unmapped


def _extract_link(cell_raw, cell_val) -> Optional[str]:
    if cell_raw is None:
        return None
    if getattr(cell_raw, "hyperlink", None) is not None and cell_raw.hyperlink.target:
        return cell_raw.hyperlink.target
    if isinstance(cell_raw.value, str):
        m = _HYPERLINK_RE.match(cell_raw.value)
        if m:
            return m.group(1)
    v = _clean(cell_val.value if cell_val is not None else None)
    if v and (("\\" in v) or ("/" in v) or v.lower().startswith("http")):
        return v
    return None


def _build_sl_lookup(session) -> dict[str, tuple[int, str]]:
    lk: dict[str, tuple[int, str]] = {}
    for sl in session.scalars(select(ServiceLine)):
        lk[sl.code.lower()] = (sl.id, sl.code)
        lk[sl.name.lower()] = (sl.id, sl.code)
    for alias, code in SERVICE_LINE_ALIASES.items():
        if code.lower() in lk:
            lk[alias.lower()] = lk[code.lower()]
    return lk


def _parse_row(r: int, vals: dict[str, Any], link: Optional[str], raw_row: dict, sl_lookup, file_name: str):
    """Pure function: returns (parsed dict | None, [Issue])."""
    issues: list[Issue] = []
    name = _clean(vals.get("party_name"))
    label = name or "(blank)"
    if not name:
        return None, [Issue(r, "ERROR", label, "Party name is blank")]
    sl_txt = _clean(vals.get("service_line"))
    sl = sl_lookup.get((sl_txt or "").lower())
    if not sl:
        return None, [Issue(r, "ERROR", label, f"Service line {sl_txt!r} not recognised. Add it to SERVICE_LINE_ALIASES or create it in the app.")]

    scope_txt = _clean(vals.get("scope"))
    scope = SCOPE_MAP.get((scope_txt or "").lower())
    if scope is None:
        if scope_txt:
            return None, [Issue(r, "ERROR", label, f"Scope {scope_txt!r} unrecognised. Add it to SCOPE_MAP.")]
        scope = DEFAULT_SCOPE
        issues.append(Issue(r, "WARNING", label, f"Scope blank: defaulted to {DEFAULT_SCOPE}"))

    pt_txt = _clean(vals.get("party_type"))
    party_type = PARTY_TYPE_MAP.get((pt_txt or "").lower())
    if party_type is None:
        if pt_txt:
            return None, [Issue(r, "ERROR", label, f"Party type {pt_txt!r} unrecognised. Add it to PARTY_TYPE_MAP.")]
        party_type = DEFAULT_PARTY_TYPE

    st_txt = _clean(vals.get("status"))
    if st_txt is None:
        status, decision = DEFAULT_STATUS
        issues.append(Issue(r, "WARNING", label, f"Status blank: defaulted to {status}"))
    elif st_txt.lower() in STATUS_MAP:
        status, decision = STATUS_MAP[st_txt.lower()]
    else:
        return None, [Issue(r, "ERROR", label, f"Status {st_txt!r} unrecognised. Add it to STATUS_MAP.")]

    ids = {}
    for kind in ("pan", "gstin", "cin", "tax_id"):
        v = normalize_id(_clean(vals.get(kind)))
        ids[kind] = v
        if v and not validate_identifier(kind, v):
            issues.append(Issue(r, "WARNING", label, f"{kind.upper()} {v!r} is not a valid format (imported as-is)"))

    req_date = _parse_date(vals.get("request_date"))
    if _clean(vals.get("request_date")) and req_date is None:
        issues.append(Issue(r, "WARNING", label, f"Request date {vals.get('request_date')!r} unreadable (import date used)"))
    appr_date = _parse_date(vals.get("approval_date"))

    fp_src = [normalize_name(name), sl[1], scope, req_date.date().isoformat() if req_date else "",
              ids["pan"] or "", ids["gstin"] or "", ids["cin"] or "", ids["tax_id"] or "",
              status, _clean(vals.get("legacy_ref")) or ""]
    fp = hashlib.sha256(json.dumps(fp_src).encode("utf-8")).hexdigest()

    mapped = {k: _jsonable(v) for k, v in vals.items() if v not in (None, "")}
    parsed = dict(
        row=r, label=label, sl_id=sl[0], sl_code=sl[1], scope=scope, party_type=party_type, name=name,
        status=status, decision=decision, req_date=req_date, appr_date=appr_date, ids=ids,
        engagement=_clean(vals.get("engagement_description")), notes=_clean(vals.get("notes")),
        partner=_clean(vals.get("aligned_partner")), link=link, fingerprint=fp,
        legacy_data=json.dumps({"mapped": mapped, "raw": raw_row, "source_file": file_name}, default=str, ensure_ascii=False),
    )
    return parsed, issues


def _get_legacy_user(session) -> User:
    u = session.scalar(select(User).where(User.username == "legacy_import"))
    if u is None:
        import auth
        u = User(username="legacy_import", full_name="Legacy Import (system)", role=config.ROLE_AUDITOR,
                 is_active=False, password_hash=auth.hash_password(secrets.token_urlsafe(24) + "aA1!"),
                 must_change_password=True)
        session.add(u)
        session.flush()
    return u


def run_migration(path, *, sheet: Optional[str] = None, header_row: Optional[int] = None,
                  commit: bool = False, actor: str = "cli", check_links: bool = False) -> dict:
    p = Path(path)
    if not p.exists():
        raise MigrationConfigError(f"File not found: {p}")
    config.ensure_dirs()
    sheet = sheet or SHEET_NAME
    hdr = header_row or HEADER_ROW
    file_hash = _sha256_file(p)

    wb_v = load_workbook(p, data_only=True)
    wb_f = load_workbook(p, data_only=False)       # second load: formulas + hyperlinks
    ws_v = wb_v[sheet] if sheet else wb_v.active
    ws_f = wb_f[sheet] if sheet else wb_f.active
    headers, idx_to_header = {}, {}          # lower-case lookup / ORIGINAL header text (kept in legacy_data)
    for c in range(1, ws_v.max_column + 1):
        h = _clean(ws_v.cell(hdr, c).value)
        if h:
            headers[h.lower()] = c
            idx_to_header[c] = h
    cols, unmapped = _resolve_columns(headers)

    with read_scope() as s:
        sl_lookup = _build_sl_lookup(s)
        seen_fp = {x for x in s.scalars(select(ConflictCheck.legacy_fingerprint)
                                         .where(ConflictCheck.legacy_fingerprint.is_not(None)))}

    issues: list[Issue] = []
    to_import: list[dict] = []
    total = 0
    for r in range(hdr + 1, ws_v.max_row + 1):
        row_vals = {h: ws_v.cell(r, i).value for i, h in idx_to_header.items()}
        if all(v in (None, "") for v in row_vals.values()):
            continue
        total += 1
        vals = {f: ws_v.cell(r, i).value for f, i in cols.items()}
        link = None
        if "proof_link" in cols:
            link = _extract_link(ws_f.cell(r, cols["proof_link"]), ws_v.cell(r, cols["proof_link"]))
        raw = {h: _jsonable(v) for h, v in row_vals.items() if v not in (None, "")}
        parsed, row_issues = _parse_row(r, vals, link, raw, sl_lookup, p.name)
        issues.extend(row_issues)
        if parsed is None:
            continue
        if parsed["fingerprint"] in seen_fp:
            issues.append(Issue(r, "SKIPPED", parsed["label"], "Already imported (same fingerprint)"))
            continue
        if check_links and parsed["link"] and not parsed["link"].lower().startswith("http") \
                and not Path(parsed["link"]).exists():
            issues.append(Issue(r, "WARNING", parsed["label"], f"Proof link not reachable from this server: {parsed['link']}"))
        seen_fp.add(parsed["fingerprint"])
        to_import.append(parsed)

    summary = {
        "file": p.name, "sha256": file_hash, "mode": "COMMIT" if commit else "DRY RUN",
        "rows_read": total, "would_import" if not commit else "imported": len(to_import),
        "errors": sum(i.severity == "ERROR" for i in issues),
        "warnings": sum(i.severity == "WARNING" for i in issues),
        "skipped_duplicates": sum(i.severity == "SKIPPED" for i in issues),
        "unmapped_columns": unmapped,
    }

    if commit and to_import:
        import backup
        summary["backup"] = backup.create_backup(label="pre-migration")["file"]
        actor_obj = SimpleNamespace(id=None, username=f"migration:{actor}")
        with write_scope() as s:                        # ONE transaction: all or nothing
            legacy_user = _get_legacy_user(s)
            for d in to_import:
                year = d["req_date"].year if d["req_date"] else None
                rec = ConflictCheck(
                    reference_no=next_reference_no(s, d["sl_code"], year), service_line_id=d["sl_id"],
                    party_type=d["party_type"], scope=d["scope"], party_name=d["name"],
                    pan=d["ids"]["pan"], gstin=d["ids"]["gstin"], cin=d["ids"]["cin"], tax_id=d["ids"]["tax_id"],
                    engagement_description=d["engagement"], requested_by_id=legacy_user.id,
                    conflict_status=d["status"], decision=d["decision"], decided_at=d["appr_date"],
                    decision_notes=d["notes"], aligned_partner_name=d["partner"],
                    cleared_at=d["appr_date"] if d["status"] in ("APPROVED", "CLEARED") else None,
                    acceptance_status="NOT_TRACKED", source="legacy_import", legacy_file=p.name,
                    legacy_row=d["row"], legacy_fingerprint=d["fingerprint"], legacy_data=d["legacy_data"],
                    created_at=d["req_date"] or utcnow(),
                )
                s.add(rec)
                s.flush()
                if d["link"]:
                    s.add(ProofDocument(check_id=rec.id, doc_type="Legacy Proof Link",
                                        external_path=d["link"][:1000], uploaded_by_id=None))
                audit.log_event(s, user=actor_obj, action="LEGACY_RECORD_IMPORTED", entity_type="conflict_check",
                                entity_id=rec.id, new={**audit.model_snapshot(rec), "legacy_sha256": file_hash})
                issues.append(Issue(d["row"], "IMPORTED", d["label"], rec.reference_no))
            audit.log_event(s, user=actor_obj, action="LEGACY_IMPORT_COMPLETED", entity_type="migration",
                            entity_id=p.name, new={k: v for k, v in summary.items() if k != "unmapped_columns"})

    stamp = datetime.now(config.IST).strftime("%Y%m%d_%H%M%S")
    rpt = config.REPORT_DIR / f"migration_{'commit' if commit else 'dryrun'}_{stamp}.csv"
    with open(rpt, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: opens cleanly in Excel
        w = csv.writer(f)
        w.writerow(["sheet_row", "severity", "party", "message"])
        for i in sorted(issues, key=lambda x: (x.row, x.severity)):
            w.writerow([i.row, i.severity, i.party, i.message])
    summary["report_csv"] = str(rpt)
    log.info("Migration %s: %s", summary["mode"], summary)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import a legacy Excel conflict tracker (dry run unless --commit).")
    ap.add_argument("file")
    ap.add_argument("--sheet")
    ap.add_argument("--header-row", type=int)
    ap.add_argument("--commit", action="store_true", help="actually write to app.db")
    ap.add_argument("--actor", default="cli", help="your username, recorded in the audit trail")
    ap.add_argument("--check-links", action="store_true", help="warn about proof links this server cannot open")
    a = ap.parse_args(argv)
    try:
        res = run_migration(a.file, sheet=a.sheet, header_row=a.header_row, commit=a.commit,
                            actor=a.actor, check_links=a.check_links)
    except MigrationConfigError as exc:
        print("CONFIG ERROR:", exc)
        return 2
    print(json.dumps(res, indent=2, default=str))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

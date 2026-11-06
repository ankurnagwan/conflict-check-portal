"""
workflow.py - the business engine (no Streamlit in here, so every rule is testable headlessly).

State machine (config.CONFLICT_TRANSITIONS)
    DRAFT --submit--> SUBMITTED --Option A--> APPROVED ------------------.
                          |                                              +--> acceptance PENDING -> ACCEPTED/DECLINED
                          +--Option B--> ALIGNMENT_PENDING --partner signs off--> ALIGNMENT_CONFIRMED --clear--> CLEARED
                          +--reject--> REJECTED        (partner may also decline -> REJECTED)

Rules enforced HERE (not in the UI)
  * Submit needs: every required document, one identifier, and the final-review confirmation.
  * Maker-checker: whoever raised a check can never approve, align, clear, reject or accept it.
  * Option B: the aligned partner must be an active partner, not the requester, not the deciding Risk Lead,
    and ONLY that partner can sign off (auth.can_confirm_alignment).
  * Clearance (Option A, or Option B after sign-off) issues the PDF certificate in the SAME transaction:
    no certificate -> no clearance. Its SHA-256 is stored in proof_documents and an audit entry is written.
  * Every state change is one audit-chain entry written in the same transaction.
  * Files are written first, rows second; if the transaction fails the files are removed again.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import select

import audit
import auth
import certificate
import config
import excel_sync
import matching
from database import (AuditHead, ConflictCheck, ProofDocument, RelatedParty, RuleViolation, ServiceLine, User,
                      next_reference_no, normalize_id, normalize_name, read_scope, to_ist, utcnow,
                      validate_identifier, write_scope)

log = config.get_logger("workflow")


def pretty(v) -> str:
    return str(v).replace("_", " ").title() if v else ""


def ist_naive(dt: Optional[datetime]):
    return to_ist(dt).replace(tzinfo=None) if dt else None


class FormInvalid(RuleViolation):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ============================================================================ documents
_MAGIC = {".pdf": (b"%PDF",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
          ".docx": (b"PK",), ".xlsx": (b"PK",), ".msg": (b"\xd0\xcf\x11\xe0",)}


def required_docs(party_type: str, scope: str) -> list[tuple[str, bool]]:
    return list(config.DOC_REQUIREMENTS.get((party_type, scope), []))


def doc_label(key: str) -> str:
    return config.DOC_LABELS.get(key, key)


def _validate_uploads(uploads: Iterable[dict]) -> tuple[list[dict], list[str]]:
    clean, errs = [], []
    for u in uploads or []:
        key, fname, data = u.get("doc_type"), os.path.basename(str(u.get("filename") or "")), u.get("data") or b""
        label = doc_label(key)
        if key not in config.DOC_LABELS or key == "APPROVAL_CERTIFICATE":
            errs.append(f"'{key}' is not a valid document type."); continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in config.ALLOWED_UPLOAD_EXTENSIONS:
            errs.append(f"{label}: file type '{ext or fname}' is not allowed (allowed: {', '.join(sorted(config.ALLOWED_UPLOAD_EXTENSIONS))})."); continue
        if not data:
            errs.append(f"{label}: the file is empty."); continue
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errs.append(f"{label}: file is larger than {config.MAX_UPLOAD_MB} MB."); continue
        magic = _MAGIC.get(ext)
        if magic and not any(data.startswith(m) for m in magic):
            errs.append(f"{label}: the file content does not look like a real {ext} file."); continue
        clean.append({"doc_type": key, "filename": fname, "ext": ext, "data": data})
    return clean, errs


def _store_file(ref_no: str, doc_type: str, ext: str, data: bytes) -> tuple[str, str, int, Path]:
    """Generated file name (never the user's), atomic write, relative path returned for the DB."""
    rel_dir = f"{ref_no.split('/')[1]}/{ref_no.replace('/', '-')}"
    rel = f"{rel_dir}/{doc_type.lower()}_{uuid.uuid4().hex[:10]}{ext}"
    path = config.PROOF_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    return rel, hashlib.sha256(data).hexdigest(), len(data), path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _attach(s, user, chk: ConflictCheck, uploads: list[dict], written: list[Path]) -> None:
    for u in uploads:
        rel, sha, size, path = _store_file(chk.reference_no, u["doc_type"], u["ext"], u["data"])
        written.append(path)
        replaced = 0
        if u["doc_type"] not in config.MULTI_DOC_TYPES:           # replace-on-upload, old row kept (soft delete)
            for old in s.scalars(select(ProofDocument).where(
                    ProofDocument.check_id == chk.id, ProofDocument.doc_type == u["doc_type"],
                    ProofDocument.is_deleted.is_(False))):
                old.is_deleted = True
                replaced += 1
        p = ProofDocument(check_id=chk.id, doc_type=u["doc_type"], original_filename=u["filename"],
                          stored_relpath=rel, sha256=sha, size_bytes=size, uploaded_by_id=user.id)
        s.add(p)
        s.flush()
        audit.log_event(s, user=user, action="DOC_REPLACED" if replaced else "DOC_UPLOADED",
                        entity_type="conflict_check", entity_id=chk.id,
                        new={"doc_type": u["doc_type"], "filename": u["filename"], "sha256": sha,
                             "size_bytes": size, "proof_id": p.id})


def _active_doc_types(s, check_id: int) -> set[str]:
    return set(s.scalars(select(ProofDocument.doc_type).where(
        ProofDocument.check_id == check_id, ProofDocument.is_deleted.is_(False))))


def missing_required(party_type: str, scope: str, have: set[str]) -> list[str]:
    return [k for k, req in required_docs(party_type, scope) if req and k not in have]


def _gate_errors(*, party_type, scope, ids: tuple, have_docs: set[str], confirm_review: bool) -> list[str]:
    errs = []
    if not confirm_review:
        errs.append("Please tick the final review confirmation before submitting.")
    if config.REQUIRE_ID_ON_SUBMIT and not any(ids):
        errs.append("At least one identifier (PAN / GSTIN / CIN, or Tax ID for Global) is required to submit.")
    if config.REQUIRE_DOCS_ON_SUBMIT:
        miss = missing_required(party_type, scope, have_docs)
        if miss:
            errs.append("Required document(s) missing: " + ", ".join(doc_label(k) for k in miss) + ".")
    return errs


# ============================================================================ helpers
@contextmanager
def _tx():
    """One write transaction + cleanup of any files written if it fails."""
    written: list[Path] = []
    try:
        with write_scope() as s:
            yield s, written
    except BaseException:
        for p in written:
            try: p.unlink(missing_ok=True)
            except OSError: log.warning("Could not remove orphan file %s", p)
        raise


def _after_commit(reason: str) -> None:
    excel_sync.request_sync(reason)      # non-blocking, never raises


def _get_for_update(s, user, check_id: int, *, visibility: bool = False) -> ConflictCheck:
    base = select(ConflictCheck).where(ConflictCheck.id == check_id, ConflictCheck.is_deleted.is_(False))
    stmt = (auth.apply_check_visibility(base, user, ConflictCheck.service_line_id, ConflictCheck.aligned_partner_id)
            if visibility else auth.apply_service_line_scope(base, user, ConflictCheck.service_line_id))
    c = s.scalar(stmt)
    if c is None:
        raise auth.PermissionDenied("Record not found or not accessible.")
    return c


def _expect(c: ConflictCheck, allowed: set[str], what: str) -> None:
    if c.conflict_status not in allowed:
        raise FormInvalid([f"Cannot {what}: this check is {pretty(c.conflict_status)}."])


def _change(s, user, c: ConflictCheck, action: str, reason: Optional[str] = None, **fields) -> None:
    """Apply field changes + write ONE audit entry containing only what changed."""
    new_status = fields.get("conflict_status")
    if new_status and new_status not in config.CONFLICT_TRANSITIONS.get(c.conflict_status, set()):
        raise FormInvalid([f"A {pretty(c.conflict_status)} check cannot move to {pretty(new_status)}."])
    before = audit.model_snapshot(c)
    for k, v in fields.items():
        setattr(c, k, v)
    o, n = audit.diff_snapshots(before, audit.model_snapshot(c))
    audit.log_event(s, user=user, action=action, entity_type="conflict_check", entity_id=c.id,
                    old=o, new=n, reason=reason)


def _norm_fields(party_type, scope, name, pan, gstin, cin, tax_id, country):
    name = (name or "").strip()
    pan, gstin, cin, tax_id = (normalize_id(x) for x in (pan, gstin, cin, tax_id))
    errs = []
    if not name: errs.append("Entity name is required.")
    if len(name) > 300: errs.append("Entity name is too long (max 300 characters).")
    if party_type not in config.PARTY_TYPES: errs.append("Choose Client or Vendor.")
    if scope not in config.SCOPES: errs.append("Choose Domestic or Global.")
    country = (country or "").strip() or None
    if scope == "Domestic":
        tax_id, country = None, None
        for kind, val in (("pan", pan), ("gstin", gstin), ("cin", cin)):
            if val and not validate_identifier(kind, val):
                errs.append(f"{kind.upper()} '{val}' is not in a valid format.")
    else:
        pan = gstin = cin = None
        if not country: errs.append("Country is required for Global engagements.")
    return dict(party_type=party_type, scope=scope, party_name=name, pan=pan, gstin=gstin, cin=cin,
                tax_id=tax_id, country=country), errs


# ============================================================================ create / edit / submit
def create_check(user, *, service_line_id: int, party_type: str, scope: str, party_name: str, pan=None, gstin=None,
                 cin=None, tax_id=None, country=None, description=None, related: Optional[list[dict]] = None,
                 uploads: Optional[list[dict]] = None, submit: bool = False, confirm_review: bool = False) -> dict:
    auth.require(user, "create_check")
    auth.assert_service_line_access(user, service_line_id)
    f, errs = _norm_fields(party_type, scope, party_name, pan, gstin, cin, tax_id, country)
    clean_uploads, up_errs = _validate_uploads(uploads)
    errs += up_errs
    rel_rows = [{"name": str(r.get("name") or "").strip(), "relationship": str(r.get("relationship") or "").strip(),
                 "identifier": str(r.get("identifier") or "").strip()}
                for r in (related or []) if str(r.get("name") or "").strip()][:25]
    if submit:
        errs += _gate_errors(party_type=party_type, scope=scope, ids=(f["pan"], f["gstin"], f["cin"], f["tax_id"]),
                             have_docs={u["doc_type"] for u in clean_uploads}, confirm_review=confirm_review)
    if errs:
        raise FormInvalid(errs)

    with _tx() as (s, written):
        sl = s.get(ServiceLine, service_line_id)
        if sl is None or not sl.is_active:
            raise FormInvalid(["The selected service line is not available."])
        chk = ConflictCheck(reference_no=next_reference_no(s, sl.code), service_line_id=sl.id,
                            engagement_description=(description or "").strip() or None, requested_by_id=user.id,
                            conflict_status="DRAFT", acceptance_status="LOCKED", **f)
        s.add(chk)
        s.flush()
        for r in rel_rows:
            s.add(RelatedParty(check_id=chk.id, name=r["name"], relationship_type=r["relationship"] or None,
                               identifier=r["identifier"] or None))
        s.flush()
        matches = matching.find_matches(s, name=f["party_name"], related_names=[r["name"] for r in rel_rows],
                                        pan=f["pan"], gstin=f["gstin"], cin=f["cin"], tax_id=f["tax_id"], exclude_id=chk.id)
        audit.log_event(s, user=user, action="CHECK_CREATED", entity_type="conflict_check", entity_id=chk.id,
                        new={**audit.model_snapshot(chk), "related_parties": rel_rows,
                             "possible_matches": [{"ref": m["reference_no"], "score": m["score"]} for m in matches]})
        _attach(s, user, chk, clean_uploads, written)
        if submit:
            _change(s, user, chk, "CHECK_SUBMITTED", conflict_status="SUBMITTED")
            audit.log_event(s, user=user, action="SUBMISSION_ACKNOWLEDGED", entity_type="conflict_check",
                            entity_id=chk.id, new={"final_review_confirmed": True,
                                                   "gst_address_warning_shown": config.GST_ADDRESS_WARNING})
        result = {"id": chk.id, "reference_no": chk.reference_no, "status": chk.conflict_status,
                  "matches": matches, "documents": len(clean_uploads)}
    _after_commit("check created")
    return result


def attach_documents(user, check_id: int, uploads: list[dict]) -> int:
    """Maker adds/replaces documents on their own DRAFT; a Risk Lead may add alignment proof / other
    documents while the check is under review."""
    clean, errs = _validate_uploads(uploads)
    if errs:
        raise FormInvalid(errs)
    if not clean:
        raise FormInvalid(["Choose at least one file."])
    with _tx() as (s, written):
        c = _get_for_update(s, user, check_id)
        is_maker = c.requested_by_id == user.id
        if is_maker and c.conflict_status == "DRAFT":
            pass
        elif auth.can(user, "decide_conflict") and c.conflict_status in ("SUBMITTED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED") \
                and all(u["doc_type"] in config.MULTI_DOC_TYPES for u in clean):
            pass
        else:
            raise auth.PermissionDenied("Documents can only be added by the maker while the check is a draft "
                                        "(or as alignment proof / other documents by a Risk Lead during review).")
        _attach(s, user, c, clean, written)
    _after_commit("documents attached")
    return len(clean)


def update_draft(user, check_id: int, *, party_type, scope, party_name, pan=None, gstin=None, cin=None, tax_id=None,
                 country=None, description=None) -> str:
    f, errs = _norm_fields(party_type, scope, party_name, pan, gstin, cin, tax_id, country)
    if errs:
        raise FormInvalid(errs)
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        if c.requested_by_id != user.id:
            raise auth.PermissionDenied("Only the person who raised this check can edit it.")
        _expect(c, {"DRAFT"}, "edit")
        _change(s, user, c, "DRAFT_EDITED", engagement_description=(description or "").strip() or None, **f)
        ref = c.reference_no
    _after_commit("draft edited")
    return ref


def submit_check(user, check_id: int, *, confirm_review: bool) -> str:
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        if c.requested_by_id != user.id:
            raise auth.PermissionDenied("Only the person who raised this check can submit it.")
        _expect(c, {"DRAFT"}, "submit")
        errs = _gate_errors(party_type=c.party_type, scope=c.scope, ids=(c.pan, c.gstin, c.cin, c.tax_id),
                            have_docs=_active_doc_types(s, c.id), confirm_review=confirm_review)
        if errs:
            raise FormInvalid(errs)
        _change(s, user, c, "CHECK_SUBMITTED", conflict_status="SUBMITTED")
        audit.log_event(s, user=user, action="SUBMISSION_ACKNOWLEDGED", entity_type="conflict_check", entity_id=c.id,
                        new={"final_review_confirmed": True, "gst_address_warning_shown": config.GST_ADDRESS_WARNING})
        ref = c.reference_no
    _after_commit("check submitted")
    return ref


def withdraw_check(user, check_id: int, reason: str) -> str:
    if not (reason or "").strip():
        raise FormInvalid(["A reason is required to withdraw a check."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        if c.requested_by_id != user.id:
            raise auth.PermissionDenied("Only the person who raised this check can withdraw it.")
        _expect(c, {"DRAFT", "SUBMITTED"}, "withdraw")
        _change(s, user, c, "CHECK_WITHDRAWN", reason=reason.strip(), conflict_status="WITHDRAWN")
        ref = c.reference_no
    _after_commit("check withdrawn")
    return ref


def maker_action(user, check_id: int, new_status: str, reason: Optional[str] = None, confirmed: bool = False) -> str:
    if new_status == "SUBMITTED":
        return submit_check(user, check_id, confirm_review=confirmed)
    if new_status == "WITHDRAWN":
        return withdraw_check(user, check_id, reason or "")
    raise FormInvalid([f"Unsupported action: {new_status}"])


# ============================================================================ decisions
def review_eligibility(user, requested_by_id: int) -> tuple[bool, str]:
    if not auth.can(user, "decide_conflict"):
        return False, "Only a Risk Lead can review this check."
    try:
        auth.assert_maker_checker(requested_by_id, user.id)
    except auth.PermissionDenied as exc:
        return False, f"{exc} Another Risk Lead must review it."
    return True, "You are eligible to review this check."


def list_partner_candidates(exclude_ids: Iterable[int] = ()) -> list[dict]:
    ex = {i for i in exclude_ids if i}
    with read_scope() as s:
        return [{"id": u.id, "name": u.full_name, "username": u.username}
                for u in s.scalars(select(User).where(User.is_partner.is_(True), User.is_active.is_(True))
                                   .order_by(User.full_name)) if u.id not in ex]


def _issue_certificate(s, user, c: ConflictCheck, option: str, written: list[Path]) -> str:
    def person(uid):
        u = s.get(User, uid) if uid else None
        return {"name": u.full_name, "username": u.username, "role": u.role} if u else None

    sl = s.get(ServiceLine, c.service_line_id)
    docs = [{"label": doc_label(p.doc_type), "filename": p.original_filename, "sha256": p.sha256, "uploaded_at": p.uploaded_at}
            for p in s.scalars(select(ProofDocument).where(
                ProofDocument.check_id == c.id, ProofDocument.is_deleted.is_(False),
                ProofDocument.doc_type != "APPROVAL_CERTIFICATE").order_by(ProofDocument.id))]
    cert_no = f"{c.reference_no}-CERT"
    pdf = certificate.build_certificate({
        "reference_no": c.reference_no, "certificate_no": cert_no, "party_name": c.party_name,
        "party_type": c.party_type, "scope": c.scope, "country": c.country, "service_line": f"{sl.code} - {sl.name}",
        "identifiers": {"PAN": c.pan, "GSTIN": c.gstin, "CIN": c.cin, "Tax ID": c.tax_id},
        "outcome": c.conflict_status, "option": option, "requested_by": person(c.requested_by_id),
        "decided_by": person(c.decided_by_id), "aligned_partner": person(c.aligned_partner_id),
        "cleared_by": person(c.cleared_by_id), "created_at": c.created_at, "decided_at": c.decided_at,
        "alignment_requested_at": c.alignment_requested_at, "alignment_confirmed_at": c.alignment_confirmed_at,
        "cleared_at": c.cleared_at, "notes": c.clearance_notes or c.decision_notes, "alignment_notes": c.alignment_notes,
        "documents": docs, "audit_seq": s.get(AuditHead, 1).last_id + 1, "issued_at": utcnow()})
    rel, sha, size, path = _store_file(c.reference_no, "APPROVAL_CERTIFICATE", ".pdf", pdf)
    written.append(path)
    for old in s.scalars(select(ProofDocument).where(ProofDocument.check_id == c.id,
                                                     ProofDocument.doc_type == "APPROVAL_CERTIFICATE",
                                                     ProofDocument.is_deleted.is_(False))):
        old.is_deleted = True
    fname = f"Approval_Certificate_{c.reference_no.replace('/', '-')}.pdf"
    p = ProofDocument(check_id=c.id, doc_type="APPROVAL_CERTIFICATE", original_filename=fname, stored_relpath=rel,
                      sha256=sha, size_bytes=size, uploaded_by_id=user.id)
    s.add(p)
    s.flush()
    audit.log_event(s, user=user, action="CERTIFICATE_ISSUED", entity_type="conflict_check", entity_id=c.id,
                    new={"certificate_no": cert_no, "file": fname, "sha256": sha, "size_bytes": size, "proof_id": p.id})
    return sha


def decide_approve(user, check_id: int, notes: Optional[str] = None) -> dict:
    """OPTION A: direct clearance by the Risk Lead."""
    auth.require(user, "decide_conflict")
    with _tx() as (s, written):
        c = _get_for_update(s, user, check_id)
        _expect(c, {"SUBMITTED"}, "approve")
        auth.assert_maker_checker(c.requested_by_id, user.id)
        now, notes = utcnow(), (notes or "").strip() or None
        _change(s, user, c, "CHECK_APPROVED", reason=notes, conflict_status="APPROVED", decision="APPROVED",
                decided_by_id=user.id, decided_at=now, decision_notes=notes, cleared_at=now, cleared_by_id=user.id,
                clearance_notes=notes, acceptance_status="PENDING")
        sha = _issue_certificate(s, user, c, "A", written)
        result = {"reference_no": c.reference_no, "certificate_sha256": sha}
    _after_commit("check approved")
    return result


def _validate_partner(s, partner_id: int, c: ConflictCheck, deciding_user_id: int) -> User:
    p = s.get(User, partner_id)
    if p is None or not p.is_active or not p.is_partner:
        raise FormInvalid(["Choose an active user who is flagged as an eligible partner."])
    if p.id == c.requested_by_id:
        raise FormInvalid(["The person who raised the check cannot be its aligned partner."])
    if p.id == deciding_user_id:
        raise FormInvalid(["You cannot name yourself as the aligned partner."])
    return p


def decide_alignment(user, check_id: int, partner_id: int, notes: str) -> dict:
    """OPTION B: flag a specific partner who must sign off before the check can clear."""
    auth.require(user, "record_alignment")
    notes = (notes or "").strip()
    if not notes:
        raise FormInvalid(["Explain the potential conflict and what the partner must align on."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        _expect(c, {"SUBMITTED"}, "request alignment")
        auth.assert_maker_checker(c.requested_by_id, user.id)
        partner = _validate_partner(s, partner_id, c, user.id)
        now = utcnow()
        _change(s, user, c, "ALIGNMENT_REQUESTED", reason=notes, conflict_status="ALIGNMENT_PENDING",
                decision="PARTNER_ALIGNMENT_NEEDED", decided_by_id=user.id, decided_at=now, decision_notes=notes,
                aligned_partner_id=partner.id, aligned_partner_name=partner.full_name, alignment_requested_at=now)
        result = {"reference_no": c.reference_no, "partner": partner.full_name}
    _after_commit("alignment requested")
    return result


def confirm_alignment(user, check_id: int, notes: str) -> str:
    """The designated partner formally signs off (auth.can_confirm_alignment)."""
    notes = (notes or "").strip()
    if not notes:
        raise FormInvalid(["Sign-off notes are required."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id, visibility=True)
        if not auth.can_confirm_alignment(user, c.aligned_partner_id):
            raise auth.PermissionDenied("Only the designated aligned partner can sign off this alignment.")
        _expect(c, {"ALIGNMENT_PENDING"}, "sign off")
        _change(s, user, c, "ALIGNMENT_CONFIRMED", reason=notes, conflict_status="ALIGNMENT_CONFIRMED",
                alignment_confirmed_at=utcnow(), alignment_notes=notes)
        ref = c.reference_no
    _after_commit("alignment confirmed")
    return ref


def decline_alignment(user, check_id: int, reason: str) -> str:
    """The designated partner refuses to align: the conflict stands and the check is rejected."""
    reason = (reason or "").strip()
    if not reason:
        raise FormInvalid(["A reason is required to decline."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id, visibility=True)
        if not auth.can_confirm_alignment(user, c.aligned_partner_id):
            raise auth.PermissionDenied("Only the designated aligned partner can decline this alignment.")
        _expect(c, {"ALIGNMENT_PENDING"}, "decline")
        _change(s, user, c, "ALIGNMENT_DECLINED", reason=reason, conflict_status="REJECTED", alignment_notes=reason)
        ref = c.reference_no
    _after_commit("alignment declined")
    return ref


def clear_aligned_check(user, check_id: int, final_notes: str, uploads: Optional[list[dict]] = None) -> dict:
    """OPTION B final step: Risk Lead records final notes/proof and clears the partner-confirmed check."""
    auth.require(user, "decide_conflict")
    final_notes = (final_notes or "").strip()
    if not final_notes:
        raise FormInvalid(["Final clearance notes are required."])
    clean, errs = _validate_uploads([{**u, "doc_type": "ALIGNMENT_PROOF"} for u in (uploads or [])])
    if errs:
        raise FormInvalid(errs)
    with _tx() as (s, written):
        c = _get_for_update(s, user, check_id)
        _expect(c, {"ALIGNMENT_CONFIRMED"}, "clear")
        auth.assert_maker_checker(c.requested_by_id, user.id)
        if user.id == c.aligned_partner_id:
            raise auth.PermissionDenied("The aligned partner cannot also clear the check.")
        _attach(s, user, c, clean, written)
        now = utcnow()
        _change(s, user, c, "CHECK_CLEARED", reason=final_notes, conflict_status="CLEARED", cleared_at=now,
                cleared_by_id=user.id, clearance_notes=final_notes, acceptance_status="PENDING")
        sha = _issue_certificate(s, user, c, "B", written)
        result = {"reference_no": c.reference_no, "certificate_sha256": sha}
    _after_commit("check cleared")
    return result


def reject_check(user, check_id: int, reason: str) -> str:
    auth.require(user, "decide_conflict")
    if not (reason or "").strip():
        raise FormInvalid(["A reason is required to reject a check."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        _expect(c, {"SUBMITTED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED"}, "reject")
        auth.assert_maker_checker(c.requested_by_id, user.id)
        _change(s, user, c, "CHECK_REJECTED", reason=reason.strip(), conflict_status="REJECTED", decision_notes=reason.strip())
        ref = c.reference_no
    _after_commit("check rejected")
    return ref


def record_acceptance(user, check_id: int, accepted: bool, notes: str) -> str:
    auth.require(user, "grant_acceptance")
    if not (notes or "").strip():
        raise FormInvalid(["Notes are required to record the acceptance decision."])
    with _tx() as (s, _w):
        c = _get_for_update(s, user, check_id)
        if c.conflict_status not in config.ACCEPTANCE_UNLOCKED_STATES or c.acceptance_status != "PENDING":
            raise FormInvalid(["Client acceptance is not open for this check."])
        auth.assert_maker_checker(c.requested_by_id, user.id)
        _change(s, user, c, "ACCEPTANCE_RECORDED", reason=notes.strip(),
                acceptance_status="ACCEPTED" if accepted else "DECLINED", accepted_by_id=user.id,
                accepted_at=utcnow(), acceptance_notes=notes.strip())
        ref = c.reference_no
    _after_commit("acceptance recorded")
    return ref


# ============================================================================ matching visibility
def partition_matches(user, matches: list[dict]) -> tuple[list[dict], int]:
    """Role-limited view of screening results: details only for lines the user may see; the rest is just a count."""
    if user.service_line_ids is None:
        return matches, 0
    vis = [m for m in matches if m["service_line_id"] in user.service_line_ids]
    return vis, len(matches) - len(vis)


def screen_entity(user, *, name: str, related_names=(), pan=None, gstin=None, cin=None, tax_id=None,
                  exclude_id: Optional[int] = None) -> tuple[list[dict], int]:
    """Live screening used while typing on the Data Entry page."""
    if len(normalize_name(name)) < config.FUZZY_MIN_LEN and not any((pan, gstin, cin, tax_id)):
        return [], 0
    with read_scope() as s:
        m = matching.find_matches(s, name=name or "", related_names=related_names, pan=pan, gstin=gstin, cin=cin,
                                  tax_id=tax_id, exclude_id=exclude_id)
    return partition_matches(user, m)


# ============================================================================ detail / queue
def get_check_detail(user, check_id: int) -> Optional[dict]:
    with read_scope() as s:
        c = s.scalar(auth.apply_check_visibility(
            select(ConflictCheck).where(ConflictCheck.id == check_id, ConflictCheck.is_deleted.is_(False)),
            user, ConflictCheck.service_line_id, ConflictCheck.aligned_partner_id))
        if c is None:
            return None
        in_scope = user.service_line_ids is None or c.service_line_id in user.service_line_ids
        uids = {x for x in (c.requested_by_id, c.decided_by_id, c.aligned_partner_id, c.cleared_by_id, c.accepted_by_id) if x}
        names = {r[0]: r[1] for r in s.execute(select(User.id, User.full_name).where(User.id.in_(uids)))} if uids else {}
        sl = s.get(ServiceLine, c.service_line_id)
        d = {
            "id": c.id, "reference_no": c.reference_no, "service_line": f"{sl.code} - {sl.name}", "service_line_id": sl.id,
            "party_name": c.party_name, "party_type": c.party_type, "scope": c.scope, "country": c.country,
            "pan": c.pan, "gstin": c.gstin, "cin": c.cin, "tax_id": c.tax_id, "description": c.engagement_description,
            "status": c.conflict_status, "decision": c.decision, "acceptance": c.acceptance_status,
            "requested_by_id": c.requested_by_id, "requested_by": names.get(c.requested_by_id),
            "decided_by": names.get(c.decided_by_id), "decided_at": ist_naive(c.decided_at), "decision_notes": c.decision_notes,
            "aligned_partner_id": c.aligned_partner_id, "aligned_partner": names.get(c.aligned_partner_id) or c.aligned_partner_name,
            "alignment_requested_at": ist_naive(c.alignment_requested_at),
            "alignment_confirmed_at": ist_naive(c.alignment_confirmed_at), "alignment_notes": c.alignment_notes,
            "cleared_by": names.get(c.cleared_by_id), "cleared_at": ist_naive(c.cleared_at), "clearance_notes": c.clearance_notes,
            "accepted_by": names.get(c.accepted_by_id), "accepted_at": ist_naive(c.accepted_at), "acceptance_notes": c.acceptance_notes,
            "created_at": ist_naive(c.created_at), "source": c.source, "limited": not in_scope,
            "related": [], "docs": [], "checklist": [], "missing": [],
        }
        d["related"] = [{"Name": r.name, "Relationship": r.relationship_type, "Identifier": r.identifier}
                        for r in s.scalars(select(RelatedParty).where(RelatedParty.check_id == c.id))]
        if in_scope:
            uploaders = {r[0]: r[1] for r in s.execute(select(User.id, User.full_name))}
            docs = s.scalars(select(ProofDocument).where(ProofDocument.check_id == c.id,
                                                         ProofDocument.is_deleted.is_(False)).order_by(ProofDocument.id)).all()
            d["docs"] = [{"id": p.id, "key": p.doc_type,
                          "label": doc_label(p.doc_type) if p.doc_type != "Legacy Proof Link" else "Legacy proof link",
                          "filename": p.original_filename or p.external_path, "size": p.size_bytes, "sha256": p.sha256,
                          "uploaded_at": ist_naive(p.uploaded_at), "uploaded_by": uploaders.get(p.uploaded_by_id),
                          "external": bool(p.external_path and not p.stored_relpath)} for p in docs]
            have = {p["key"] for p in d["docs"]}
            d["checklist"] = [{"key": k, "label": doc_label(k), "required": req, "present": k in have}
                              for k, req in required_docs(c.party_type, c.scope)]
            d["missing"] = [x["label"] for x in d["checklist"] if x["required"] and not x["present"]]
        return d


def fetch_queue(user) -> pd.DataFrame:
    cols = ["id", "reference_no", "party_name", "service_line", "scope", "status", "action", "age_days", "raised_by"]
    rows: list[dict] = []
    now = datetime.now(config.IST).replace(tzinfo=None)
    with read_scope() as s:
        def add(action_fn, *conds, visibility=False):
            stmt = (select(ConflictCheck, ServiceLine.code, User.full_name)
                    .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
                    .join(User, User.id == ConflictCheck.requested_by_id)
                    .where(ConflictCheck.is_deleted.is_(False), *conds).order_by(ConflictCheck.created_at))
            stmt = (auth.apply_check_visibility(stmt, user, ConflictCheck.service_line_id, ConflictCheck.aligned_partner_id)
                    if visibility else auth.apply_service_line_scope(stmt, user, ConflictCheck.service_line_id))
            for c, code, rname in s.execute(stmt):
                rows.append({"id": c.id, "reference_no": c.reference_no, "party_name": c.party_name, "service_line": code,
                             "scope": c.scope, "status": pretty(c.conflict_status), "action": action_fn(c),
                             "age_days": (now - ist_naive(c.created_at)).days, "raised_by": rname})

        add(lambda c: "Finish & submit your draft", ConflictCheck.requested_by_id == user.id, ConflictCheck.conflict_status == "DRAFT")
        if auth.can(user, "decide_conflict"):
            own = lambda c, text: "Waiting for another Risk Lead (you raised it)" if c.requested_by_id == user.id else text
            add(lambda c: own(c, "Decide: Approve / Partner alignment / Reject"), ConflictCheck.conflict_status == "SUBMITTED")
            add(lambda c: own(c, "Clear & issue certificate"), ConflictCheck.conflict_status == "ALIGNMENT_CONFIRMED")
        if user.is_partner:
            add(lambda c: "Sign off alignment", ConflictCheck.aligned_partner_id == user.id,
                ConflictCheck.conflict_status == "ALIGNMENT_PENDING", visibility=True)
        if auth.can(user, "grant_acceptance"):
            add(lambda c: "Waiting for another Risk Lead (you raised it)" if c.requested_by_id == user.id
                else "Record client acceptance", ConflictCheck.acceptance_status == "PENDING",
                ConflictCheck.conflict_status.in_(list(config.ACCEPTANCE_UNLOCKED_STATES)))
    df = pd.DataFrame(rows, columns=cols)
    return df.drop_duplicates(subset=["id", "action"]).sort_values("age_days", ascending=False).reset_index(drop=True) if len(df) else df


# ============================================================================ document integrity / access
def _verify_file(p: ProofDocument) -> dict:
    if not p.stored_relpath:
        return {"ok": None, "reason": "External legacy link: integrity cannot be verified."}
    path = config.PROOF_ROOT / p.stored_relpath
    if not path.exists():
        return {"ok": False, "reason": "File is missing from the proof store.", "expected": p.sha256, "actual": None}
    actual = _sha256_file(path)
    return {"ok": actual == p.sha256, "reason": "" if actual == p.sha256 else "SHA-256 does not match the value recorded at upload.",
            "expected": p.sha256, "actual": actual}


def _doc_for_user(s, user, doc_id: int):
    p = s.get(ProofDocument, doc_id)
    if p is None or p.is_deleted:
        raise auth.PermissionDenied("Document not found.")
    c = s.scalar(auth.apply_service_line_scope(select(ConflictCheck).where(ConflictCheck.id == p.check_id),
                                               user, ConflictCheck.service_line_id))
    if c is None:
        raise auth.PermissionDenied("Document not found or not accessible.")
    return p, c


def verify_check_documents(user, check_id: int) -> list[dict]:
    """Recompute SHA-256 of every stored document and compare with the value recorded at upload. Audited."""
    with write_scope() as s:
        c = _get_for_update(s, user, check_id)
        out = []
        for p in s.scalars(select(ProofDocument).where(ProofDocument.check_id == c.id, ProofDocument.is_deleted.is_(False))):
            r = _verify_file(p)
            out.append({"id": p.id, "label": doc_label(p.doc_type), "filename": p.original_filename, **r})
        bad = [x["label"] for x in out if x["ok"] is False]
        audit.log_event(s, user=user, action="DOCS_VERIFIED", entity_type="conflict_check", entity_id=c.id,
                        new={"checked": len(out), "failed": bad})
        if bad:
            log.error("INTEGRITY FAILURE on %s: %s", c.reference_no, bad)
    return out


def read_document(user, doc_id: int) -> tuple[str, bytes, dict]:
    """Returns (filename, bytes, verification). Refuses to serve a file whose hash no longer matches."""
    with read_scope() as s:
        p, _c = _doc_for_user(s, user, doc_id)
        fname, rel, expected, ext_only = p.original_filename or "document", p.stored_relpath, p.sha256, not p.stored_relpath
    if ext_only:
        raise FormInvalid(["This is an external legacy link; open it from its original location."])
    data = (config.PROOF_ROOT / rel).read_bytes() if (config.PROOF_ROOT / rel).exists() else None
    if data is None:
        raise RuleViolation("The file is missing from the proof store. Contact an Admin.")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        log.error("INTEGRITY FAILURE serving document %s (%s)", doc_id, rel)
        raise RuleViolation("Integrity check FAILED: this file has been altered since upload and will not be served.")
    return fname, data, {"ok": True, "sha256": actual}


def log_document_download(user, doc_id: int) -> None:
    with write_scope() as s:
        p, c = _doc_for_user(s, user, doc_id)
        audit.log_event(s, user=user, action="DOC_DOWNLOADED", entity_type="conflict_check", entity_id=c.id,
                        new={"doc_type": p.doc_type, "filename": p.original_filename, "sha256": p.sha256})

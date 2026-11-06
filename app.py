"""
app.py - Streamlit front end for the Enterprise Conflict Check & Client Acceptance Portal (Part 2).

Run:   streamlit run app.py --server.address 0.0.0.0 --server.port 8501

Structure
    1. Service layer   plain functions (no Streamlit): fetch, create, transitions, matching.
                       Every one re-checks permissions and service-line scope itself, so the UI
                       can never be the only thing standing between a user and data.
    2. UI helpers      session, flash messages, filters, error handling.
    3. Pages           Dashboard, Data Entry, Records, Reports, Diagnostics, Administration.
    4. main()          login gate -> forced password change -> sidebar -> navigation.

Audit: all events go through audit.log_event() (the "log_audit_event" of the brief).
"""
from __future__ import annotations

import io
import json
import platform
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func, or_, select
from sqlalchemy.orm.exc import StaleDataError

import audit
import auth
import backup
import config
import database
from database import (AuditLog, ConflictCheck, DatabaseBusy, ProofDocument, RelatedParty, RuleViolation,
                      ServiceLine, User, next_reference_no, normalize_id, normalize_name, read_scope,
                      to_ist, validate_identifier, write_scope)

log = config.get_logger("app")

PENDING_STATES = ("SUBMITTED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED")
OVERDUE_DAYS = 5                      # open checks older than this are flagged
REQUIRE_ID_ON_SUBMIT = True           # name-only matching is weak: need >=1 identifier to submit
MAX_RECORDS_TABLE = 5000
SCOPE_COLORS = {"Domestic": "#2563eb", "Global": "#f59e0b"}
# What the maker (requester) may do themselves. Decisions are the Risk Lead's (Part 3).
MAKER_TRANSITIONS = {"SUBMITTED": {"DRAFT"}, "WITHDRAWN": {"DRAFT", "SUBMITTED"}}


def pretty(v) -> str:
    return str(v).replace("_", " ").title() if v else ""


# ============================================================================ 1. SERVICE LAYER
class FormInvalid(RuleViolation):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Filters:
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None     # exclusive
    scope: str = "All"
    statuses: tuple = ()
    sl_ids: tuple = ()
    search: str = ""


def _ist_day_start_utc(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=config.IST).astimezone(timezone.utc).replace(tzinfo=None)


def _ist_naive(dt: Optional[datetime]):
    return to_ist(dt).replace(tzinfo=None) if dt else None


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def allowed_service_lines(user: auth.SessionUser) -> list[tuple[int, str, str]]:
    """(id, code, name) of ACTIVE service lines this user may see. Scoped by the same rule as data."""
    with read_scope() as s:
        stmt = auth.apply_service_line_scope(select(ServiceLine.id, ServiceLine.code, ServiceLine.name)
                                             .where(ServiceLine.is_active.is_(True)), user, ServiceLine.id)
        return [tuple(r) for r in s.execute(stmt.order_by(ServiceLine.code))]


_COLS = ["id", "reference_no", "service_line", "party_type", "scope", "party_name", "pan", "gstin", "cin",
         "tax_id", "conflict_status", "decision", "acceptance_status", "requested_by", "created_at",
         "decided_at", "accepted_at", "source"]


def fetch_checks(user: auth.SessionUser, f: Filters, limit: Optional[int] = None) -> pd.DataFrame:
    """The ONLY list query. Service-line isolation is applied here at SQL level; UI filters can only
    narrow it further (they are AND-ed), never widen it."""
    with read_scope() as s:
        stmt = (select(ConflictCheck, ServiceLine.code, User.full_name)
                .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
                .join(User, User.id == ConflictCheck.requested_by_id)
                .where(ConflictCheck.is_deleted.is_(False)))
        stmt = auth.apply_service_line_scope(stmt, user, ConflictCheck.service_line_id)
        if f.start_utc: stmt = stmt.where(ConflictCheck.created_at >= f.start_utc)
        if f.end_utc: stmt = stmt.where(ConflictCheck.created_at < f.end_utc)
        if f.scope in config.SCOPES: stmt = stmt.where(ConflictCheck.scope == f.scope)
        if f.statuses: stmt = stmt.where(ConflictCheck.conflict_status.in_(f.statuses))
        if f.sl_ids: stmt = stmt.where(ConflictCheck.service_line_id.in_(f.sl_ids))
        q = (f.search or "").strip()
        if q:
            like, nq, ident = f"%{_like_escape(q)}%", normalize_name(q), normalize_id(q)
            conds = [ConflictCheck.reference_no.ilike(like, escape="\\"),
                     ConflictCheck.party_name.ilike(like, escape="\\"),
                     ConflictCheck.pan == ident, ConflictCheck.gstin == ident,
                     ConflictCheck.cin == ident, ConflictCheck.tax_id == ident]
            if nq:
                conds.append(ConflictCheck.normalized_name.like(f"%{_like_escape(nq)}%", escape="\\"))
            stmt = stmt.where(or_(*conds))
        stmt = stmt.order_by(ConflictCheck.created_at.desc(), ConflictCheck.id.desc())
        if limit: stmt = stmt.limit(limit)
        rows = [{
            "id": c.id, "reference_no": c.reference_no, "service_line": code, "party_type": c.party_type,
            "scope": c.scope, "party_name": c.party_name, "pan": c.pan, "gstin": c.gstin, "cin": c.cin,
            "tax_id": c.tax_id, "conflict_status": c.conflict_status, "decision": c.decision,
            "acceptance_status": c.acceptance_status, "requested_by": rname,
            "created_at": _ist_naive(c.created_at), "decided_at": _ist_naive(c.decided_at),
            "accepted_at": _ist_naive(c.accepted_at), "source": c.source,
        } for c, code, rname in s.execute(stmt)]
    df = pd.DataFrame(rows, columns=_COLS)
    for col in ("created_at", "decided_at", "accepted_at"):
        df[col] = pd.to_datetime(df[col])
    now_ist = datetime.now(config.IST).replace(tzinfo=None)
    df["age_days"] = (now_ist - df["created_at"]).dt.days if len(df) else pd.Series(dtype="float")
    return df


def find_exact_matches(s, *, norm_name: str, pan, gstin, cin, tax_id, limit: int = 20) -> list[dict]:
    """Firm-wide exact-match scan. DELIBERATELY NOT service-line scoped: a conflict check is only
    meaningful against the whole firm. What is DISPLAYED is role-limited by the caller.
    (Part 3 adds rapidfuzz fuzzy matching on top of this.)"""
    out: list[dict] = []
    conds = []
    if norm_name: conds.append(ConflictCheck.normalized_name == norm_name)
    for col, val in ((ConflictCheck.pan, pan), (ConflictCheck.gstin, gstin),
                     (ConflictCheck.cin, cin), (ConflictCheck.tax_id, tax_id)):
        if val: conds.append(col == val)
    if conds:
        for r in s.execute(select(ConflictCheck.reference_no, ConflictCheck.party_name, ServiceLine.code,
                                  ConflictCheck.conflict_status)
                           .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
                           .where(ConflictCheck.is_deleted.is_(False), or_(*conds)).limit(limit)):
            out.append({"reference_no": r[0], "party_name": r[1], "service_line": r[2],
                        "status": r[3], "matched_on": "Name / identifier"})
    if norm_name:
        for r in s.execute(select(ConflictCheck.reference_no, ConflictCheck.party_name, ServiceLine.code,
                                  ConflictCheck.conflict_status)
                           .join(RelatedParty, RelatedParty.check_id == ConflictCheck.id)
                           .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
                           .where(ConflictCheck.is_deleted.is_(False),
                                  RelatedParty.normalized_name == norm_name).limit(limit)):
            out.append({"reference_no": r[0], "party_name": r[1], "service_line": r[2],
                        "status": r[3], "matched_on": "Listed as related party"})
    return out


def create_check(user: auth.SessionUser, *, service_line_id: int, party_type: str, scope: str,
                 party_name: str, pan=None, gstin=None, cin=None, tax_id=None, country=None,
                 description=None, related: Optional[list[dict]] = None, submit: bool = False) -> dict:
    auth.require(user, "create_check")
    auth.assert_service_line_access(user, service_line_id)
    name = (party_name or "").strip()
    pan, gstin, cin, tax_id = (normalize_id(x) for x in (pan, gstin, cin, tax_id))
    errors: list[str] = []
    if not name: errors.append("Entity name is required.")
    if len(name) > 300: errors.append("Entity name is too long (max 300 characters).")
    if party_type not in config.PARTY_TYPES: errors.append("Choose Client or Vendor.")
    if scope not in config.SCOPES: errors.append("Choose Domestic or Global.")
    if scope == "Domestic":
        tax_id, country = None, None
        for kind, val in (("pan", pan), ("gstin", gstin), ("cin", cin)):
            if val and not validate_identifier(kind, val):
                errors.append(f"{kind.upper()} '{val}' is not in a valid format.")
    else:
        pan = gstin = cin = None
        if not (country or "").strip(): errors.append("Country is required for Global engagements.")
    if submit and REQUIRE_ID_ON_SUBMIT and not any((pan, gstin, cin, tax_id)):
        errors.append("At least one identifier (PAN / GSTIN / CIN, or Tax ID for Global) is required to submit.")
    rel_rows = [{"name": str(r.get("name") or "").strip(), "relationship": str(r.get("relationship") or "").strip(),
                 "identifier": str(r.get("identifier") or "").strip()}
                for r in (related or []) if str(r.get("name") or "").strip()][:25]
    if errors:
        raise FormInvalid(errors)

    with write_scope() as s:
        sl = s.get(ServiceLine, service_line_id)
        if sl is None or not sl.is_active:
            raise FormInvalid(["The selected service line is not available."])
        matches = find_exact_matches(s, norm_name=normalize_name(name), pan=pan, gstin=gstin, cin=cin, tax_id=tax_id)
        chk = ConflictCheck(reference_no=next_reference_no(s, sl.code), service_line_id=sl.id,
                            party_type=party_type, scope=scope, party_name=name, pan=pan, gstin=gstin,
                            cin=cin, tax_id=tax_id, country=(country or "").strip() or None,
                            engagement_description=(description or "").strip() or None,
                            requested_by_id=user.id, conflict_status="DRAFT", acceptance_status="LOCKED")
        s.add(chk)
        s.flush()
        for r in rel_rows:
            s.add(RelatedParty(check_id=chk.id, name=r["name"], relationship_type=r["relationship"] or None,
                               identifier=r["identifier"] or None))
        audit.log_event(s, user=user, action="CHECK_CREATED", entity_type="conflict_check", entity_id=chk.id,
                        new={**audit.model_snapshot(chk), "related_parties": rel_rows,
                             "possible_matches": [m["reference_no"] for m in matches]})
        if submit:
            chk.conflict_status = "SUBMITTED"
            audit.log_event(s, user=user, action="CHECK_SUBMITTED", entity_type="conflict_check",
                            entity_id=chk.id, old={"conflict_status": "DRAFT"},
                            new={"conflict_status": "SUBMITTED"})
        return {"id": chk.id, "reference_no": chk.reference_no, "status": chk.conflict_status, "matches": matches}


def maker_action(user: auth.SessionUser, check_id: int, new_status: str, reason: Optional[str] = None) -> str:
    """Submit a draft / withdraw. Only the person who raised the check (the maker)."""
    with write_scope() as s:
        stmt = auth.apply_service_line_scope(
            select(ConflictCheck).where(ConflictCheck.id == check_id, ConflictCheck.is_deleted.is_(False)),
            user, ConflictCheck.service_line_id)
        c = s.scalar(stmt)
        if c is None:
            raise auth.PermissionDenied("Record not found or not accessible.")
        if c.requested_by_id != user.id:
            raise auth.PermissionDenied("Only the person who raised this check can do that.")
        old = c.conflict_status
        if old not in MAKER_TRANSITIONS.get(new_status, set()) or new_status not in config.CONFLICT_TRANSITIONS.get(old, set()):
            raise FormInvalid([f"A {pretty(old)} check cannot be moved to {pretty(new_status)}."])
        if new_status == "WITHDRAWN" and not (reason or "").strip():
            raise FormInvalid(["A reason is required to withdraw a check."])
        if new_status == "SUBMITTED" and REQUIRE_ID_ON_SUBMIT and not any((c.pan, c.gstin, c.cin, c.tax_id)):
            raise FormInvalid(["Add at least one identifier before submitting (edit is coming in Part 3, create a new draft for now)."])
        c.conflict_status = new_status
        audit.log_event(s, user=user, action="CHECK_SUBMITTED" if new_status == "SUBMITTED" else "CHECK_WITHDRAWN",
                        entity_type="conflict_check", entity_id=c.id, old={"conflict_status": old},
                        new={"conflict_status": new_status}, reason=(reason or None))
        return c.reference_no


def review_eligibility(user: auth.SessionUser, requested_by_id: int) -> tuple[bool, str]:
    """Maker-checker gate. Part 3's Approve / Partner-Alignment buttons call exactly this."""
    if not auth.can(user, "decide_conflict"):
        return False, "Only a Risk Lead can review this check."
    try:
        auth.assert_maker_checker(requested_by_id, user.id)
    except auth.PermissionDenied as exc:
        return False, f"{exc} Another Risk Lead must review it."
    return True, "You are eligible to review this check (decision buttons arrive in Part 3)."


def get_check_detail(user: auth.SessionUser, check_id: int) -> Optional[dict]:
    with read_scope() as s:
        c = s.scalar(auth.apply_service_line_scope(
            select(ConflictCheck).where(ConflictCheck.id == check_id, ConflictCheck.is_deleted.is_(False)),
            user, ConflictCheck.service_line_id))
        if c is None:
            return None
        uids = {x for x in (c.requested_by_id, c.decided_by_id, c.aligned_partner_id, c.accepted_by_id) if x}
        names = {r[0]: r[1] for r in s.execute(select(User.id, User.full_name).where(User.id.in_(uids)))} if uids else {}
        sl = s.get(ServiceLine, c.service_line_id)
        rel = [{"Name": r.name, "Relationship": r.relationship_type, "Identifier": r.identifier}
               for r in s.scalars(select(RelatedParty).where(RelatedParty.check_id == c.id))]
        prf = [{"Type": p.doc_type, "File": p.original_filename or p.external_path or p.stored_relpath,
                "Uploaded": _ist_naive(p.uploaded_at)}
               for p in s.scalars(select(ProofDocument).where(ProofDocument.check_id == c.id,
                                                              ProofDocument.is_deleted.is_(False)))]
        return {
            "id": c.id, "reference_no": c.reference_no, "service_line": f"{sl.code} - {sl.name}",
            "party_name": c.party_name, "party_type": c.party_type, "scope": c.scope, "country": c.country,
            "pan": c.pan, "gstin": c.gstin, "cin": c.cin, "tax_id": c.tax_id,
            "description": c.engagement_description, "status": c.conflict_status, "decision": c.decision,
            "acceptance": c.acceptance_status, "requested_by_id": c.requested_by_id,
            "requested_by": names.get(c.requested_by_id), "decided_by": names.get(c.decided_by_id),
            "decided_at": _ist_naive(c.decided_at), "decision_notes": c.decision_notes,
            "aligned_partner": names.get(c.aligned_partner_id) or c.aligned_partner_name,
            "created_at": _ist_naive(c.created_at), "source": c.source, "related": rel, "proofs": prf,
        }


def recent_audit(limit: int = 200, action_like: str = "") -> pd.DataFrame:
    with read_scope() as s:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if action_like.strip():
            stmt = stmt.where(AuditLog.action.ilike(f"%{_like_escape(action_like.strip())}%", escape="\\"))
        rows = [{"#": r.id, "When (IST)": _ist_naive(datetime.fromisoformat(r.ts_utc.rstrip("Z"))),
                 "User": r.username, "Action": r.action, "Entity": r.entity_type, "Entity ID": r.entity_id,
                 "Reason": r.reason} for r in s.scalars(stmt)]
    return pd.DataFrame(rows, columns=["#", "When (IST)", "User", "Action", "Entity", "Entity ID", "Reason"])


def user_service_codes(user_id: int) -> list[str]:
    with read_scope() as s:
        return sorted(s.scalars(select(ServiceLine.code).join(
            database.UserServiceLine, database.UserServiceLine.service_line_id == ServiceLine.id
        ).where(database.UserServiceLine.user_id == user_id)))


def tail_file(path: Path, lines: int = 200, max_bytes: int = 400_000) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Stop spreadsheet formula injection: a party called '=HYPERLINK(...)' must not execute in Excel."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object or str(out[col].dtype).startswith("str"):
            out[col] = out[col].map(lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v)
    return out


# ============================================================================ 2. UI HELPERS
def flash(kind: str, msg: str) -> None:
    st.session_state["_flash"] = (kind, msg)


def render_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        kind, msg = item
        {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(msg)
        st.toast(msg, icon={"success": "✅", "warning": "⚠️", "error": "❌"}.get(kind, "ℹ️"))


def run_action(fn, *args, **kwargs):
    """Run a service call; show EVERY failure to the user (no silent failures). Returns (ok, result)."""
    try:
        return True, fn(*args, **kwargs)
    except FormInvalid as exc:
        for m in exc.errors: st.error(m)
        st.toast("Please fix the highlighted problems.", icon="⚠️")
    except (auth.PermissionDenied, auth.AuthError, audit.AuditError) as exc:
        st.error(str(exc)); st.toast(str(exc), icon="⛔")
    except DatabaseBusy as exc:
        st.warning(str(exc)); st.toast(str(exc), icon="⏳")
    except StaleDataError:
        st.warning("Someone else changed this record while you were working. Reload the page and try again.")
    except Exception:
        ref = uuid.uuid4().hex[:8].upper()
        log.exception("Unhandled error in action [%s]", ref)
        st.error(f"Unexpected error (reference {ref}). It has been written to error.log.")
        st.toast(f"Unexpected error, ref {ref}", icon="❌")
    return False, None


def _page_user(action: str) -> auth.SessionUser:
    user = st.session_state.get("_user")
    if not auth.can(user, action):
        st.error("You do not have access to this page.")
        st.stop()
    return user


def _end_session(msg: Optional[str] = None) -> None:
    st.session_state.clear()
    if msg:
        st.session_state["_login_notice"] = msg


def current_user() -> Optional[auth.SessionUser]:
    uid = st.session_state.get("user_id")
    if uid is None:
        return None
    if time.time() - st.session_state.get("last_active", 0) > config.SESSION_IDLE_MINUTES * 60:
        _end_session("Your session expired after inactivity. Please sign in again.")
        return None
    user = auth.load_session_user(uid)           # fresh every run: deactivation / scope changes apply at once
    if user is None:
        _end_session("Your account is no longer active.")
        return None
    st.session_state.update(last_active=time.time(), username=user.username, role=user.role)
    return user


@st.cache_resource(show_spinner="Starting up...")
def bootstrap() -> bool:
    database.init_db()
    auth.bootstrap_admin()
    backup.start_scheduler()
    return True


def inject_css() -> None:
    st.markdown("""<style>
    .block-container {padding-top: 1.6rem; max-width: 1500px;}
    [data-testid="stMetricValue"] {font-size: 1.9rem; font-weight: 650;}
    [data-testid="stMetricLabel"] p {font-size: .85rem; opacity: .75; text-transform: uppercase; letter-spacing: .03em;}
    </style>""", unsafe_allow_html=True)


# ---- login / forced password change
def render_login() -> None:
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown(f"## 🛡️ {config.APP_NAME}")
        st.caption("Authorised personnel only. All activity is recorded in a tamper-evident audit trail.")
        notice = st.session_state.pop("_login_notice", None)
        if notice:
            st.warning(notice)
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            go = st.form_submit_button("Sign in", type="primary")
        if go:
            if not username or not password:
                st.error("Enter both username and password.")
                return
            try:
                res = auth.authenticate(username, password)
            except DatabaseBusy as exc:
                st.warning(str(exc)); return
            except Exception:
                ref = uuid.uuid4().hex[:8].upper()
                log.exception("Login failure [%s]", ref)
                st.error(f"Sign-in is temporarily unavailable (reference {ref}).")
                return
            if res.ok:
                st.session_state.update(user_id=res.user.id, username=res.user.username,
                                        role=res.user.role, last_active=time.time())
                st.rerun()
            else:
                st.error(res.message)


def render_force_password_change(user: auth.SessionUser) -> None:
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown("## 🔐 Set a new password")
        st.info("For security you must choose your own password before continuing.")
        st.caption(f"At least {config.PASSWORD_MIN_LENGTH} characters with upper, lower, digit and symbol.")
        with st.form("pw_form"):
            cur = st.text_input("Current (temporary) password", type="password")
            new = st.text_input("New password", type="password")
            new2 = st.text_input("Repeat new password", type="password")
            go = st.form_submit_button("Change password", type="primary")
        if go:
            if new != new2:
                st.error("The two new passwords do not match.")
            else:
                ok, _ = run_action(auth.change_password, user.id, cur, new)
                if ok:
                    flash("success", "Password changed. Welcome!")
                    st.rerun()
        if st.button("Sign out"):
            auth.logout(user); _end_session(); st.rerun()


# ---- sidebar + global filters
def _reset_filters() -> None:
    for k in ("f_use_dates", "f_dates", "f_scope", "f_status", "f_sl", "f_search"):
        st.session_state.pop(k, None)


def render_sidebar(user: auth.SessionUser) -> Filters:
    lines = allowed_service_lines(user)
    with st.sidebar:
        with st.container(border=True):
            st.markdown(f"**👤 {user.full_name}**")
            st.caption(f"{user.role}" + (" · Partner" if user.is_partner else ""))
            st.caption("Access: all service lines" if user.service_line_ids is None
                       else "Access: " + (", ".join(c for _, c, _ in lines) or "none assigned"))
            if st.button("Sign out", key="btn_logout"):
                try: auth.logout(user)
                except Exception: log.exception("Logout audit failed")
                _end_session(); st.rerun()
        st.markdown("#### 🔎 Global filters")
        search = st.text_input("Search", key="f_search", placeholder="Name, ref no., PAN, GSTIN, CIN…")
        use_dates = st.toggle("Limit by created date", key="f_use_dates")
        start_utc = end_utc = None
        if use_dates:
            today = datetime.now(config.IST).date()
            rng = st.date_input("Created between", value=(today - timedelta(days=365), today), key="f_dates")
            if isinstance(rng, (tuple, list)) and len(rng) == 2:
                start_utc, end_utc = _ist_day_start_utc(rng[0]), _ist_day_start_utc(rng[1] + timedelta(days=1))
        scope = st.selectbox("Scope", ["All", *config.SCOPES], key="f_scope")
        statuses = st.multiselect("Conflict status", config.CONFLICT_STATUSES, format_func=pretty, key="f_status")
        sl_labels = {i: f"{c} - {n}" for i, c, n in lines}
        sl_ids = st.multiselect("Service line", list(sl_labels), format_func=sl_labels.get, key="f_sl")
        st.button("↺ Reset filters", on_click=_reset_filters)
        st.divider()
    return Filters(start_utc, end_utc, scope, tuple(statuses), tuple(sl_ids), search or "")


def describe_filters(f: Filters) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(f).items() if v not in (None, (), "", "All")}


def _log_export(kind: str, rows: int, filters: dict) -> None:
    try:
        with write_scope() as s:
            audit.log_event(s, user=st.session_state.get("_user"), action="RECORDS_EXPORTED",
                            entity_type="export", entity_id=kind, new={"rows": rows, "filters": filters})
    except Exception:
        log.exception("Could not audit-log an export")
        st.toast("Export was not recorded in the audit log - see error.log", icon="❌")


def export_button(df: pd.DataFrame, kind: str, f: Filters, label: str = "⬇️ Export CSV") -> None:
    user = st.session_state.get("_user")
    if not auth.can(user, "export_reports"):
        st.caption("CSV export is available to Admin, Risk Lead and Auditor roles.")
        return
    st.download_button(label, csv_safe(df).to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{kind}_{datetime.now(config.IST):%Y%m%d_%H%M}.csv", mime="text/csv",
                       key=f"dl_{kind}", on_click=_log_export, args=(kind, len(df), describe_filters(f)))


# ============================================================================ 3. PAGES
def _filters() -> Filters:
    return st.session_state.get("_filters", Filters())


def _chart_empty() -> None:
    st.info("No records match the current filters.")


def fig_scope_donut(df: pd.DataFrame):
    d = df.groupby("scope").size().reset_index(name="Records")
    fig = px.pie(d, names="scope", values="Records", hole=0.58, color="scope", color_discrete_map=SCOPE_COLORS)
    fig.update_traces(textinfo="value+percent")
    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), legend=dict(orientation="h", y=-0.1))
    return fig


def fig_status_bars(df: pd.DataFrame):
    d = df.groupby(["conflict_status", "scope"]).size().reset_index(name="Records")
    d["Status"] = d["conflict_status"].map(pretty)
    order = [pretty(s) for s in config.CONFLICT_STATUSES]
    fig = px.bar(d, x="Status", y="Records", color="scope", barmode="group", color_discrete_map=SCOPE_COLORS,
                 category_orders={"Status": order}, labels={"scope": "Scope"})
    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), legend=dict(orientation="h", y=1.1, title=None))
    return fig


def page_dashboard() -> None:
    user = _page_user("view_records")
    f = _filters()
    st.title("📊 Executive Dashboard")
    df = fetch_checks(user, f)
    now = datetime.now(config.IST).replace(tzinfo=None)
    pending = df[df["conflict_status"].isin(PENDING_STATES)]
    overdue = int((pending["age_days"] > OVERDUE_DAYS).sum()) if len(pending) else 0
    accepted = df[df["acceptance_status"] == "ACCEPTED"]

    def trend(frame: pd.DataFrame, col: str = "created_at") -> int:
        last = int((frame[col] >= now - timedelta(days=30)).sum())
        prev = int(((frame[col] >= now - timedelta(days=60)) & (frame[col] < now - timedelta(days=30))).sum())
        return last - prev

    cols = st.columns(5)
    cards = [
        ("Total tracked", len(df), f"{trend(df):+d} vs prev 30 days", "normal", "New records in the last 30 days compared with the 30 days before."),
        ("Pending conflicts", len(pending), f"{overdue} older than {OVERDUE_DAYS} days" if overdue else None, "inverse", "Submitted, or waiting on / completing partner alignment."),
        ("Domestic", int((df["scope"] == "Domestic").sum()), f"{trend(df[df['scope'] == 'Domestic']):+d} vs prev 30 days", "normal", None),
        ("Global", int((df["scope"] == "Global").sum()), f"{trend(df[df['scope'] == 'Global']):+d} vs prev 30 days", "normal", None),
        ("Completed acceptances", len(accepted), f"{trend(accepted, 'accepted_at'):+d} vs prev 30 days", "normal", "Client / vendor acceptance signed off."),
    ]
    for col, (label, value, delta, dcolor, hint) in zip(cols, cards):
        with col, st.container(border=True):
            st.metric(label, f"{value:,}", delta=delta, delta_color=dcolor, help=hint)
    drafts = int((df["conflict_status"] == "DRAFT").sum())
    if drafts:
        st.caption(f"{drafts:,} draft(s) not yet submitted are included in Total tracked but not in Pending conflicts.")

    c1, c2 = st.columns([1, 1.4])
    with c1, st.container(border=True):
        st.subheader("Scope breakdown")
        st.plotly_chart(fig_scope_donut(df), key="dash_donut") if len(df) else _chart_empty()
    with c2, st.container(border=True):
        st.subheader("Conflict status by scope")
        st.plotly_chart(fig_status_bars(df), key="dash_bars") if len(df) else _chart_empty()

    st.subheader("Most recent")
    recent = df.head(10)[["reference_no", "service_line", "party_name", "scope", "conflict_status", "created_at"]].copy()
    recent["conflict_status"] = recent["conflict_status"].map(pretty)
    st.dataframe(recent, hide_index=True, column_config={
        "reference_no": "Reference", "service_line": "Line", "party_name": "Entity", "scope": "Scope",
        "conflict_status": "Status", "created_at": st.column_config.DatetimeColumn("Created", format="DD MMM YYYY, HH:mm")})


def page_data_entry() -> None:
    user = _page_user("create_check")
    st.title("📝 New Conflict Check")
    lines = allowed_service_lines(user)
    if not lines:
        st.error("You have no service line assigned. Ask an Admin to assign one.")
        st.stop()
    n = st.session_state.setdefault("_de_nonce", 0)
    k = lambda name: f"de_{name}_{n}"          # new nonce after a successful save = fresh empty form
    scope = st.radio("Engagement scope", config.SCOPES, horizontal=True, key=k("scope"),
                     help="Global engagements need a Tax ID and country instead of PAN / GSTIN / CIN.")
    with st.form(f"de_form_{n}"):
        a, b = st.columns(2)
        sl_opts = {f"{c} - {nm}": i for i, c, nm in lines}
        sl_label = a.selectbox("Service line *", list(sl_opts), key=k("sl"))
        party_type = b.radio("Entity type *", config.PARTY_TYPES, horizontal=True, key=k("ptype"))
        name = st.text_input("Entity name *", max_chars=300, key=k("name"), placeholder="Legal name of the client / vendor")
        pan = gstin = cin = tax_id = country = None
        if scope == "Domestic":
            c1, c2, c3 = st.columns(3)
            pan = c1.text_input("PAN", max_chars=10, key=k("pan"))
            gstin = c2.text_input("GSTIN", max_chars=15, key=k("gstin"))
            cin = c3.text_input("CIN / LLPIN", max_chars=21, key=k("cin"))
        else:
            c1, c2 = st.columns(2)
            tax_id = c1.text_input("Tax ID / registration no.", max_chars=50, key=k("taxid"))
            country = c2.text_input("Country *", max_chars=60, key=k("country"))
        desc = st.text_area("Engagement description", key=k("desc"), height=100)
        st.markdown("**Related parties** (parent, subsidiaries, promoters, counterparties) - add rows as needed")
        rel_df = st.data_editor(
            pd.DataFrame({"name": pd.Series(dtype="str"), "relationship": pd.Series(dtype="str"),
                          "identifier": pd.Series(dtype="str")}),
            num_rows="dynamic", hide_index=True, key=k("rel"),
            column_config={"name": "Name", "relationship": "Relationship", "identifier": "PAN / ID"})
        d1, d2, _ = st.columns([1, 1, 2])
        draft = d1.form_submit_button("💾 Save as draft")
        submit = d2.form_submit_button("📨 Submit for review", type="primary")

    if draft or submit:
        ok, res = run_action(
            create_check, user, service_line_id=sl_opts[sl_label], party_type=party_type, scope=scope,
            party_name=name, pan=pan, gstin=gstin, cin=cin, tax_id=tax_id, country=country, description=desc,
            related=rel_df.to_dict("records"), submit=bool(submit))
        if ok:
            msg = f"{'Submitted' if submit else 'Saved as draft'}: {res['reference_no']}."
            if res["matches"]:
                if user.service_line_ids is None:
                    msg += " ⚠️ Possible existing records: " + "; ".join(
                        f"{m['reference_no']} ({m['service_line']}, {m['party_name']}, {m['matched_on']})" for m in res["matches"][:5])
                else:
                    msg += (f" ⚠️ {len(res['matches'])} possible existing record(s) elsewhere in the firm - "
                            "the Risk Lead will review them.")
            flash("warning" if res["matches"] else "success", msg)
            st.session_state["_de_nonce"] = n + 1
            st.rerun()


def render_detail(user: auth.SessionUser, check_id: int) -> None:
    d = get_check_detail(user, check_id)
    if d is None:
        st.warning("That record is not available to you.")
        return
    st.subheader(f"{d['reference_no']} · {d['party_name']}")
    tabs = ["Overview", "Related parties & proofs"] + (["Audit trail"] if auth.can(user, "view_audit") else [])
    t = st.tabs(tabs)
    with t[0]:
        a, b, c = st.columns(3)
        a.markdown(f"**Service line**  \n{d['service_line']}\n\n**Type / scope**  \n{d['party_type']} · {d['scope']}"
                   + (f" · {d['country']}" if d["country"] else ""))
        b.markdown(f"**Conflict status**  \n{pretty(d['status'])}\n\n**Decision**  \n{pretty(d['decision']) or '-'}\n\n"
                   f"**Acceptance**  \n{pretty(d['acceptance'])}")
        c.markdown(f"**Raised by**  \n{d['requested_by']}\n\n**Created**  \n{d['created_at']:%d %b %Y, %H:%M}"
                   + (f"\n\n**Source**  \nLegacy import" if d["source"] == "legacy_import" else ""))
        ids = {k.upper(): d[k] for k in ("pan", "gstin", "cin", "tax_id") if d[k]}
        if ids: st.markdown("**Identifiers:** " + " · ".join(f"{k}: `{v}`" for k, v in ids.items()))
        if d["description"]: st.markdown(f"**Engagement:** {d['description']}")
        if d["decided_by"] or d["aligned_partner"]:
            st.markdown(f"**Decided by:** {d['decided_by'] or '-'} · **Aligned partner:** {d['aligned_partner'] or '-'}")
        st.divider()
        is_maker = d["requested_by_id"] == user.id
        if is_maker and d["status"] == "DRAFT":
            if st.button("📨 Submit for review", key=f"sub_{d['id']}", type="primary"):
                ok, ref = run_action(maker_action, user, d["id"], "SUBMITTED")
                if ok: flash("success", f"{ref} submitted for review."); st.rerun()
        if is_maker and d["status"] in ("DRAFT", "SUBMITTED"):
            with st.expander("Withdraw this check"):
                reason = st.text_input("Reason (required)", key=f"wd_reason_{d['id']}")
                if st.button("Withdraw", key=f"wd_{d['id']}"):
                    ok, ref = run_action(maker_action, user, d["id"], "WITHDRAWN", reason)
                    if ok: flash("success", f"{ref} withdrawn."); st.rerun()
        if d["status"] == "SUBMITTED" and auth.can(user, "decide_conflict"):
            eligible, why = review_eligibility(user, d["requested_by_id"])
            (st.info if eligible else st.warning)(("✅ " if eligible else "🚫 Maker-checker: ") + why)
    with t[1]:
        st.markdown("**Related parties**")
        st.dataframe(pd.DataFrame(d["related"]), hide_index=True) if d["related"] else st.caption("None recorded.")
        st.markdown("**Proof documents**")
        st.dataframe(pd.DataFrame(d["proofs"]), hide_index=True) if d["proofs"] else st.caption("None attached yet (upload arrives in Part 3).")
    if len(t) > 2:
        with t[2]:
            trail = audit.get_entity_trail("conflict_check", d["id"])
            if trail:
                tdf = pd.DataFrame(trail)
                tdf["ts_utc"] = tdf["ts_utc"].map(lambda x: _ist_naive(datetime.fromisoformat(x.rstrip("Z"))))
                st.dataframe(tdf.rename(columns={"ts_utc": "When (IST)", "user": "User", "action": "Action",
                                                 "old": "Before", "new": "After", "reason": "Reason"}).drop(columns=["id"]),
                             hide_index=True)
            else:
                st.caption("No audit entries.")


def page_records() -> None:
    user = _page_user("view_records")
    f = _filters()
    st.title("🗂️ Master Records")
    df = fetch_checks(user, f, limit=MAX_RECORDS_TABLE)
    if len(df) == MAX_RECORDS_TABLE:
        st.warning(f"Showing the newest {MAX_RECORDS_TABLE:,} matches. Narrow the filters to see others.")
    h1, h2 = st.columns([3, 1])
    h1.caption(f"{len(df):,} record(s). Click a column header to sort; tick a row to open it.")
    with h2:
        export_button(df.drop(columns=["id", "age_days"]), "records", f)
    view = df[["id", "reference_no", "service_line", "party_name", "party_type", "scope", "conflict_status",
               "acceptance_status", "requested_by", "created_at", "pan", "gstin", "cin", "tax_id"]].copy()
    view["conflict_status"] = view["conflict_status"].map(pretty)
    view["acceptance_status"] = view["acceptance_status"].map(pretty)
    event = st.dataframe(view, hide_index=True, on_select="rerun", selection_mode="single-row", key="records_tbl",
                         column_config={"id": None, "reference_no": "Reference", "service_line": "Line",
                                        "party_name": "Entity", "party_type": "Type", "scope": "Scope",
                                        "conflict_status": "Conflict status", "acceptance_status": "Acceptance",
                                        "requested_by": "Raised by", "pan": "PAN", "gstin": "GSTIN", "cin": "CIN",
                                        "tax_id": "Tax ID",
                                        "created_at": st.column_config.DatetimeColumn("Created", format="DD MMM YYYY, HH:mm")})
    rows = event.selection.rows if event and event.selection else []
    if rows and rows[0] < len(df):
        st.divider()
        render_detail(user, int(df.iloc[rows[0]]["id"]))


def page_reports() -> None:
    user = _page_user("view_records")
    f = _filters()
    st.title("📈 Reports & Analytics")
    df = fetch_checks(user, f)
    if df.empty:
        _chart_empty(); return
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.subheader("Scope breakdown"); st.plotly_chart(fig_scope_donut(df), key="rep_donut")
    with c2, st.container(border=True):
        st.subheader("Conflict status by scope"); st.plotly_chart(fig_status_bars(df), key="rep_bars")

    with st.container(border=True):
        st.subheader("Volume over time")
        m = df.assign(Month=df["created_at"].dt.to_period("M").astype(str)).groupby(["Month", "scope"]).size().reset_index(name="Records")
        fig = px.bar(m, x="Month", y="Records", color="scope", barmode="stack", color_discrete_map=SCOPE_COLORS)
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), legend=dict(orientation="h", y=1.1, title=None))
        st.plotly_chart(fig, key="rep_trend")

    c3, c4 = st.columns(2)
    with c3, st.container(border=True):
        st.subheader("By service line")
        s = df.groupby(["service_line", "scope"]).size().reset_index(name="Records")
        fig = px.bar(s, x="service_line", y="Records", color="scope", color_discrete_map=SCOPE_COLORS, barmode="stack",
                     labels={"service_line": "Service line"})
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), legend=dict(orientation="h", y=1.1, title=None))
        st.plotly_chart(fig, key="rep_sl")
    with c4, st.container(border=True):
        st.subheader("Ageing of open checks")
        op = df[df["conflict_status"].isin(PENDING_STATES)].copy()
        if op.empty:
            st.success("No open checks.")
        else:
            op["Age"] = pd.cut(op["age_days"], [-1, 2, 5, 10, 10_000], labels=["0-2 days", "3-5 days", "6-10 days", "11+ days"])
            a = op.groupby("Age", observed=False).size().reset_index(name="Open checks")
            fig = px.bar(a, x="Age", y="Open checks", color="Age", color_discrete_sequence=["#22c55e", "#eab308", "#f97316", "#ef4444"])
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=False)
            st.plotly_chart(fig, key="rep_age")

    st.subheader("Summary table")
    piv = pd.pivot_table(df, index="service_line", columns="conflict_status", values="id", aggfunc="count", fill_value=0)
    piv.columns = [pretty(c) for c in piv.columns]
    piv["Total"] = piv.sum(axis=1)
    piv.loc["TOTAL"] = piv.sum()
    st.dataframe(piv)
    export_button(piv.reset_index().rename(columns={"service_line": "Service line"}), "summary", f, "⬇️ Export summary CSV")


def _diagnostics_report(user: auth.SessionUser) -> dict:
    import sys
    with read_scope() as s:
        counts = {name: s.scalar(select(func.count()).select_from(tbl)) for name, tbl in
                  (("users", User), ("conflict_checks", ConflictCheck), ("proof_documents", ProofDocument),
                   ("audit_log", AuditLog))}
    chain = audit.verify_chain()
    bks = backup.list_backups()
    return {
        "generated_at_ist": datetime.now(config.IST).isoformat(timespec="seconds"), "generated_by": user.username,
        "app_version": config.APP_VERSION, "python": sys.version.split()[0], "streamlit": st.__version__,
        "platform": platform.platform(), "db_path": str(config.DB_PATH),
        "db_size_mb": round(config.DB_PATH.stat().st_size / 1e6, 2) if config.DB_PATH.exists() else None,
        "health": database.health_check(), "audit_chain": asdict(chain), "row_counts": counts,
        "latest_backup": (bks[0]["path"].name if bks else None), "backup_count": len(bks),
        "error_log_tail": tail_file(config.LOG_DIR / "error.log", 100),
    }


def page_diagnostics() -> None:
    user = _page_user("view_audit")
    st.title("🩺 Diagnostics & Self-Repair")
    labels = ["🔗 Audit chain"]
    full = auth.can(user, "view_diagnostics")
    if full:
        labels += ["💚 System health", "🧾 Error log", "💾 Backups"]
    tabs = st.tabs(labels)

    with tabs[0]:
        st.caption("Recomputes every hash in the audit trail. Any edited, deleted or injected entry is pinpointed.")
        if auth.can(user, "verify_audit") and st.button("🔍 Verify audit chain now", type="primary"):
            with st.spinner("Verifying..."):
                rep = audit.verify_chain()
            st.session_state["_chain_rep"] = rep
            try:
                with write_scope() as s:
                    audit.log_event(s, user=user, action="AUDIT_CHAIN_VERIFIED", entity_type="audit",
                                    new={"ok": rep.ok, "checked": rep.checked, "message": rep.message})
            except Exception:
                log.exception("Could not log chain verification")
        rep = st.session_state.get("_chain_rep")
        if rep:
            (st.success if rep.ok else st.error)(("✅ " if rep.ok else "🚨 ") + rep.message)
            m1, m2 = st.columns(2)
            m1.metric("Entries verified", f"{rep.checked:,}")
            m2.metric("First bad entry", rep.first_bad_id or "-")
        with read_scope() as s:
            head = s.get(database.AuditHead, 1)
            tip = (head.last_id, head.last_hash) if head else (None, None)
        st.caption(f"Chain tip: #{tip[0]} · `{tip[1]}`  (also stored in every backup manifest)")
        flt = st.text_input("Filter recent events by action", key="audit_flt", placeholder="e.g. LOGIN, CHECK_, USER_")
        adf = recent_audit(300, flt)
        st.dataframe(adf, hide_index=True, column_config={"When (IST)": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss")})

    if not full:
        return
    with tabs[1]:
        h = database.health_check()
        (st.success if h["ok"] else st.error)("All health checks passed." if h["ok"] else "One or more checks failed.")
        st.dataframe(pd.DataFrame([{"Check": pretty(k), "Status": "✅ OK" if v["ok"] else "❌ FAIL", "Detail": v["detail"]}
                                   for k, v in h["checks"].items()]), hide_index=True)
        if config.DB_PATH.exists():
            st.caption(f"Database: `{config.DB_PATH}` · {config.DB_PATH.stat().st_size / 1e6:.2f} MB · {config.APP_VERSION}")
        st.download_button("⬇️ Download diagnostics report (JSON)",
                           json.dumps(_diagnostics_report(user), indent=2, default=str).encode("utf-8"),
                           file_name=f"diagnostics_{datetime.now(config.IST):%Y%m%d_%H%M}.json", mime="application/json")
        if auth.can(user, "manage_users"):
            st.markdown("##### Safe self-repair (Admin) - none of these change business data")
            r1, r2, r3 = st.columns(3)
            for col, key, label, fn in ((r1, "triggers", "Re-install append-only triggers", _repair_triggers),
                                        (r2, "wal", "Checkpoint WAL file", _repair_checkpoint),
                                        (r3, "reindex", "Rebuild indexes", _repair_reindex)):
                if col.button(label, key=f"repair_{key}"):
                    ok, msg = run_action(fn, user)
                    if ok: st.success(msg)
    with tabs[2]:
        n = st.selectbox("Lines to show", [100, 200, 500], index=1)
        lines = tail_file(config.LOG_DIR / "error.log", n)
        if not lines:
            st.success("error.log is empty - no warnings or errors recorded.")
        else:
            st.code("\n".join(lines), language="log", height=420)
        lp = config.LOG_DIR / "error.log"
        if lp.exists() and auth.can(user, "manage_users"):
            st.download_button("⬇️ Download full error.log", lp.read_bytes(), file_name="error.log")
    with tabs[3]:
        bks = backup.list_backups()
        if auth.can(user, "run_backup") and st.button("💾 Back up now", type="primary"):
            ok, m = run_action(backup.create_backup, "manual", user)
            if ok: st.success(f"Backup created: {m['file']} ({m['size_bytes'] / 1e6:.2f} MB)"); bks = backup.list_backups()
        if bks:
            st.dataframe(pd.DataFrame([{"File": b["path"].name, "Label": b["label"], "Size (MB)": round(b["size"] / 1e6, 2),
                                        "Created (IST)": b["ts"].astimezone(config.IST).replace(tzinfo=None)} for b in bks]),
                         hide_index=True)
            pick = st.selectbox("Verify a backup (restore test)", [b["path"].name for b in bks])
            if st.button("✔️ Verify selected backup"):
                with st.spinner("Unpacking and checking..."):
                    rep = backup.verify_backup(config.BACKUP_DIR / pick)
                (st.success if rep["ok"] else st.error)("Backup is valid." if rep["ok"] else f"Problems: {rep['problems']}")
        else:
            st.info("No backups yet.")
        st.caption("Restoring is deliberately command-line only: stop the app, then `python backup.py restore <file>`.")


def _repair_log(user, action: str, detail: dict) -> None:
    with write_scope() as s:
        audit.log_event(s, user=user, action=action, entity_type="system", new=detail)


def _repair_triggers(user) -> str:
    auth.require(user, "manage_users")
    with database.engine.begin() as conn:
        before = database.missing_triggers(conn)
        database.install_triggers(conn)
    _repair_log(user, "REPAIR_TRIGGERS", {"were_missing": before})
    return f"Triggers verified ({len(before)} had been missing)."


def _repair_checkpoint(user) -> str:
    auth.require(user, "manage_users")
    with database.engine.connect() as c:
        row = c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    _repair_log(user, "REPAIR_WAL_CHECKPOINT", {"result": list(row) if row else None})
    return "WAL checkpoint completed."


def _repair_reindex(user) -> str:
    auth.require(user, "manage_users")
    with database.engine.connect() as c:
        c.exec_driver_sql("REINDEX")
    _repair_log(user, "REPAIR_REINDEX", {})
    return "Indexes rebuilt."


def page_admin() -> None:
    user = _page_user("manage_users")
    st.title("⚙️ User Administration")
    users = auth.list_users()
    st.dataframe(pd.DataFrame(users).drop(columns=["id"]).rename(columns={
        "username": "Username", "full_name": "Name", "role": "Role", "is_partner": "Partner", "is_active": "Active",
        "locked_until": "Locked until (UTC)", "last_login_at": "Last login (UTC)"}), hide_index=True)
    codes = [c for _, c, _ in allowed_service_lines(user)]
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("Create user")
        with st.form("create_user"):
            un = st.text_input("Username (lowercase)")
            fn = st.text_input("Full name")
            em = st.text_input("Email (optional)")
            role = st.selectbox("Role", config.ROLES)
            partner = st.checkbox("Eligible as aligned partner")
            lines = st.multiselect("Service lines (required for Service Line Members)", codes)
            go = st.form_submit_button("Create user", type="primary")
        if go:
            ok, res = run_action(auth.create_user, user, username=un, full_name=fn, email=em or None, role=role,
                                 is_partner=partner, service_line_codes=lines)
            if ok:
                st.success(f"User '{un.strip().lower()}' created. Give them this temporary password - it is shown only once:")
                st.code(res[1])
    with right, st.container(border=True):
        st.subheader("Edit user / reset password")
        by_name = {u["username"]: u for u in users}
        pick = st.selectbox("User", list(by_name), key="adm_pick")
        u = by_name[pick]
        with st.form(f"edit_user_{u['id']}"):
            fn2 = st.text_input("Full name", value=u["full_name"])
            role2 = st.selectbox("Role", config.ROLES, index=config.ROLES.index(u["role"]))
            act = st.checkbox("Active", value=u["is_active"])
            par = st.checkbox("Eligible as aligned partner", value=u["is_partner"])
            cur_codes = [c for c in user_service_codes(u["id"]) if c in codes]
            ln2 = st.multiselect("Service lines", codes, default=cur_codes)
            reason = st.text_input("Reason for change (recorded in audit trail)")
            save = st.form_submit_button("Save changes", type="primary")
        if save:
            ok, _ = run_action(auth.update_user, user, u["id"], full_name=fn2, role=role2, is_active=act,
                               is_partner=par, service_line_codes=ln2, reason=reason or None)
            if ok: flash("success", f"{pick} updated."); st.rerun()
        if st.button("🔑 Reset password", key=f"reset_{u['id']}"):
            ok, tmp = run_action(auth.admin_reset_password, user, u["id"])
            if ok:
                st.success("Temporary password (shown once; user must change it at next login):")
                st.code(tmp)


# ============================================================================ 4. MAIN
def build_pages(user: auth.SessionUser) -> list:
    pages = [st.Page(page_dashboard, title="Dashboard", icon="📊", url_path="dashboard", default=True)]
    if auth.can(user, "create_check"):
        pages.append(st.Page(page_data_entry, title="Data Entry", icon="📝", url_path="data-entry"))
    pages += [st.Page(page_records, title="Records Table", icon="🗂️", url_path="records"),
              st.Page(page_reports, title="Reports & Analytics", icon="📈", url_path="reports")]
    if auth.can(user, "view_audit"):
        pages.append(st.Page(page_diagnostics, title="Diagnostics & Self-Repair", icon="🩺", url_path="diagnostics"))
    if auth.can(user, "manage_users"):
        pages.append(st.Page(page_admin, title="User Administration", icon="⚙️", url_path="admin"))
    return pages


def main() -> None:
    st.set_page_config(page_title=config.APP_NAME, page_icon="🛡️", layout="wide", initial_sidebar_state="expanded")
    inject_css()
    try:
        bootstrap()
    except Exception as exc:
        log.exception("Startup failed")
        st.error("The application could not start. Details were written to error.log.")
        st.code(str(exc))
        st.stop()
    user = current_user()
    if user is None:
        render_login()
        st.stop()
    st.session_state["_user"] = user
    if user.must_change_password:
        render_force_password_change(user)
        st.stop()
    st.session_state["_filters"] = render_sidebar(user)
    render_flash()
    pg = st.navigation(build_pages(user))
    try:
        pg.run()
    except Exception:
        ref = uuid.uuid4().hex[:8].upper()
        log.exception("Unhandled page error [%s]", ref)
        st.error(f"Something went wrong (reference {ref}). Your data is safe; the error was logged. "
                 "Try another page or reload. If it persists, send the reference to your Admin.")


if __name__ == "__main__":
    main()

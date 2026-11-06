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

Part 3 adds: document upload + verification UI, the two-option decision workflow with partner sign-off,
PDF certificate access, live fuzzy screening, the Action Queue, strict per-line CSV export, Excel sync status.
The business rules themselves live in workflow.py; this file is screens only.

Audit: all events go through audit.log_event() (the "log_audit_event" of the brief).
"""
from __future__ import annotations

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
import excel_sync
import matching
import workflow
from database import (AuditLog, ConflictCheck, DatabaseBusy, ProofDocument, RuleViolation, ServiceLine, User,
                      normalize_id, normalize_name, read_scope, write_scope)
# Re-exported so existing tests / callers keep working: the rules live in workflow.py.
from workflow import (FormInvalid, create_check, get_check_detail, maker_action, pretty,  # noqa: F401
                      review_eligibility)

log = config.get_logger("app")

PENDING_STATES = ("SUBMITTED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED")
OVERDUE_DAYS = 5                      # open checks older than this are flagged
MAX_RECORDS_TABLE = 5000
SCOPE_COLORS = {"Domestic": "#2563eb", "Global": "#f59e0b"}
UPLOAD_TYPES = [e.lstrip(".") for e in sorted(config.ALLOWED_UPLOAD_EXTENSIONS)]
_ist_naive = workflow.ist_naive


# ============================================================================ 1. QUERY LAYER (strictly service-line scoped)

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
    except RuleViolation as exc:                    # e.g. document integrity failure
        st.error(str(exc)); st.toast(str(exc), icon="🚨")
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
    excel_sync.start()
    excel_sync.request_sync("startup")
    return True


def inject_css() -> None:
    st.markdown("""<style>
    .block-container {padding-top: 1.6rem; max-width: 1500px;}
    [data-testid="stMetricValue"] {font-size: 1.9rem; font-weight: 650;}
    [data-testid="stMetricLabel"] p {font-size: .85rem; opacity: .75; text-transform: uppercase; letter-spacing: .03em;}
    .ccp-banner {border: 2px solid #f59e0b; background: rgba(245,158,11,.12); border-radius: 10px;
                 padding: .9rem 1.1rem; margin: .6rem 0 .8rem 0; font-size: 1.02rem; line-height: 1.45;}
    .ccp-banner b {color: #b45309;}
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


def _log_export(kind: str, rows: int, filters: dict, lines: list) -> None:
    try:
        with write_scope() as s:
            audit.log_event(s, user=st.session_state.get("_user"), action="RECORDS_EXPORTED", entity_type="export",
                            entity_id=kind, new={"rows": rows, "filters": filters, "service_lines": lines})
    except Exception:
        log.exception("Could not audit-log an export")
        st.toast("Export was not recorded in the audit log - see error.log", icon="❌")


def _assigned_codes(user: auth.SessionUser) -> Optional[set]:
    """Codes of EVERY service line assigned to the user (active or not); None = unrestricted role."""
    if user.service_line_ids is None:
        return None
    with read_scope() as s:
        return set(s.scalars(select(ServiceLine.code).where(ServiceLine.id.in_(list(user.service_line_ids)))))


def export_dataframe(user: auth.SessionUser, f: Filters) -> pd.DataFrame:
    """Fresh SERVER-SIDE query for export. A Service Line Member can only ever export rows of their own
    assigned lines: scoped in SQL by fetch_checks(), then re-verified here (defence in depth), never taken from
    what happens to be on screen."""
    auth.require(user, "export_reports")
    df = fetch_checks(user, f)
    codes = _assigned_codes(user)
    if codes is not None:
        df = df[df["service_line"].isin(codes)]
    return df.drop(columns=["id", "age_days"])


def summary_pivot(user: auth.SessionUser, f: Filters) -> pd.DataFrame:
    df = fetch_checks(user, f)
    piv = pd.pivot_table(df, index="service_line", columns="conflict_status", values="id", aggfunc="count", fill_value=0)
    piv.columns = [pretty(c) for c in piv.columns]
    piv["Total"] = piv.sum(axis=1)
    piv.loc["TOTAL"] = piv.sum()
    return piv


def export_button(user: auth.SessionUser, f: Filters, kind: str, builder, label: str = "CSV") -> None:
    """Two steps: 'Prepare' runs the strict query, then 'Download' serves it and writes the audit entry."""
    if not auth.can(user, "export_reports"):
        st.caption("You do not have permission to export.")
        return
    desc = describe_filters(f)
    key = f"_exp_{kind}"
    if st.button(f"📤 Prepare {label} export", key=f"prep_{kind}"):
        ok, df = run_action(builder)
        if ok:
            safe = csv_safe(df)
            st.session_state[key] = {"csv": safe.to_csv(index=False).encode("utf-8-sig"), "rows": len(df), "desc": desc,
                                     "lines": sorted(set(df["service_line"])) if "service_line" in df else []}
    ex = st.session_state.get(key)
    if ex and ex["desc"] == desc:
        scope_note = "" if user.service_line_ids is None else " (your assigned service lines only)"
        st.download_button(f"⬇️ Download {label} ({ex['rows']:,} rows){scope_note}", ex["csv"],
                           file_name=f"{kind}_{datetime.now(config.IST):%Y%m%d_%H%M}.csv", mime="text/csv",
                           key=f"dl_{kind}", on_click=_log_export, args=(kind, ex["rows"], desc, ex["lines"]))
    elif ex:
        st.caption("Filters changed: prepare the export again.")


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
    q = workflow.fetch_queue(user)
    todo = int((~q["action"].str.startswith("Waiting")).sum()) if len(q) else 0
    if todo:
        st.info(f"📥 **{todo} item(s) need your action.** Open **Action Queue** in the sidebar.")
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


def gst_banner() -> None:
    st.markdown(f'<div class="ccp-banner">⚠️ <b>Please read before submitting</b><br>{config.GST_ADDRESS_WARNING}</div>',
                unsafe_allow_html=True)


def render_screening(user: auth.SessionUser, matches: list[dict], hidden: int, typed: bool) -> None:
    """Live duplicate / conflict flags. Details only for lines the user may see; elsewhere just a count."""
    if not matches and not hidden:
        if typed:
            st.caption("✅ No similar entities found across the firm.")
        return
    high = any(m["level"] == matching.LEVEL_HIGH for m in matches)
    (st.error if high else st.warning)(
        f"{'🚨 Likely duplicate' if high else '⚠️ Possible match'}: {len(matches) + hidden} existing record(s) "
        "look similar. Please review before submitting.")
    if matches:
        mdf = pd.DataFrame([{"Reference": m["reference_no"], "Entity": m["party_name"], "Line": m["service_line"],
                             "Status": pretty(m["status"]), "Match": m["score"], "Flag": m["level"],
                             "Why": "; ".join(m["reasons"])} for m in matches])
        st.dataframe(mdf, hide_index=True, column_config={
            "Match": st.column_config.ProgressColumn("Match", min_value=0, max_value=100, format="%d%%")})
    if hidden:
        st.info(f"{hidden} further potential match(es) are in other service lines. Details are restricted to the "
                "Risk Lead, who will review them when you submit.")


def page_data_entry() -> None:
    user = _page_user("create_check")
    st.title("📝 New Conflict Check")
    lines = allowed_service_lines(user)
    if not lines:
        st.error("You have no service line assigned. Ask an Admin to assign one.")
        st.stop()
    n = st.session_state.setdefault("_de_nonce", 0)
    k = lambda name: f"de_{name}_{n}"          # a new nonce after a successful save = fresh empty page
    sl_opts = {f"{c} - {nm}": i for i, c, nm in lines}

    st.markdown("##### 1 · Entity")
    a, b, c = st.columns(3)
    party_type = a.radio("Entity type *", config.PARTY_TYPES, horizontal=True, key=k("ptype"))
    scope = b.radio("Engagement scope *", config.SCOPES, horizontal=True, key=k("scope"),
                    help="Global = international. It changes the identifiers and the documents required.")
    sl_label = c.selectbox("Service line *", list(sl_opts), key=k("sl"))
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
    desc = st.text_area("Engagement description", key=k("desc"), height=90)
    st.markdown("**Related parties** (parent, subsidiaries, promoters, counterparties). Add one row per party.")
    rel_df = st.data_editor(
        pd.DataFrame({"name": pd.Series(dtype="str"), "relationship": pd.Series(dtype="str"),
                      "identifier": pd.Series(dtype="str")}),
        num_rows="dynamic", hide_index=True, key=k("rel"),
        column_config={"name": "Name", "relationship": "Relationship", "identifier": "PAN / ID"})
    rel_rows = rel_df.to_dict("records")

    st.markdown("##### 2 · Live conflict screening")
    matches, hidden = workflow.screen_entity(
        user, name=name or "", related_names=[r.get("name") or "" for r in rel_rows],
        pan=pan, gstin=gstin, cin=cin, tax_id=tax_id)
    render_screening(user, matches, hidden, typed=len(normalize_name(name or "")) >= config.FUZZY_MIN_LEN)

    st.markdown("##### 3 · Documents")
    reqs = workflow.required_docs(party_type, scope)
    st.caption(f"Required for a {party_type.lower()} / {scope.lower()} engagement. Fields marked * must be attached to submit "
               f"(PDF, image, Word, Excel; max {config.MAX_UPLOAD_MB} MB each). You can save a draft without them.")
    uploads, got = [], 0
    cols = st.columns(2)
    for i, (key, required) in enumerate(reqs):
        f = cols[i % 2].file_uploader(f"{workflow.doc_label(key)}{' *' if required else ' (if applicable)'}",
                                      type=UPLOAD_TYPES, key=k(f"doc_{key}_{party_type}_{scope}"))
        if f is not None:
            uploads.append({"doc_type": key, "filename": f.name, "data": f.getvalue()})
            got += bool(required)
    extra = st.file_uploader("Other supporting documents (optional)", type=UPLOAD_TYPES, accept_multiple_files=True,
                             key=k("doc_other"))
    uploads += [{"doc_type": "OTHER", "filename": x.name, "data": x.getvalue()} for x in extra or []]
    need = sum(1 for _, r in reqs if r)
    st.progress(got / need if need else 1.0, text=f"{got} of {need} required documents attached")

    st.markdown("##### 4 · Final check")
    gst_banner()
    confirm = st.checkbox(config.FINAL_REVIEW_TEXT, key=k("confirm"))
    d1, d2, _ = st.columns([1, 1, 2])
    draft = d1.button("💾 Save as draft", key=k("btn_draft"))
    submit = d2.button("📨 Submit for review", type="primary", disabled=not confirm, key=k("btn_submit"),
                       help="Tick the final review confirmation to enable submission.")
    if not confirm:
        st.caption("Tick the confirmation above to enable **Submit for review**.")

    if draft or submit:
        ok, res = run_action(
            create_check, user, service_line_id=sl_opts[sl_label], party_type=party_type, scope=scope, party_name=name,
            pan=pan, gstin=gstin, cin=cin, tax_id=tax_id, country=country, description=desc, related=rel_rows,
            uploads=uploads, submit=bool(submit), confirm_review=bool(confirm))
        if ok:
            vis, hid = workflow.partition_matches(user, res["matches"])
            msg = f"{'Submitted' if submit else 'Saved as draft'}: {res['reference_no']} ({res['documents']} document(s))."
            if vis:
                msg += " ⚠️ Possible existing records: " + "; ".join(
                    f"{m['reference_no']} ({m['service_line']}, {m['party_name']}, {m['score']}%)" for m in vis[:5])
            if hid:
                msg += f" ⚠️ {hid} further potential match(es) elsewhere in the firm will be reviewed by the Risk Lead."
            flash("warning" if (vis or hid) else "success", msg)
            st.session_state["_de_nonce"] = n + 1
            st.rerun()


@st.dialog("Final check before submitting")
def submit_dialog(user: auth.SessionUser, check_id: int, ref: str, missing: list[str]) -> None:
    gst_banner()
    if missing:
        st.error("Required documents still missing: " + ", ".join(missing) + ". Upload them in the Documents tab first.")
    ok = st.checkbox(config.FINAL_REVIEW_TEXT, key=f"dlg_confirm_{check_id}")
    c1, c2 = st.columns(2)
    if c1.button("Confirm & submit", type="primary", disabled=(not ok) or bool(missing), key=f"dlg_go_{check_id}"):
        done, _ = run_action(workflow.submit_check, user, check_id, confirm_review=True)
        if done:
            flash("success", f"{ref} submitted for review.")
            st.rerun()
    if c2.button("Cancel", key=f"dlg_cancel_{check_id}"):
        st.rerun()


def _log_download(doc_id: int) -> None:
    try:
        workflow.log_document_download(st.session_state.get("_user"), doc_id)
    except Exception:
        log.exception("Could not audit-log a document download")


def _human_size(n) -> str:
    n = n or 0
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(n, 1) / 1024:.0f} KB"


def _render_documents(user: auth.SessionUser, d: dict) -> None:
    chk = d["checklist"]
    req = [x for x in chk if x["required"]]
    done = sum(1 for x in req if x["present"])
    st.progress(done / len(req) if req else 1.0, text=f"{done} of {len(req)} required documents attached")
    by_key = {x["key"]: x for x in d["docs"]}
    rows = [{"Document": x["label"], "Requirement": "Required" if x["required"] else "Optional",
             "Status": "✅ Attached" if x["present"] else ("❌ Missing" if x["required"] else "➖ Not provided"),
             "File": (by_key.get(x["key"]) or {}).get("filename"),
             "SHA-256": ((by_key.get(x["key"]) or {}).get("sha256") or "")[:16],
             "Uploaded": (by_key.get(x["key"]) or {}).get("uploaded_at")} for x in chk]
    in_list = {x["key"] for x in chk}
    rows += [{"Document": x["label"], "Requirement": "System" if x["key"] == "APPROVAL_CERTIFICATE" else "Extra",
              "Status": "✅ Attached", "File": x["filename"], "SHA-256": (x["sha256"] or "")[:16], "Uploaded": x["uploaded_at"]}
             for x in d["docs"] if x["key"] not in in_list]
    st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={
        "Uploaded": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm")})

    is_maker_draft = d["requested_by_id"] == user.id and d["status"] == "DRAFT"
    reviewer_extra = auth.can(user, "decide_conflict") and d["status"] in ("SUBMITTED", "ALIGNMENT_PENDING", "ALIGNMENT_CONFIRMED")
    if is_maker_draft or reviewer_extra:
        with st.container(border=True):
            st.markdown("**Add / replace a document**")
            opts = ([(x["key"], x["label"]) for x in chk] + [("OTHER", config.DOC_LABELS["OTHER"])]) if is_maker_draft \
                else [("ALIGNMENT_PROOF", config.DOC_LABELS["ALIGNMENT_PROOF"]), ("OTHER", config.DOC_LABELS["OTHER"])]
            un = st.session_state.setdefault(f"_up_nonce_{d['id']}", 0)
            kind = st.selectbox("Document type", [k for k, _ in opts], format_func=workflow.doc_label, key=f"up_kind_{d['id']}_{un}")
            f = st.file_uploader("File", type=UPLOAD_TYPES, key=f"up_file_{d['id']}_{un}")
            if st.button("⬆️ Upload", key=f"up_btn_{d['id']}_{un}", disabled=f is None):
                ok, cnt = run_action(workflow.attach_documents, user, d["id"],
                                     [{"doc_type": kind, "filename": f.name, "data": f.getvalue()}])
                if ok:
                    flash("success", f"{workflow.doc_label(kind)} uploaded.")
                    st.session_state[f"_up_nonce_{d['id']}"] = un + 1
                    st.rerun()

    opts = {f"{x['label']} - {x['filename']}": x for x in d["docs"] if not x["external"]}
    if opts:
        st.markdown("**Open / verify a document**")
        labels = list(opts)
        default = next((i for i, lab in enumerate(labels) if opts[lab]["key"] == "APPROVAL_CERTIFICATE"), 0)
        sel = opts[st.selectbox("Document", labels, index=default, key=f"docpick_{d['id']}")]
        try:
            fname, data, _v = workflow.read_document(user, sel["id"])
        except RuleViolation as exc:
            st.error(f"🚨 {exc}")
        else:
            st.success(f"✅ Integrity verified: SHA-256 matches the value recorded at upload ({sel['sha256'][:16]}…, {_human_size(sel['size'])}).")
            st.download_button(f"⬇️ Download {fname}", data, file_name=fname, key=f"dl_doc_{sel['id']}",
                               on_click=_log_download, args=(sel["id"],))
        if st.button("🛡️ Verify ALL documents of this check", key=f"verify_all_{d['id']}"):
            ok, res = run_action(workflow.verify_check_documents, user, d["id"])
            if ok:
                bad = [x for x in res if x["ok"] is False]
                (st.error if bad else st.success)(f"{len(res) - len(bad)} of {len(res)} verified." + (
                    " FAILED: " + ", ".join(x["label"] for x in bad) if bad else ""))


def _render_actions(user: auth.SessionUser, d: dict) -> None:
    cid, status = d["id"], d["status"]
    is_maker = d["requested_by_id"] == user.id
    if d["limited"] and status != "ALIGNMENT_PENDING":
        return

    # ---- maker: submit / edit / withdraw
    if is_maker and status == "DRAFT":
        c1, c2 = st.columns([1, 3])
        if c1.button("📨 Submit for review", key=f"sub_{cid}", type="primary"):
            submit_dialog(user, cid, d["reference_no"], d["missing"])
        if d["missing"]:
            c2.caption("Missing required documents: " + ", ".join(d["missing"]))
        with st.expander("✏️ Edit draft"):
            with st.form(f"edit_{cid}"):
                pt = st.radio("Entity type", config.PARTY_TYPES, horizontal=True, index=config.PARTY_TYPES.index(d["party_type"]))
                sc = st.radio("Scope", config.SCOPES, horizontal=True, index=config.SCOPES.index(d["scope"]))
                nm = st.text_input("Entity name", value=d["party_name"])
                e1, e2, e3, e4 = st.columns(4)
                pan = e1.text_input("PAN", value=d["pan"] or ""); gst = e2.text_input("GSTIN", value=d["gstin"] or "")
                cin = e3.text_input("CIN / LLPIN", value=d["cin"] or ""); tax = e4.text_input("Tax ID", value=d["tax_id"] or "")
                ctry = st.text_input("Country (Global)", value=d["country"] or "")
                ds = st.text_area("Engagement description", value=d["description"] or "")
                if st.form_submit_button("Save changes"):
                    ok, _ = run_action(workflow.update_draft, user, cid, party_type=pt, scope=sc, party_name=nm, pan=pan,
                                       gstin=gst, cin=cin, tax_id=tax, country=ctry, description=ds)
                    if ok:
                        flash("success", "Draft updated."); st.rerun()
    if is_maker and status in ("DRAFT", "SUBMITTED"):
        with st.expander("Withdraw this check"):
            reason = st.text_input("Reason (required)", key=f"wd_reason_{cid}")
            if st.button("Withdraw", key=f"wd_{cid}"):
                ok, ref = run_action(workflow.withdraw_check, user, cid, reason)
                if ok:
                    flash("success", f"{ref} withdrawn."); st.rerun()

    # ---- Risk Lead: the two-option decision
    if status == "SUBMITTED" and auth.can(user, "decide_conflict"):
        with st.container(border=True):
            st.markdown("#### ⚖️ Conflict decision")
            ok_to_review, why = review_eligibility(user, d["requested_by_id"])
            if not ok_to_review:
                st.warning("🚫 Maker-checker: " + why)
            else:
                st.caption("Review the Screening tab first, then choose one option.")
                opt = st.radio("Decision", ["A", "B"], key=f"opt_{cid}", horizontal=True,
                               format_func=lambda x: "Option A: Approved (no conflict)" if x == "A"
                               else "Option B: Partner alignment needed")
                notes = st.text_area("Notes" + (" (required: describe the potential conflict)" if opt == "B" else " (optional)"),
                                     key=f"dec_notes_{cid}")
                partner_id = None
                if opt == "B":
                    cands = workflow.list_partner_candidates({d["requested_by_id"], user.id})
                    if not cands:
                        st.error("No eligible partner exists. An Admin must flag a user as an eligible partner first.")
                    else:
                        labels = {c["id"]: f"{c['name']} ({c['username']})" for c in cands}
                        partner_id = st.selectbox("Aligned partner", list(labels), format_func=labels.get, key=f"partner_{cid}")
                if st.button("✅ Record decision", type="primary", key=f"decide_{cid}", disabled=(opt == "B" and partner_id is None)):
                    if opt == "A":
                        ok, res = run_action(workflow.decide_approve, user, cid, notes)
                        if ok:
                            flash("success", f"{res['reference_no']} approved. Certificate issued (SHA-256 {res['certificate_sha256'][:12]}…). "
                                  "Client acceptance is now unlocked."); st.rerun()
                    else:
                        ok, res = run_action(workflow.decide_alignment, user, cid, partner_id, notes)
                        if ok:
                            flash("success", f"{res['reference_no']} sent to {res['partner']} for alignment sign-off."); st.rerun()
            with st.expander("Reject this check"):
                rr = st.text_input("Reason (required)", key=f"rej_reason_{cid}")
                if st.button("Reject", key=f"rej_{cid}", disabled=not ok_to_review):
                    ok, ref = run_action(workflow.reject_check, user, cid, rr)
                    if ok:
                        flash("success", f"{ref} rejected."); st.rerun()

    # ---- aligned partner sign-off (or waiting)
    if status == "ALIGNMENT_PENDING":
        with st.container(border=True):
            st.markdown("#### 🤝 Partner alignment")
            if auth.can_confirm_alignment(user, d["aligned_partner_id"]):
                st.info(f"The Risk Lead ({d['decided_by']}) asked you to align on this check.")
                st.markdown(f"> {d['decision_notes']}")
                notes = st.text_area("Your sign-off notes (required)", key=f"align_notes_{cid}")
                sure = st.checkbox("I confirm I have aligned with the teams involved and sign off on this check.", key=f"align_sure_{cid}")
                c1, c2 = st.columns(2)
                if c1.button("✍️ Confirm & sign off", type="primary", key=f"align_ok_{cid}", disabled=not sure):
                    ok, ref = run_action(workflow.confirm_alignment, user, cid, notes)
                    if ok:
                        flash("success", f"{ref}: your sign-off is recorded. The Risk Lead can now clear the check."); st.rerun()
                with st.expander("Decline alignment (conflict stands)"):
                    reason = st.text_input("Reason (required)", key=f"align_no_reason_{cid}")
                    if st.button("Decline", key=f"align_no_{cid}"):
                        ok, ref = run_action(workflow.decline_alignment, user, cid, reason)
                        if ok:
                            flash("warning", f"{ref} rejected: alignment declined."); st.rerun()
            else:
                st.info(f"Waiting for **{d['aligned_partner']}** to sign off. Only the designated partner can do this.")

    # ---- Risk Lead: clear after partner sign-off
    if status == "ALIGNMENT_CONFIRMED" and auth.can(user, "decide_conflict"):
        with st.container(border=True):
            st.markdown("#### ✅ Clear after partner sign-off")
            st.success(f"{d['aligned_partner']} signed off on {d['alignment_confirmed_at']:%d %b %Y, %H:%M}: {d['alignment_notes']}")
            ok_to_review, why = review_eligibility(user, d["requested_by_id"])
            if not ok_to_review:
                st.warning("🚫 Maker-checker: " + why)
            else:
                final = st.text_area("Final clearance notes (required)", key=f"final_notes_{cid}")
                proofs = st.file_uploader("Alignment proof (optional, one or more files)", type=UPLOAD_TYPES,
                                          accept_multiple_files=True, key=f"final_proof_{cid}")
                if st.button("📜 Clear check & issue certificate", type="primary", key=f"clear_{cid}"):
                    ok, res = run_action(workflow.clear_aligned_check, user, cid, final,
                                         [{"doc_type": "ALIGNMENT_PROOF", "filename": x.name, "data": x.getvalue()} for x in proofs or []])
                    if ok:
                        flash("success", f"{res['reference_no']} cleared. Certificate issued (SHA-256 {res['certificate_sha256'][:12]}…)."); st.rerun()

    # ---- Risk Lead: client acceptance
    if status in config.ACCEPTANCE_UNLOCKED_STATES and d["acceptance"] == "PENDING" and auth.can(user, "grant_acceptance"):
        with st.container(border=True):
            st.markdown("#### 🏁 Client acceptance")
            ok_to_review, why = review_eligibility(user, d["requested_by_id"])
            if not ok_to_review:
                st.warning("🚫 Maker-checker: " + why)
            else:
                choice = st.radio("Outcome", ["Accept", "Decline"], horizontal=True, key=f"acc_choice_{cid}")
                notes = st.text_area("Notes (required)", key=f"acc_notes_{cid}")
                if st.button("Record acceptance decision", type="primary", key=f"acc_{cid}"):
                    ok, ref = run_action(workflow.record_acceptance, user, cid, choice == "Accept", notes)
                    if ok:
                        flash("success", f"{ref}: client acceptance {'granted' if choice == 'Accept' else 'declined'}."); st.rerun()


def render_detail(user: auth.SessionUser, check_id: int) -> None:
    d = get_check_detail(user, check_id)
    if d is None:
        st.warning("That record is not available to you.")
        return
    st.subheader(f"{d['reference_no']} · {d['party_name']}")
    if d["limited"]:
        st.info("You can open this check only because you are its designated aligned partner. "
                "Documents and the audit trail are restricted to the service line.")
    labels = ["Overview"]
    if not d["limited"]:
        labels.append("Documents")
    labels.append("Related parties & screening")
    if auth.can(user, "view_audit") and not d["limited"]:
        labels.append("Audit trail")
    tabs = dict(zip(labels, st.tabs(labels)))

    with tabs["Overview"]:
        a, b, c = st.columns(3)
        a.markdown(f"**Service line**  \n{d['service_line']}\n\n**Type / scope**  \n{d['party_type']} · {d['scope']}"
                   + (f" · {d['country']}" if d["country"] else ""))
        b.markdown(f"**Conflict status**  \n{pretty(d['status'])}\n\n**Decision**  \n{pretty(d['decision']) or '-'}\n\n"
                   f"**Acceptance**  \n{pretty(d['acceptance'])}")
        c.markdown(f"**Raised by**  \n{d['requested_by']}\n\n**Created**  \n{d['created_at']:%d %b %Y, %H:%M}"
                   + ("\n\n**Source**  \nLegacy import" if d["source"] == "legacy_import" else ""))
        ids = {k.upper(): d[k] for k in ("pan", "gstin", "cin", "tax_id") if d[k]}
        if ids: st.markdown("**Identifiers:** " + " · ".join(f"{k}: `{v}`" for k, v in ids.items()))
        if d["description"]: st.markdown(f"**Engagement:** {d['description']}")
        events = [("Raised", d["created_at"], d["requested_by"], None)]
        if d["decided_at"]: events.append((f"Decision: {pretty(d['decision']) or 'Rejected'}", d["decided_at"], d["decided_by"], d["decision_notes"]))
        if d["alignment_confirmed_at"]: events.append(("Partner sign-off", d["alignment_confirmed_at"], d["aligned_partner"], d["alignment_notes"]))
        if d["cleared_at"]: events.append(("Cleared", d["cleared_at"], d["cleared_by"], d["clearance_notes"]))
        if d["accepted_at"]: events.append((f"Acceptance: {pretty(d['acceptance'])}", d["accepted_at"], d["accepted_by"], d["acceptance_notes"]))
        if len(events) > 1 or d["status"] != "DRAFT":
            st.dataframe(pd.DataFrame(events, columns=["Step", "When (IST)", "By", "Notes"]), hide_index=True, column_config={
                "When (IST)": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm")})
        cert = next((x for x in d["docs"] if x["key"] == "APPROVAL_CERTIFICATE"), None)
        if cert:
            st.success(f"📜 Approval certificate issued · SHA-256 `{cert['sha256'][:24]}…` · open it in the Documents tab.")
        st.divider()
        _render_actions(user, d)

    if "Documents" in tabs:
        with tabs["Documents"]:
            _render_documents(user, d)

    with tabs["Related parties & screening"]:
        st.markdown("**Related parties**")
        st.dataframe(pd.DataFrame(d["related"]), hide_index=True) if d["related"] else st.caption("None recorded.")
        if not d["limited"]:
            st.markdown("**Conflict screening against the whole firm**")
            m, hid = workflow.screen_entity(user, name=d["party_name"], related_names=[r["Name"] for r in d["related"]],
                                            pan=d["pan"], gstin=d["gstin"], cin=d["cin"], tax_id=d["tax_id"], exclude_id=d["id"])
            render_screening(user, m, hid, typed=True)

    if "Audit trail" in tabs:
        with tabs["Audit trail"]:
            trail = audit.get_entity_trail("conflict_check", d["id"])
            if trail:
                tdf = pd.DataFrame(trail)
                tdf["ts_utc"] = tdf["ts_utc"].map(lambda x: _ist_naive(datetime.fromisoformat(x.rstrip("Z"))))
                st.dataframe(tdf.rename(columns={"ts_utc": "When (IST)", "user": "User", "action": "Action",
                                                 "old": "Before", "new": "After", "reason": "Reason"}).drop(columns=["id"]),
                             hide_index=True)
            else:
                st.caption("No audit entries.")


def page_queue() -> None:
    user = _page_user("view_records")
    st.title("📥 Action Queue")
    st.caption("Everything waiting for you: drafts to finish, checks to decide, alignments to sign off, acceptances to record.")
    q = workflow.fetch_queue(user)
    if q.empty:
        st.success("Nothing needs your action right now.")
        return
    actionable = q[~q["action"].str.startswith("Waiting")]
    m1, m2, m3 = st.columns(3)
    m1.metric("Needs your action", len(actionable))
    m2.metric("Waiting on someone else", len(q) - len(actionable))
    m3.metric(f"Older than {OVERDUE_DAYS} days", int((actionable["age_days"] > OVERDUE_DAYS).sum()))
    view = q[["id", "reference_no", "party_name", "service_line", "scope", "status", "action", "age_days", "raised_by"]]
    event = st.dataframe(view, hide_index=True, on_select="rerun", selection_mode="single-row", key="queue_tbl",
                         column_config={"id": None, "reference_no": "Reference", "party_name": "Entity", "service_line": "Line",
                                        "scope": "Scope", "status": "Status", "action": "Action needed",
                                        "age_days": st.column_config.NumberColumn("Age (days)"), "raised_by": "Raised by"})
    rows = event.selection.rows if event and event.selection else []
    if rows and rows[0] < len(q):
        st.divider()
        render_detail(user, int(q.iloc[rows[0]]["id"]))
    else:
        st.caption("Tick a row to open it and act.")


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
        export_button(user, f, "records", lambda: export_dataframe(user, f))
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
    piv = summary_pivot(user, f)
    st.dataframe(piv)
    export_button(user, f, "summary", lambda: summary_pivot(user, f).reset_index().rename(columns={"service_line": "Service line"}),
                  label="summary CSV")


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
        "excel_sync": excel_sync.status(),
        "error_log_tail": tail_file(config.LOG_DIR / "error.log", 100),
    }


def page_diagnostics() -> None:
    user = _page_user("view_audit")
    st.title("🩺 Diagnostics & Self-Repair")
    labels = ["🔗 Audit chain"]
    full = auth.can(user, "view_diagnostics")
    if full:
        labels += ["💚 System health", "🧾 Error log", "💾 Backups", "📊 Excel sync"]
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
    with tabs[4]:
        es = excel_sync.status()
        (st.error if es["last_error"] else st.success)(
            ("Last sync FAILED (will retry automatically): " + es["last_error"]) if es["last_error"]
            else "Excel tracker is in step with the database." if es["last_ok"] else "No sync has completed yet.")
        e1, e2, e3 = st.columns(3)
        e1.metric("Last successful sync", f"{es['last_ok']:%d %b %H:%M:%S}" if es["last_ok"] else "-")
        e2.metric("Records in file", es["records"] if es["records"] is not None else "-")
        e3.metric("Pending / failures", f"{'yes' if es['dirty'] else 'no'} / {es['failures']}")
        st.caption(f"Master file: `{es['master_file']}`" + (f" · modified {es['master_mtime']:%d %b %Y %H:%M:%S}" if es["master_mtime"] else ""))
        st.caption(f"Proof links in the workbook resolve against: `{config.EXCEL_LINK_ROOT}` (set CCP_LINK_ROOT to the shared path users can open).")
        if st.button("🔄 Sync now", key="excel_sync_now"):
            excel_sync.request_sync("manual", immediate=True)
            st.info("Sync queued: it runs in the background within a few seconds. Refresh this tab to see the result.")
        st.caption("The workbook is read-only OUTPUT: edits are overwritten. A locked file (open in Excel) is retried automatically; saves are never blocked.")


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
    pages = [st.Page(page_dashboard, title="Dashboard", icon="📊", url_path="dashboard", default=True),
             st.Page(page_queue, title="Action Queue", icon="📥", url_path="queue")]
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

"""
auth.py - local accounts (bcrypt), lockout, role permissions, service-line scoping, maker-checker.

Rules
  * Passwords: bcrypt (cost config.BCRYPT_ROUNDS), policy enforced, 72-byte bcrypt limit enforced.
  * New/reset passwords force a change at next login (must_change_password).
  * 5 bad passwords -> 15 min lock. Unknown usernames take the same time as wrong passwords.
  * Streamlit stores only a frozen SessionUser (never an ORM object / hash) in st.session_state.
  * Service-line isolation is enforced at QUERY level: wrap every list query in
    apply_service_line_scope(); do not rely on hidden UI widgets.
  * Re-load the SessionUser on every rerun with load_session_user(): deactivation and
    service-line changes then take effect immediately.

CLI:  python auth.py bootstrap
"""
from __future__ import annotations

import os
import re
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Sequence

import bcrypt
from sqlalchemy import delete, false, func, select

import audit
import config
from database import (RuleViolation, ServiceLine, User, UserServiceLine, read_scope, utcnow, write_scope)

log = config.get_logger("auth")

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")


class AuthError(RuleViolation):
    pass


class PermissionDenied(RuleViolation):
    pass


@dataclass(frozen=True)
class SessionUser:
    id: int
    username: str
    full_name: str
    role: str
    is_partner: bool
    must_change_password: bool
    service_line_ids: Optional[frozenset]   # None = all service lines


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    user: Optional[SessionUser]
    message: str


# ------------------------------------------------------------------ permissions
R = config
PERMISSIONS: dict[str, frozenset] = {
    "view_records":        frozenset(config.ROLES),
    "create_check":        frozenset({R.ROLE_MEMBER, R.ROLE_RISK_LEAD}),
    "edit_draft":          frozenset({R.ROLE_MEMBER, R.ROLE_RISK_LEAD}),
    "upload_proof":        frozenset({R.ROLE_MEMBER, R.ROLE_RISK_LEAD}),
    "decide_conflict":     frozenset({R.ROLE_RISK_LEAD}),
    "record_alignment":    frozenset({R.ROLE_RISK_LEAD}),
    "grant_acceptance":    frozenset({R.ROLE_RISK_LEAD}),
    "export_reports":      frozenset(config.ROLES),   # scope is enforced in the query: Members export ONLY their own lines
    "view_audit":          frozenset({R.ROLE_ADMIN, R.ROLE_RISK_LEAD, R.ROLE_AUDITOR}),
    "verify_audit":        frozenset({R.ROLE_ADMIN, R.ROLE_RISK_LEAD, R.ROLE_AUDITOR}),
    "view_diagnostics":    frozenset({R.ROLE_ADMIN, R.ROLE_RISK_LEAD}),   # health, error log, backups
    "manage_users":        frozenset({R.ROLE_ADMIN}),
    "manage_service_lines": frozenset({R.ROLE_ADMIN}),
    "run_diagnostics":     frozenset({R.ROLE_ADMIN}),
    "run_backup":          frozenset({R.ROLE_ADMIN}),
    "run_migration":       frozenset({R.ROLE_ADMIN}),
}


def can(user: Optional[SessionUser], action: str) -> bool:
    return user is not None and user.role in PERMISSIONS.get(action, frozenset())


def require(user: Optional[SessionUser], action: str) -> None:
    if not can(user, action):
        log.warning("Permission denied: user=%s action=%s", getattr(user, "username", None), action)
        raise PermissionDenied(f"You do not have permission to perform: {action}")


def can_confirm_alignment(user: SessionUser, aligned_partner_id: Optional[int]) -> bool:
    """Only the named partner (not the Risk Lead, not Admin) may confirm their own alignment."""
    return bool(user and user.is_partner and aligned_partner_id is not None and user.id == aligned_partner_id)


def assert_maker_checker(maker_id: int, checker_id: int, *, allow: bool = config.ALLOW_SELF_APPROVAL) -> None:
    if not allow and maker_id == checker_id:
        raise PermissionDenied("Maker-checker: the person who raised a check cannot approve it.")


# ------------------------------------------------------------------ service-line scoping
def apply_service_line_scope(stmt, user: SessionUser, column):
    """Add the isolation filter to any SELECT. `column` e.g. ConflictCheck.service_line_id."""
    if user.service_line_ids is None:
        return stmt
    if not user.service_line_ids:
        return stmt.where(false())
    return stmt.where(column.in_(user.service_line_ids))


def apply_check_visibility(stmt, user: SessionUser, service_line_col, partner_col):
    """Detail-view visibility: the user's service lines PLUS any check where they are the designated
    aligned partner (so a partner from another line can open it to sign off). Lists, KPIs, reports and
    exports never use this: they stay strictly service-line scoped."""
    from sqlalchemy import or_
    if user.service_line_ids is None:
        return stmt
    return stmt.where(or_(service_line_col.in_(user.service_line_ids), partner_col == user.id))


def assert_service_line_access(user: SessionUser, service_line_id: int) -> None:
    if user.service_line_ids is not None and service_line_id not in user.service_line_ids:
        raise PermissionDenied("You do not have access to this service line.")


# ------------------------------------------------------------------ passwords
def _pw_bytes(pw: str) -> bytes:
    b = (pw or "").encode("utf-8")
    if len(b) > config.PASSWORD_MAX_BYTES:
        raise AuthError(f"Password too long (max {config.PASSWORD_MAX_BYTES} bytes).")
    return b


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(_pw_bytes(pw), bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)).decode("ascii")


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(pw), pw_hash.encode("ascii"))
    except (ValueError, AuthError):
        return False


_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def validate_password_policy(pw: str, username: str = "") -> list[str]:
    problems = []
    if len(pw or "") < config.PASSWORD_MIN_LENGTH:
        problems.append(f"at least {config.PASSWORD_MIN_LENGTH} characters")
    if len((pw or "").encode("utf-8")) > config.PASSWORD_MAX_BYTES:
        problems.append(f"at most {config.PASSWORD_MAX_BYTES} bytes")
    if not re.search(r"[A-Z]", pw or ""):
        problems.append("an uppercase letter")
    if not re.search(r"[a-z]", pw or ""):
        problems.append("a lowercase letter")
    if not re.search(r"\d", pw or ""):
        problems.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", pw or ""):
        problems.append("a symbol")
    if username and username.lower() in (pw or "").lower():
        problems.append("must not contain the username")
    return problems


def generate_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if not validate_password_policy(pw):
            return pw


# ------------------------------------------------------------------ session user
def _build_session_user(s, u: User) -> SessionUser:
    if u.role in config.ALL_LINES_ROLES:
        ids = None
    else:
        ids = frozenset(s.scalars(select(UserServiceLine.service_line_id).join(
            ServiceLine, ServiceLine.id == UserServiceLine.service_line_id
        ).where(UserServiceLine.user_id == u.id, ServiceLine.is_active.is_(True))))
    return SessionUser(u.id, u.username, u.full_name, u.role, bool(u.is_partner),
                       bool(u.must_change_password), ids)


def load_session_user(user_id: int) -> Optional[SessionUser]:
    """Fresh copy from the DB; None if the account no longer exists or was deactivated."""
    with read_scope() as s:
        u = s.get(User, user_id)
        if u is None or not u.is_active:
            return None
        return _build_session_user(s, u)


# ------------------------------------------------------------------ login
def authenticate(username: str, password: str) -> AuthResult:
    uname = (username or "").strip().lower()
    generic = AuthResult(False, None, "Invalid username or password.")
    with read_scope() as s:
        u = s.scalar(select(User).where(User.username == uname))
        snap = None if u is None else (u.id, u.password_hash, bool(u.is_active), u.locked_until)
    if snap is None:                               # unknown user: same cost as a wrong password
        verify_password(password or "", _DUMMY_HASH)
        log.warning("Login attempt for unknown username %r", uname)
        return generic
    uid, pw_hash, active, locked_until = snap
    now = utcnow()
    if locked_until and locked_until > now:
        mins = int((locked_until - now).total_seconds() // 60) + 1
        return AuthResult(False, None, f"Account temporarily locked. Try again in {mins} minute(s).")
    pw_ok = verify_password(password or "", pw_hash)   # bcrypt runs OUTSIDE the write lock
    with write_scope() as s:
        u = s.get(User, uid)
        if pw_ok and active:
            u.failed_attempts, u.locked_until, u.last_login_at = 0, None, utcnow()
            audit.log_event(s, user=u, action="LOGIN_SUCCESS", entity_type="user", entity_id=u.id)
            return AuthResult(True, _build_session_user(s, u), "")
        if not active:
            audit.log_event(s, user=u, action="LOGIN_DENIED_INACTIVE", entity_type="user", entity_id=u.id)
            return generic
        u.failed_attempts = (u.failed_attempts or 0) + 1
        if u.failed_attempts >= config.MAX_FAILED_LOGINS:
            u.failed_attempts = 0
            u.locked_until = utcnow() + timedelta(minutes=config.LOCKOUT_MINUTES)
            audit.log_event(s, user=u, action="ACCOUNT_LOCKED", entity_type="user", entity_id=u.id,
                            reason=f"{config.MAX_FAILED_LOGINS} failed attempts")
        else:
            audit.log_event(s, user=u, action="LOGIN_FAILED", entity_type="user", entity_id=u.id)
        return generic


def logout(user: SessionUser) -> None:
    with write_scope() as s:
        audit.log_event(s, user=user, action="LOGOUT", entity_type="user", entity_id=user.id)


# ------------------------------------------------------------------ user management
def _set_service_lines(s, user_id: int, codes: Sequence[str]) -> list[str]:
    s.execute(delete(UserServiceLine).where(UserServiceLine.user_id == user_id))
    wanted = sorted({c.strip().upper() for c in codes if c and c.strip()})
    if not wanted:
        return []
    rows = s.execute(select(ServiceLine.id, ServiceLine.code).where(ServiceLine.code.in_(wanted))).all()
    found = {code: sid for sid, code in rows}
    unknown = [c for c in wanted if c not in found]
    if unknown:
        raise AuthError(f"Unknown service line code(s): {', '.join(unknown)}")
    for code in wanted:
        s.add(UserServiceLine(user_id=user_id, service_line_id=found[code]))
    return wanted


def _active_admin_count(s) -> int:
    return s.scalar(select(func.count()).select_from(User)
                    .where(User.role == config.ROLE_ADMIN, User.is_active.is_(True))) or 0


def create_user(actor: SessionUser, *, username: str, full_name: str, role: str,
                password: Optional[str] = None, email: Optional[str] = None,
                is_partner: bool = False, service_line_codes: Sequence[str] = ()) -> tuple[int, str]:
    """Returns (user_id, temporary_password). Show the temp password ONCE to the Admin."""
    require(actor, "manage_users")
    uname = (username or "").strip().lower()
    if not USERNAME_RE.match(uname):
        raise AuthError("Username must be 3-40 chars: lowercase letters, digits, . _ -")
    if role not in config.ROLES:
        raise AuthError(f"Invalid role: {role}")
    if role == config.ROLE_MEMBER and not service_line_codes:
        raise AuthError("A Service Line Member must be assigned at least one service line.")
    temp = password or generate_password()
    problems = validate_password_policy(temp, uname)
    if problems:
        raise AuthError("Password needs: " + "; ".join(problems))
    with write_scope() as s:
        if s.scalar(select(User.id).where(User.username == uname)):
            raise AuthError("Username already exists.")
        u = User(username=uname, full_name=full_name.strip(), email=email, role=role,
                 is_partner=is_partner, password_hash=hash_password(temp), must_change_password=True)
        s.add(u)
        s.flush()
        codes = _set_service_lines(s, u.id, service_line_codes)
        audit.log_event(s, user=actor, action="USER_CREATED", entity_type="user", entity_id=u.id,
                        new={"username": uname, "full_name": u.full_name, "role": role,
                             "is_partner": is_partner, "service_lines": codes})
        return u.id, temp


def update_user(actor: SessionUser, user_id: int, *, full_name: Optional[str] = None,
                email: Optional[str] = None, role: Optional[str] = None,
                is_active: Optional[bool] = None, is_partner: Optional[bool] = None,
                service_line_codes: Optional[Sequence[str]] = None, reason: Optional[str] = None) -> None:
    require(actor, "manage_users")
    with write_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            raise AuthError("User not found.")
        old = {"full_name": u.full_name, "email": u.email, "role": u.role, "is_active": u.is_active,
               "is_partner": u.is_partner}
        losing_admin = u.role == config.ROLE_ADMIN and u.is_active and (
            (role is not None and role != config.ROLE_ADMIN) or is_active is False)
        if losing_admin and _active_admin_count(s) <= 1:
            raise AuthError("Cannot remove or deactivate the last active Admin.")
        if full_name is not None: u.full_name = full_name.strip()
        if email is not None: u.email = email
        if role is not None: u.role = role
        if is_active is not None: u.is_active = is_active
        if is_partner is not None: u.is_partner = is_partner
        new = {"full_name": u.full_name, "email": u.email, "role": u.role, "is_active": u.is_active,
               "is_partner": u.is_partner}
        if service_line_codes is not None:
            old["service_lines"] = sorted(s.scalars(select(ServiceLine.code).join(
                UserServiceLine, UserServiceLine.service_line_id == ServiceLine.id
            ).where(UserServiceLine.user_id == u.id)))
            new["service_lines"] = _set_service_lines(s, u.id, service_line_codes)
        if u.role == config.ROLE_MEMBER and service_line_codes is not None and not new["service_lines"]:
            raise AuthError("A Service Line Member must keep at least one service line.")
        o, n = audit.diff_snapshots(old, new)
        audit.log_event(s, user=actor, action="USER_UPDATED", entity_type="user", entity_id=u.id,
                        old=o, new=n, reason=reason)


def admin_reset_password(actor: SessionUser, user_id: int) -> str:
    require(actor, "manage_users")
    temp = generate_password()
    with write_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            raise AuthError("User not found.")
        u.password_hash, u.must_change_password = hash_password(temp), True
        u.failed_attempts, u.locked_until = 0, None
        audit.log_event(s, user=actor, action="PASSWORD_RESET_BY_ADMIN", entity_type="user", entity_id=u.id)
    return temp


def change_password(user_id: int, current: str, new: str) -> None:
    with write_scope() as s:
        u = s.get(User, user_id)
        if u is None or not verify_password(current, u.password_hash):
            raise AuthError("Current password is incorrect.")
        if new == current:
            raise AuthError("New password must differ from the current one.")
        problems = validate_password_policy(new, u.username)
        if problems:
            raise AuthError("Password needs: " + "; ".join(problems))
        u.password_hash, u.must_change_password = hash_password(new), False
        audit.log_event(s, user=u, action="PASSWORD_CHANGED", entity_type="user", entity_id=u.id)


def list_users() -> list[dict]:
    with read_scope() as s:
        return [{"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role,
                 "is_partner": u.is_partner, "is_active": u.is_active,
                 "locked_until": u.locked_until, "last_login_at": u.last_login_at}
                for u in s.scalars(select(User).order_by(User.username))]


# ------------------------------------------------------------------ first run
def bootstrap_admin() -> Optional[str]:
    """Create the first Admin if no users exist. Password: $CCP_ADMIN_PASSWORD or generated
    (printed ONCE to the console). Forced to change at first login."""
    uname = os.environ.get("CCP_ADMIN_USERNAME", "admin").strip().lower()
    with read_scope() as s:
        if s.scalar(select(func.count()).select_from(User)):
            return None
    pw = os.environ.get("CCP_ADMIN_PASSWORD") or generate_password()
    problems = validate_password_policy(pw, uname)
    if problems:
        raise AuthError("CCP_ADMIN_PASSWORD needs: " + "; ".join(problems))
    with write_scope() as s:
        u = User(username=uname, full_name="System Administrator", role=config.ROLE_ADMIN,
                 password_hash=hash_password(pw), must_change_password=True)
        s.add(u)
        s.flush()
        audit.log_event(s, user=None, action="BOOTSTRAP_ADMIN_CREATED", entity_type="user",
                        entity_id=u.id, new={"username": uname})
    if not os.environ.get("CCP_ADMIN_PASSWORD"):
        print("=" * 64)
        print(f" FIRST-RUN ADMIN  username: {uname}   password: {pw}")
        print(" Shown only once. You must change it at first login.")
        print("=" * 64)
    return pw


if __name__ == "__main__":
    if sys.argv[1:] == ["bootstrap"]:
        bootstrap_admin()
    else:
        print("Usage: python auth.py bootstrap")

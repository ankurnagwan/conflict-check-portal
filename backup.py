"""
backup.py - safe online backups of app.db (SQLite backup API, never a raw file copy).

Each backup  app_<UTC stamp>_<label>.db.gz  +  app_<stamp>_<label>.manifest.json  contains:
sha256 of the archive, PRAGMA integrity_check result, audit-chain tip (id + hash), app version.
The chain tip in the manifest is an external anchor for audit-trail truncation checks.

Retention (grandfather-father-son): keep every backup for BACKUP_DAILY_KEEP_DAYS, then one per
week for BACKUP_WEEKLY_KEEP_WEEKS, then one per month for BACKUP_MONTHLY_KEEP_MONTHS.
"pre-*" backups (pre-migration, pre-upgrade, pre-restore) are kept BACKUP_PRE_EVENT_KEEP_DAYS.

CLI:  python backup.py run | list | verify <file> | restore <file>   (stop the app before restore)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config

log = config.get_logger("backup")
_NAME_RE = re.compile(r"^app_(\d{8}_\d{6})_([a-z0-9-]+)\.db\.gz$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(gz: Path) -> Path:
    return gz.with_name(gz.name.removesuffix(".db.gz") + ".manifest.json")


def _chain_tip(conn: sqlite3.Connection) -> Optional[tuple[int, str]]:
    try:
        r = conn.execute("SELECT last_id, last_hash FROM audit_head WHERE id=1").fetchone()
        return (r[0], r[1]) if r else None
    except sqlite3.Error:
        return None


# ------------------------------------------------------------------ create
def create_backup(label: str = "scheduled", actor=None) -> dict:
    config.ensure_dirs()
    label = re.sub(r"[^a-z0-9-]", "", label.lower()) or "manual"
    now = datetime.now(timezone.utc)
    base = f"app_{now.strftime('%Y%m%d_%H%M%S')}_{label}"
    raw = config.BACKUP_DIR / f"{base}.tmp.db"
    gz = config.BACKUP_DIR / f"{base}.db.gz"

    with closing(sqlite3.connect(str(config.DB_PATH), timeout=30)) as src, \
            closing(sqlite3.connect(str(raw))) as dst:
        src.backup(dst)                                   # consistent snapshot while app keeps running
        dst.execute("PRAGMA journal_mode=DELETE")         # standalone single file
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        tip = _chain_tip(dst)
    if integrity != "ok":
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"Backup aborted: integrity_check on the copy returned {integrity!r}")

    with open(raw, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink(missing_ok=True)

    manifest = {
        "file": gz.name, "created_utc": now.isoformat(), "label": label,
        "sha256": _sha256(gz), "size_bytes": gz.stat().st_size, "integrity_check": integrity,
        "audit_tip_id": tip[0] if tip else None, "audit_tip_hash": tip[1] if tip else None,
        "app_version": config.APP_VERSION,
    }
    _manifest_path(gz).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    deleted = apply_retention()
    mirrored = mirror_proofs()
    manifest["retention_deleted"], manifest["proofs_mirrored"] = deleted, mirrored
    log.info("Backup created: %s (%d bytes)", gz.name, manifest["size_bytes"])

    if label != "pre-restore":                            # logged AFTER the snapshot; included in the next one
        try:
            import audit
            from database import write_scope
            with write_scope() as s:
                audit.log_event(s, user=actor, action="BACKUP_CREATED", entity_type="backup",
                                entity_id=gz.name, new={"label": label, "sha256": manifest["sha256"]})
        except Exception:
            log.exception("Backup succeeded but could not be written to the audit log")
    return manifest


# ------------------------------------------------------------------ list / retention
def list_backups() -> list[dict]:
    out = []
    if not config.BACKUP_DIR.exists():
        return out
    for p in config.BACKUP_DIR.glob("app_*.db.gz"):
        m = _NAME_RE.match(p.name)
        if m:
            ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            out.append({"path": p, "ts": ts, "label": m.group(2), "size": p.stat().st_size})
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def apply_retention(now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    items = list_backups()
    keep: set[Path] = set()
    seen: set[tuple] = set()
    for i, b in enumerate(items):                         # newest -> oldest
        age = (now - b["ts"]).days
        iso = b["ts"].isocalendar()
        if i == 0:
            keep.add(b["path"])
        elif b["label"].startswith("pre-"):
            if age <= config.BACKUP_PRE_EVENT_KEEP_DAYS:
                keep.add(b["path"])
        elif age < config.BACKUP_DAILY_KEEP_DAYS:
            keep.add(b["path"])
        elif age < config.BACKUP_WEEKLY_KEEP_WEEKS * 7:
            key = ("w", iso[0], iso[1])
            if key not in seen:
                seen.add(key); keep.add(b["path"])
        elif age < config.BACKUP_MONTHLY_KEEP_MONTHS * 31:
            key = ("m", b["ts"].year, b["ts"].month)
            if key not in seen:
                seen.add(key); keep.add(b["path"])
    deleted = []
    for b in items:
        if b["path"] not in keep:
            b["path"].unlink(missing_ok=True)
            _manifest_path(b["path"]).unlink(missing_ok=True)
            deleted.append(b["path"].name)
    return deleted


# ------------------------------------------------------------------ proofs mirror
def mirror_proofs() -> int:
    """Incremental, never-deleting copy of PROOF_ROOT into BACKUP_DIR/proofs_mirror."""
    src, dst = config.PROOF_ROOT, config.BACKUP_DIR / "proofs_mirror"
    if not src.exists() or src.resolve() == dst.resolve():
        return 0
    copied = 0
    for root, _dirs, files in os.walk(src):
        for fn in files:
            sp = Path(root) / fn
            tp = dst / sp.relative_to(src)
            if tp.exists() and tp.stat().st_size == sp.stat().st_size and tp.stat().st_mtime >= sp.stat().st_mtime:
                continue
            tp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, tp)
            copied += 1
    return copied


# ------------------------------------------------------------------ verify / restore
def verify_backup(gz_path) -> dict:
    """The 'restore test': unpack to a temp dir, integrity-check, compare against the manifest."""
    gz = Path(gz_path)
    rep = {"file": gz.name, "ok": False, "problems": []}
    mpath = _manifest_path(gz)
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else None
    if manifest is None:
        rep["problems"].append("manifest missing")
    elif _sha256(gz) != manifest["sha256"]:
        rep["problems"].append("archive sha256 does not match manifest (file altered or corrupted)")
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "check.db"
            with gzip.open(gz, "rb") as f_in, open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            with closing(sqlite3.connect(str(out))) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    rep["problems"].append("integrity_check failed")
                tip = _chain_tip(c)
                rep["audit_rows"] = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            if manifest and tip and (tip[0], tip[1]) != (manifest["audit_tip_id"], manifest["audit_tip_hash"]):
                rep["problems"].append("audit chain tip differs from manifest")
    except Exception as exc:
        rep["problems"].append(f"unreadable: {exc}")
    rep["ok"] = not rep["problems"]
    return rep


def restore_backup(gz_path, *, confirm: bool = False) -> None:
    """Replace app.db with a verified backup. STOP THE APP FIRST."""
    if not confirm:
        raise RuntimeError("restore_backup requires confirm=True (and the app must be stopped).")
    gz = Path(gz_path)
    rep = verify_backup(gz)
    if not rep["ok"]:
        raise RuntimeError(f"Refusing to restore: backup failed verification: {rep['problems']}")
    try:
        import database
        database.engine.dispose()
    except Exception:
        pass
    if config.DB_PATH.exists():
        try:
            create_backup(label="pre-restore")
        except Exception:
            shutil.copy2(config.DB_PATH, config.DB_PATH.with_name(config.DB_PATH.name + ".pre-restore"))
    tmp = config.DB_PATH.with_name(config.DB_PATH.name + ".restore.tmp")
    with gzip.open(gz, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    for suffix in ("-wal", "-shm"):
        config.DB_PATH.with_name(config.DB_PATH.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, config.DB_PATH)
    log.warning("Database restored from %s", gz.name)


# ------------------------------------------------------------------ scheduling
def run_if_due(max_age_hours: float = config.BACKUP_INTERVAL_HOURS) -> Optional[dict]:
    sched = [b for b in list_backups() if b["label"] == "scheduled"]
    if sched and datetime.now(timezone.utc) - sched[0]["ts"] < timedelta(hours=max_age_hours):
        return None
    return create_backup("scheduled")


_scheduler_started = False
_scheduler_lock = threading.Lock()


def start_scheduler(check_every_minutes: int = 60) -> None:
    """Call once from the Streamlit app (wrap in st.cache_resource). Alternative: Windows Task
    Scheduler running `python backup.py run` daily."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def loop():
        while True:
            try:
                run_if_due()
            except Exception:
                log.exception("Scheduled backup failed")
            time.sleep(check_every_minutes * 60)

    threading.Thread(target=loop, name="ccp-backup", daemon=True).start()


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["run"]:
        print(json.dumps(create_backup("manual"), indent=2, default=str))
    elif a[:1] == ["list"]:
        for b in list_backups():
            print(b["ts"].isoformat(), b["label"], b["size"], b["path"].name)
    elif a[:1] == ["verify"] and len(a) == 2:
        r = verify_backup(a[1]); print(json.dumps(r, indent=2)); sys.exit(0 if r["ok"] else 1)
    elif a[:1] == ["restore"] and len(a) == 2:
        restore_backup(a[1], confirm=input("Type RESTORE (app stopped?): ").strip() == "RESTORE")
    else:
        print("Usage: python backup.py run | list | verify <file> | restore <file>")

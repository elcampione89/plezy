"""Plezy panel: releases, in-app update checks and system messages for a private Plezy build."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
APK_DIR = DATA_DIR / "apks"
DB_PATH = DATA_DIR / "panel.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CI_TOKEN = os.environ.get("CI_TOKEN", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or ""
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
STATIC_DIR = Path(__file__).parent / "static"

ABIS = {"armeabi-v7a", "arm64-v8a", "x86_64", "universal"}
LEVELS = {"info", "warning", "critical"}
REPEATS = {"once", "every_launch"}

APK_DIR.mkdir(parents=True, exist_ok=True)

# A missing SECRET_KEY would log everyone out on every restart, so keep one on disk.
if not SECRET_KEY:
    key_file = DATA_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    SECRET_KEY = key_file.read_text().strip()

# ---------------------------------------------------------------- database

_db_lock = threading.Lock()


@contextmanager
def db():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS releases (
              id TEXT PRIMARY KEY,
              version_name TEXT NOT NULL,
              version_code INTEGER NOT NULL,
              abi TEXT NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'draft',      -- importing | draft | published | failed
              mandatory INTEGER NOT NULL DEFAULT 0,
              file_name TEXT,
              size INTEGER,
              sha256 TEXT,
              source TEXT NOT NULL DEFAULT 'upload',
              error TEXT,
              downloads INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              published_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '',
              level TEXT NOT NULL DEFAULT 'info',
              repeat TEXT NOT NULL DEFAULT 'once',
              active INTEGER NOT NULL DEFAULT 1,
              starts_at INTEGER,
              ends_at INTEGER,
              min_version_code INTEGER,
              max_version_code INTEGER,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
              id TEXT PRIMARY KEY,
              nickname TEXT,
              model TEXT,
              os TEXT,
              abi TEXT,
              version_name TEXT,
              version_code INTEGER,
              first_seen INTEGER NOT NULL,
              last_seen INTEGER NOT NULL,
              ip TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )


DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_publish": False,          # CI builds go live straight away
    "min_version_code": 0,          # anything below this must update (no "Later")
    "check_interval_hours": 6,      # how often a running app re-checks
    "app_name": "Plezy",
}


def get_settings() -> dict[str, Any]:
    with db() as c:
        rows = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **rows}


def now() -> int:
    return int(time.time())


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r else None


# ---------------------------------------------------------------- app

app = FastAPI(title="Plezy panel", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="plezy_panel",
    max_age=60 * 60 * 24 * 30,
    same_site="strict",
    https_only=COOKIE_SECURE,
)
init_db()


def client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip"):
        if v := request.headers.get(header):
            return v
    if fwd := request.headers.get("x-forwarded-for"):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(401, "Sign in first")


def require_ci(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not CI_TOKEN or not hmac.compare_digest(token, CI_TOKEN):
        raise HTTPException(401, "Bad CI token")


def base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    return f"{proto}://{host}"


# ---------------------------------------------------------------- public (the app calls these)


@app.get("/api/v1/check")
def check(
    request: Request,
    vc: int = 0,
    vn: str = "",
    abi: str = "",
    device: str = "",
    model: str = "",
):
    """One call per launch: returns the newest build for this ABI plus any live messages."""
    settings = get_settings()
    abi = abi if abi in ABIS else ""
    os_name = request.query_params.get("os", "")[:80]

    if re.fullmatch(r"[A-Za-z0-9-]{8,64}", device or ""):
        with db() as c:
            c.execute(
                """INSERT INTO devices (id, model, os, abi, version_name, version_code, first_seen, last_seen, ip)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET model=excluded.model, os=excluded.os, abi=excluded.abi,
                     version_name=excluded.version_name, version_code=excluded.version_code,
                     last_seen=excluded.last_seen, ip=excluded.ip""",
                (device, model[:80], os_name, abi, vn[:40], vc, now(), now(), client_ip(request)),
            )

    abis = [abi, "universal"] if abi else ["armeabi-v7a", "universal"]
    with db() as c:
        latest = row(
            c.execute(
                f"""SELECT * FROM releases WHERE status='published' AND abi IN ({','.join('?' * len(abis))})
                    ORDER BY version_code DESC, published_at DESC LIMIT 1""",
                abis,
            ).fetchone()
        )
        msg_rows = c.execute(
            """SELECT id, title, body, level, repeat, updated_at FROM messages
               WHERE active=1 AND (starts_at IS NULL OR starts_at<=?) AND (ends_at IS NULL OR ends_at>?)
                 AND (min_version_code IS NULL OR ?>=min_version_code)
                 AND (max_version_code IS NULL OR ?<=max_version_code)
               ORDER BY created_at""",
            (now(), now(), vc, vc),
        ).fetchall()

    update = None
    if latest and latest["version_code"] > vc:
        # Mandatory if this release says so, or the device is older than the minimum allowed.
        mandatory = bool(latest["mandatory"]) or vc < int(settings["min_version_code"] or 0)
        update = {
            "version_name": latest["version_name"],
            "version_code": latest["version_code"],
            "notes": latest["notes"],
            "mandatory": mandatory,
            "size": latest["size"],
            "sha256": latest["sha256"],
            "url": f"{base_url(request)}/api/v1/download/{latest['id']}.apk",
        }

    messages = [
        # Editing a message gives it a new revision, so "show once" messages appear again after an edit.
        {**dict(m), "rev": f"{m['id']}:{m['updated_at']}"}
        for m in msg_rows
    ]
    return {
        "update": update,
        "messages": messages,
        "check_interval_hours": settings["check_interval_hours"],
        "server_time": now(),
    }


@app.get("/api/v1/download/{release_id}.apk")
def download(release_id: str):
    with db() as c:
        r = row(c.execute("SELECT * FROM releases WHERE id=? AND status IN ('published','draft')", (release_id,)).fetchone())
        if not r or not r["file_name"]:
            raise HTTPException(404, "No such build")
        c.execute("UPDATE releases SET downloads=downloads+1 WHERE id=?", (release_id,))
    path = APK_DIR / r["file_name"]
    if not path.exists():
        raise HTTPException(404, "File missing on server")
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=f"plezy-{r['version_name']}-{r['abi']}.apk",
    )


@app.get("/get")
@app.get("/get/{abi}")
@app.get("/plezy.apk")
@app.get("/plezy-{abi}.apk")
def get_latest(abi: str = "armeabi-v7a"):
    """Fixed direct link to the newest published build (for Downloader and app stores)."""
    if abi in {"arm64", "64"}:
        abi = "arm64-v8a"
    if abi not in ABIS:
        abi = "armeabi-v7a"
    with db() as c:
        r = row(
            c.execute(
                "SELECT * FROM releases WHERE status='published' AND abi IN (?, 'universal') AND file_name IS NOT NULL "
                "ORDER BY version_code DESC LIMIT 1",
                (abi,),
            ).fetchone()
        )
        if not r:
            raise HTTPException(404, "No published build yet")
        c.execute("UPDATE releases SET downloads=downloads+1 WHERE id=?", (r["id"],))
    path = APK_DIR / r["file_name"]
    if not path.exists():
        raise HTTPException(404, "File missing on server")
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename="plezy.apk",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- admin: auth

_login_attempts: dict[str, list[float]] = {}


class LoginBody(BaseModel):
    password: str


@app.post("/api/admin/login")
def login(body: LoginBody, request: Request):
    ip = client_ip(request)
    attempts = [t for t in _login_attempts.get(ip, []) if t > time.time() - 600]
    if len(attempts) >= 8:
        raise HTTPException(429, "Too many tries. Wait 10 minutes.")
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_PASSWORD is not set on the server")
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        attempts.append(time.time())
        _login_attempts[ip] = attempts
        raise HTTPException(401, "Wrong password")
    _login_attempts.pop(ip, None)
    request.session["admin"] = True
    return {"ok": True}


@app.post("/api/admin/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/admin/me")
def me(request: Request):
    return {"admin": bool(request.session.get("admin"))}


# ---------------------------------------------------------------- admin: overview


@app.get("/api/admin/overview")
def overview(request: Request):
    require_admin(request)
    t = now()
    with db() as c:
        devices_24h = c.execute("SELECT COUNT(*) FROM devices WHERE last_seen>?", (t - 86400,)).fetchone()[0]
        devices_7d = c.execute("SELECT COUNT(*) FROM devices WHERE last_seen>?", (t - 7 * 86400,)).fetchone()[0]
        devices_all = c.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        versions = [
            dict(r)
            for r in c.execute(
                """SELECT version_name, version_code, COUNT(*) AS count FROM devices WHERE last_seen>?
                   GROUP BY version_code, version_name ORDER BY version_code DESC""",
                (t - 30 * 86400,),
            )
        ]
        latest = row(
            c.execute(
                "SELECT version_name, version_code, published_at FROM releases WHERE status='published' "
                "ORDER BY version_code DESC LIMIT 1"
            ).fetchone()
        )
        waiting = c.execute("SELECT COUNT(*) FROM releases WHERE status='draft'").fetchone()[0]
        live_messages = c.execute(
            "SELECT COUNT(*) FROM messages WHERE active=1 AND (ends_at IS NULL OR ends_at>?)", (t,)
        ).fetchone()[0]
    return {
        "devices_24h": devices_24h,
        "devices_7d": devices_7d,
        "devices_all": devices_all,
        "versions": versions,
        "latest": latest,
        "drafts": waiting,
        "live_messages": live_messages,
        "check_url": f"{base_url(request)}/api/v1/check",
    }


# ---------------------------------------------------------------- admin: releases


def _finish_release_file(release_id: str, tmp: Path) -> None:
    """Hash, size-check and move a downloaded/uploaded APK into place."""
    head = tmp.open("rb").read(4)
    if head != b"PK\x03\x04":
        tmp.unlink(missing_ok=True)
        raise ValueError("That file isn't an APK")
    sha = hashlib.sha256()
    with tmp.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    final_name = f"{release_id}.apk"
    shutil.move(tmp, APK_DIR / final_name)
    settings = get_settings()
    with db() as c:
        current = c.execute("SELECT status, source FROM releases WHERE id=?", (release_id,)).fetchone()
        status = "draft"
        if current and current["source"] == "ci" and settings["auto_publish"]:
            status = "published"
        c.execute(
            "UPDATE releases SET file_name=?, size=?, sha256=?, status=?, error=NULL, "
            "published_at=CASE WHEN ?='published' THEN ? ELSE published_at END WHERE id=?",
            (final_name, (APK_DIR / final_name).stat().st_size, sha.hexdigest(), status, status, now(), release_id),
        )


def _import_from_url(release_id: str, url: str, expected_sha: str | None, token: str | None) -> None:
    tmp = APK_DIR / f".{release_id}.part"
    try:
        headers = {"Accept": "application/octet-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        if expected_sha:
            sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
            if sha.lower() != expected_sha.lower():
                raise ValueError("Checksum didn't match the build")
        _finish_release_file(release_id, tmp)
    except Exception as e:  # noqa: BLE001 — show any failure in the panel
        tmp.unlink(missing_ok=True)
        with db() as c:
            c.execute("UPDATE releases SET status='failed', error=? WHERE id=?", (str(e)[:500], release_id))


def _new_release(version_name: str, version_code: int, abi: str, notes: str, mandatory: bool, source: str, status: str) -> str:
    if abi not in ABIS:
        raise HTTPException(400, f"abi must be one of {sorted(ABIS)}")
    if version_code <= 0:
        raise HTTPException(400, "version_code must be a positive number")
    rid = uuid.uuid4().hex[:12]
    with db() as c:
        dup = c.execute(
            "SELECT id FROM releases WHERE version_code=? AND abi=? AND status!='failed'", (version_code, abi)
        ).fetchone()
        if dup:
            raise HTTPException(409, f"Build {version_code} for {abi} already exists")
        c.execute(
            """INSERT INTO releases (id, version_name, version_code, abi, notes, status, mandatory, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, version_name.strip()[:40], version_code, abi, notes, status, int(mandatory), source, now()),
        )
    return rid


@app.get("/api/admin/releases")
def list_releases(request: Request):
    require_admin(request)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM releases ORDER BY version_code DESC, created_at DESC")]
    return rows


@app.post("/api/admin/releases/upload")
def upload_release(
    request: Request,
    file: UploadFile = File(...),
    version_name: str = Form(...),
    version_code: int = Form(...),
    abi: str = Form("armeabi-v7a"),
    notes: str = Form(""),
    mandatory: bool = Form(False),
    publish: bool = Form(False),
):
    require_admin(request)
    rid = _new_release(version_name, version_code, abi, notes, mandatory, "upload", "importing")
    tmp = APK_DIR / f".{rid}.part"
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f, 1 << 20)
    try:
        _finish_release_file(rid, tmp)
    except ValueError as e:
        with db() as c:
            c.execute("DELETE FROM releases WHERE id=?", (rid,))
        raise HTTPException(400, str(e))
    if publish:
        _set_status(rid, "published")
    return {"id": rid}


class ImportBody(BaseModel):
    url: str
    version_name: str
    version_code: int
    abi: str = "armeabi-v7a"
    notes: str = ""
    mandatory: bool = False
    sha256: Optional[str] = None


@app.post("/api/admin/releases/import")
def import_release(body: ImportBody, request: Request, tasks: BackgroundTasks):
    require_admin(request)
    if not body.url.startswith(("https://", "http://")):
        raise HTTPException(400, "Give a web address")
    rid = _new_release(body.version_name, body.version_code, body.abi, body.notes, body.mandatory, "url", "importing")
    tasks.add_task(_import_from_url, rid, body.url, body.sha256, None)
    return {"id": rid}


class ReleasePatch(BaseModel):
    notes: Optional[str] = None
    mandatory: Optional[bool] = None
    status: Optional[str] = None
    version_name: Optional[str] = None


def _set_status(rid: str, status: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE releases SET status=?, published_at=CASE WHEN ?='published' THEN ? ELSE published_at END WHERE id=?",
            (status, status, now(), rid),
        )


@app.patch("/api/admin/releases/{rid}")
def patch_release(rid: str, body: ReleasePatch, request: Request):
    require_admin(request)
    with db() as c:
        r = row(c.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone())
        if not r:
            raise HTTPException(404, "No such build")
        if body.notes is not None:
            c.execute("UPDATE releases SET notes=? WHERE id=?", (body.notes, rid))
        if body.version_name is not None:
            c.execute("UPDATE releases SET version_name=? WHERE id=?", (body.version_name.strip()[:40], rid))
        if body.mandatory is not None:
            c.execute("UPDATE releases SET mandatory=? WHERE id=?", (int(body.mandatory), rid))
    if body.status is not None:
        if body.status not in {"draft", "published"}:
            raise HTTPException(400, "status must be draft or published")
        if body.status == "published" and not r["file_name"]:
            raise HTTPException(400, "This build has no file yet")
        _set_status(rid, body.status)
    return {"ok": True}


@app.delete("/api/admin/releases/{rid}")
def delete_release(rid: str, request: Request):
    require_admin(request)
    with db() as c:
        r = row(c.execute("SELECT file_name FROM releases WHERE id=?", (rid,)).fetchone())
        if not r:
            raise HTTPException(404, "No such build")
        c.execute("DELETE FROM releases WHERE id=?", (rid,))
    if r["file_name"]:
        (APK_DIR / r["file_name"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------- CI (GitHub Actions calls this)


class CiBody(BaseModel):
    url: str
    version_name: str
    version_code: int
    abi: str
    notes: str = ""
    sha256: Optional[str] = None
    github_token: Optional[str] = Field(default=None, description="Only for private release assets")


@app.post("/api/ci/release")
def ci_release(body: CiBody, request: Request, tasks: BackgroundTasks):
    require_ci(request)
    rid = _new_release(body.version_name, body.version_code, body.abi, body.notes, False, "ci", "importing")
    tasks.add_task(_import_from_url, rid, body.url, body.sha256, body.github_token)
    return {"id": rid, "auto_publish": get_settings()["auto_publish"]}


# ---------------------------------------------------------------- admin: messages


class MessageBody(BaseModel):
    title: str
    body: str = ""
    level: str = "info"
    repeat: str = "once"
    active: bool = True
    starts_at: Optional[int] = None
    ends_at: Optional[int] = None
    min_version_code: Optional[int] = None
    max_version_code: Optional[int] = None


def _check_message(m: MessageBody) -> None:
    if not m.title.strip():
        raise HTTPException(400, "Give the message a title")
    if m.level not in LEVELS:
        raise HTTPException(400, "level must be info, warning or critical")
    if m.repeat not in REPEATS:
        raise HTTPException(400, "repeat must be once or every_launch")
    if m.starts_at and m.ends_at and m.ends_at <= m.starts_at:
        raise HTTPException(400, "The end time must be after the start time")


@app.get("/api/admin/messages")
def list_messages(request: Request):
    require_admin(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY created_at DESC")]


@app.post("/api/admin/messages")
def create_message(m: MessageBody, request: Request):
    require_admin(request)
    _check_message(m)
    mid = uuid.uuid4().hex[:12]
    with db() as c:
        c.execute(
            """INSERT INTO messages (id, title, body, level, repeat, active, starts_at, ends_at,
                 min_version_code, max_version_code, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, m.title.strip()[:120], m.body[:4000], m.level, m.repeat, int(m.active), m.starts_at, m.ends_at,
             m.min_version_code, m.max_version_code, now(), now()),
        )
    return {"id": mid}


@app.put("/api/admin/messages/{mid}")
def update_message(mid: str, m: MessageBody, request: Request):
    require_admin(request)
    _check_message(m)
    with db() as c:
        cur = c.execute(
            """UPDATE messages SET title=?, body=?, level=?, repeat=?, active=?, starts_at=?, ends_at=?,
                 min_version_code=?, max_version_code=?, updated_at=? WHERE id=?""",
            (m.title.strip()[:120], m.body[:4000], m.level, m.repeat, int(m.active), m.starts_at, m.ends_at,
             m.min_version_code, m.max_version_code, now(), mid),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "No such message")
    return {"ok": True}


class ActiveBody(BaseModel):
    active: bool


@app.patch("/api/admin/messages/{mid}")
def toggle_message(mid: str, body: ActiveBody, request: Request):
    """Switching on/off doesn't bump the revision, so people who already saw it won't see it again."""
    require_admin(request)
    with db() as c:
        cur = c.execute("UPDATE messages SET active=? WHERE id=?", (int(body.active), mid))
        if cur.rowcount == 0:
            raise HTTPException(404, "No such message")
    return {"ok": True}


@app.delete("/api/admin/messages/{mid}")
def delete_message(mid: str, request: Request):
    require_admin(request)
    with db() as c:
        c.execute("DELETE FROM messages WHERE id=?", (mid,))
    return {"ok": True}


# ---------------------------------------------------------------- admin: devices


@app.get("/api/admin/devices")
def list_devices(request: Request):
    require_admin(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM devices ORDER BY last_seen DESC")]


class DevicePatch(BaseModel):
    nickname: str


@app.patch("/api/admin/devices/{did}")
def name_device(did: str, body: DevicePatch, request: Request):
    require_admin(request)
    with db() as c:
        c.execute("UPDATE devices SET nickname=? WHERE id=?", (body.nickname.strip()[:60] or None, did))
    return {"ok": True}


@app.delete("/api/admin/devices/{did}")
def forget_device(did: str, request: Request):
    require_admin(request)
    with db() as c:
        c.execute("DELETE FROM devices WHERE id=?", (did,))
    return {"ok": True}


# ---------------------------------------------------------------- admin: settings


class SettingsBody(BaseModel):
    auto_publish: Optional[bool] = None
    min_version_code: Optional[int] = None
    check_interval_hours: Optional[int] = None
    app_name: Optional[str] = None


@app.get("/api/admin/settings")
def read_settings(request: Request):
    require_admin(request)
    return {**get_settings(), "ci_token_set": bool(CI_TOKEN)}


@app.patch("/api/admin/settings")
def write_settings(body: SettingsBody, request: Request):
    require_admin(request)
    changes = body.model_dump(exclude_none=True)
    if "check_interval_hours" in changes and not 1 <= changes["check_interval_hours"] <= 168:
        raise HTTPException(400, "Check interval must be 1 to 168 hours")
    with db() as c:
        for k, v in changes.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )
    return get_settings()


# ---------------------------------------------------------------- the admin page


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/")
def root():
    return RedirectResponse("/admin/")


app.mount("/admin", StaticFiles(directory=STATIC_DIR, html=True), name="admin")

import json
import mimetypes
import os
import queue
import secrets
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api_client import UTPClassClient
from src.config import (
    DEFAULT_GDRIVE_FOLDER_ID,
    DOWNLOAD_DIR,
    HOST,
    PORT,
    ensure_dirs,
)
from src.downloader import (
    check_and_notify_tasks,
    sanitize_name,
    strip_html,
    sync_course,
)
from src.core.events import event_bus
from src.core.session_manager import SessionRegistry
from src.gdrive import (
    _index_local_files_as_drive_mirror,
    check_gdrive_status,
    extract_folder_id,
    get_folder_url,
    parse_course_detail_from_local,
    parse_local_catalog,
    sync_from_gdrive,
)
from src.local_ai import answer_question, build_gcal_url
from src.services.calendar_service import CalendarService
from src.services.catalog_service import CatalogService
from src.storage import StateDB

WEB_DIR = Path(__file__).resolve().parent / "src" / "web"
session_registry = SessionRegistry(event_bus)

# ---------------------------------------------------------------------------
# Real-time State: Sync, Presence & Server-Sent Events (SSE)
# ---------------------------------------------------------------------------

SYNC_STATE: Dict[str, Any] = {
    "running": False,
    "kind": "utp",  # 'utp' or 'gdrive'
    "current_course": "",
    "completed": 0,
    "total": 0,
    "files_downloaded": 0,
    "last_finished_at": None,
    "error": None,
    "logs": [],
}
SYNC_LOCK = threading.Lock()

# Active sessions: token -> {token, user_id, username, name, role, avatar_color, ip, connected_at, last_seen, current_view}
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSIONS_LOCK = threading.Lock()

# SSE subscriber queues: list ofdict(queue, token, user_id)
SSE_CLIENTS: List[Dict[str, Any]] = []
SSE_LOCK = threading.Lock()


def broadcast_event(event_type: str, payload: Any) -> None:
    msg = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
    with SSE_LOCK:
        dead = []
        for client in SSE_CLIENTS:
            try:
                client["queue"].put_nowait(msg)
            except Exception:
                dead.append(client)
        for d in dead:
            if d in SSE_CLIENTS:
                SSE_CLIENTS.remove(d)


def get_presence_snapshot(db: Optional[StateDB] = None) -> Dict[str, Any]:
    close_db = False
    if db is None:
        db = StateDB()
        close_db = True
    try:
        users = db.list_web_users()
        utp_profiles = db.list_utp_profiles()
        now_ts = time.time()

        with SESSIONS_LOCK:
            # Clean stale sessions (> 90s without heartbeat and not connected via SSE)
            active_tokens_in_sse = {c.get("token") for c in SSE_CLIENTS}
            online_by_user_id: Dict[int, List[Dict[str, Any]]] = {}
            for tok, sess in list(SESSIONS.items()):
                is_live_sse = tok in active_tokens_in_sse
                if not is_live_sse and now_ts - sess.get("last_seen_ts", 0) > 120:
                    continue
                uid = sess.get("user_id")
                if uid:
                    online_by_user_id.setdefault(uid, []).append(
                        {
                            "token_prefix": tok[:8],
                            "ip": sess.get("ip", ""),
                            "current_view": sess.get("current_view", "Cursos"),
                            "connected_at": sess.get("connected_at", ""),
                            "last_seen": sess.get("last_seen", ""),
                            "live_sse": is_live_sse,
                        }
                    )

        enriched_users = []
        online_count = 0
        for u in users:
            uid = u["id"]
            u_sessions = online_by_user_id.get(uid, [])
            is_online = len(u_sessions) > 0 and bool(u["is_allowed"])
            if is_online:
                online_count += 1
            enriched_users.append(
                {
                    "id": uid,
                    "username": u["username"],
                    "name": u["name"],
                    "role": u["role"],
                    "access_code": u["access_code"],
                    "is_allowed": bool(u["is_allowed"]),
                    "avatar_color": u.get("avatar_color") or "#3b82f6",
                    "last_seen_at": u.get("last_seen_at"),
                    "online": is_online,
                    "active_sessions": len(u_sessions),
                    "sessions": u_sessions,
                }
            )

        return {
            "online_count": online_count,
            "users": enriched_users,
            "utp_profiles": utp_profiles,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }
    finally:
        if close_db:
            db.close()


def add_sync_log(msg: str) -> None:
    with SYNC_LOCK:
        ts = datetime.now().strftime("%H:%M:%S")
        SYNC_STATE["logs"].append(f"[{ts}] {msg}")
        if len(SYNC_STATE["logs"]) > 80:
            SYNC_STATE["logs"] = SYNC_STATE["logs"][-80:]
        snapshot = dict(SYNC_STATE)
    broadcast_event("sync_status", snapshot)


def get_course_dir(course: Dict[str, Any]) -> Path:
    course_name = course.get("name", "Curso")
    class_num = course.get("classNumber", "")
    folder = sanitize_name(f"{course_name} ({class_num})" if class_num else course_name, 70)
    return DOWNLOAD_DIR / folder


def count_local_files(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for p in folder.rglob("*") if p.is_file())


def list_local_files(folder: Path) -> List[Dict[str, Any]]:
    if not folder.exists():
        return []
    items: List[Dict[str, Any]] = []
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            rel = p.relative_to(DOWNLOAD_DIR).as_posix()
            items.append(
                {
                    "name": p.name,
                    "rel_path": rel,
                    "folder": p.parent.relative_to(folder).as_posix() if p.parent != folder else "Raíz del curso",
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "ext": p.suffix.lower(),
                }
            )
    return items


# ---------------------------------------------------------------------------
# Background Sync Runners (UTP+class & Google Drive)
# ---------------------------------------------------------------------------

def run_sync_background(course_filter: str = "", all_cycles: bool = False) -> None:
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            return
        SYNC_STATE["running"] = True
        SYNC_STATE["kind"] = "utp"
        SYNC_STATE["current_course"] = "Iniciando sesión en UTP+class..."
        SYNC_STATE["completed"] = 0
        SYNC_STATE["total"] = 0
        SYNC_STATE["files_downloaded"] = 0
        SYNC_STATE["error"] = None
        SYNC_STATE["logs"] = []

    add_sync_log("Conectando a UTP+class...")
    db = StateDB()
    try:
        client = UTPClassClient()
        courses = client.get_dashboard_courses(only_current=not all_cycles)
        if course_filter:
            q = course_filter.lower()
            courses = [
                c
                for c in courses
                if q in (c.get("name") or "").lower()
                or q in (c.get("classNumber") or "").lower()
                or q == (c.get("courseId") or "").lower()
            ]

        with SYNC_LOCK:
            SYNC_STATE["total"] = len(courses)

        for idx, c in enumerate(courses, start=1):
            cname = c.get("name", "Curso")
            with SYNC_LOCK:
                SYNC_STATE["current_course"] = f"({idx}/{len(courses)}) {cname}"
            add_sync_log(f"Sincronizando: {cname}...")
            stats = sync_course(client, db, c, notify_new=False, include_media=False)
            with SYNC_LOCK:
                SYNC_STATE["completed"] = idx
                SYNC_STATE["files_downloaded"] += stats["files_downloaded"]
            add_sync_log(
                f"✓ {cname}: +{stats['files_downloaded']} archivo(s) nuevos"
            )

        check_and_notify_tasks(client, db, notify_alerts=False)
        # Re-index local files for Google Drive mirror
        folder_id = db.get_setting("gdrive_folder_id", DEFAULT_GDRIVE_FOLDER_ID)
        db.replace_gdrive_files(_index_local_files_as_drive_mirror(folder_id))
        add_sync_log("¡Sincronización UTP+class completada con éxito!")
    except Exception as exc:
        err_msg = f"{exc}"
        with SYNC_LOCK:
            SYNC_STATE["error"] = err_msg
        add_sync_log(f"ERROR: {err_msg}")
        traceback.print_exc()
    finally:
        db.close()
        with SYNC_LOCK:
            SYNC_STATE["running"] = False
            SYNC_STATE["current_course"] = ""
            SYNC_STATE["last_finished_at"] = datetime.now().strftime("%H:%M:%S")
            final_state = dict(SYNC_STATE)
        broadcast_event("sync_status", final_state)
        broadcast_event("data_updated", {"source": "utp_sync"})


def run_gdrive_sync_background() -> None:
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            return
        SYNC_STATE["running"] = True
        SYNC_STATE["kind"] = "gdrive"
        SYNC_STATE["current_course"] = "Conectando con Google Drive..."
        SYNC_STATE["completed"] = 0
        SYNC_STATE["total"] = 1
        SYNC_STATE["files_downloaded"] = 0
        SYNC_STATE["error"] = None
        SYNC_STATE["logs"] = []

    db = StateDB()
    try:
        res = sync_from_gdrive(db, download_binaries=True, log_fn=add_sync_log)
        with SYNC_LOCK:
            SYNC_STATE["completed"] = 1
            SYNC_STATE["files_downloaded"] = res.get("files_downloaded", 0)
            if not res.get("ok"):
                SYNC_STATE["error"] = res.get("error")
    except Exception as exc:
        with SYNC_LOCK:
            SYNC_STATE["error"] = str(exc)
        add_sync_log(f"ERROR Drive: {exc}")
    finally:
        db.close()
        with SYNC_LOCK:
            SYNC_STATE["running"] = False
            SYNC_STATE["current_course"] = ""
            SYNC_STATE["last_finished_at"] = datetime.now().strftime("%H:%M:%S")
            final_state = dict(SYNC_STATE)
        broadcast_event("sync_status", final_state)
        broadcast_event("data_updated", {"source": "gdrive_sync"})


# ---------------------------------------------------------------------------
# HTTP Server & API Endpoints
# ---------------------------------------------------------------------------

class ThreadedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "0.0.0.0"

    def _get_token(self, qs: Dict[str, List[str]]) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        hdr_tok = self.headers.get("X-Session-Token", "")
        if hdr_tok:
            return hdr_tok.strip()
        return qs.get("token", [""])[0].strip()

    def _verify_session(
        self, db: StateDB, qs: Dict[str, List[str]]
    ) -> Optional[Dict[str, Any]]:
        tok = self._get_token(qs)
        if not tok:
            return None
        with SESSIONS_LOCK:
            sess = SESSIONS.get(tok)
        if not sess:
            return None
        user = db.get_web_user_by_id(sess["user_id"])
        if not user:
            return None
        with SESSIONS_LOCK:
            sess["last_seen_ts"] = time.time()
            sess["last_seen"] = datetime.now().strftime("%H:%M:%S")
            sess["is_allowed"] = bool(user["is_allowed"])
            sess["role"] = user["role"]
            sess["name"] = user["name"]
        return {
            "token": tok,
            "user": user,
            "is_allowed": bool(user["is_allowed"]),
        }

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 1. Healthcheck for Dokploy
        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "service": "utp-class-cloud-panel",
                    "gdrive_folder_id": DEFAULT_GDRIVE_FOLDER_ID,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            )
            return

        # 2. Frontend UI & Modular Static Assets (CSS / JS)
        if path in ("/", "/index.html"):
            html_file = WEB_DIR / "index.html"
            raw = html_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)
            return

        if path.startswith("/static/"):
            rel_asset = path[len("/static/"):]
            asset_path = (WEB_DIR / rel_asset).resolve()
            if not str(asset_path).startswith(str(WEB_DIR.resolve())) or not asset_path.is_file():
                self.send_error(404, "Asset no encontrado")
                return
            ctype, _ = mimetypes.guess_type(str(asset_path))
            raw = asset_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (ctype or "text/plain") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)
            return

        # 3. Server-Sent Events (Real-time updates)
        if path == "/api/events":
            tok = self._get_token(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # Required for Nginx/Traefik on Dokploy
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=100)
            client_entry = {"queue": q, "token": tok}
            with SSE_LOCK:
                SSE_CLIENTS.append(client_entry)

            # Touch session & broadcast updated presence immediately
            broadcast_event("presence_update", get_presence_snapshot())

            try:
                init_msg = f"event: connected\ndata: {json.dumps({'ok': True, 'ts': datetime.now().strftime('%H:%M:%S')})}\n\n".encode("utf-8")
                self.wfile.write(init_msg)
                self.wfile.flush()

                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive comment every 15s for Dokploy proxy
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        with SESSIONS_LOCK:
                            if tok in SESSIONS:
                                SESSIONS[tok]["last_seen_ts"] = time.time()
                                SESSIONS[tok]["last_seen"] = datetime.now().strftime("%H:%M:%S")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with SSE_LOCK:
                    if client_entry in SSE_CLIENTS:
                        SSE_CLIENTS.remove(client_entry)
                broadcast_event("presence_update", get_presence_snapshot())
            return

        # 4. Public / Session Bootstrap info (requires explicit login)
        if path == "/api/auth/session":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                all_users = db.list_web_users()
                users_public = [
                    {
                        "id": u["id"],
                        "username": u["username"],
                        "name": u["name"],
                        "role": u["role"],
                        "is_allowed": bool(u["is_allowed"]),
                        "avatar_color": u.get("avatar_color") or "#3b82f6",
                    }
                    for u in all_users
                ]

                if ver:
                    u = ver["user"]
                    self._send_json(
                        {
                            "authenticated": True,
                            "token": ver["token"],
                            "is_allowed": ver["is_allowed"],
                            "user": {
                                "id": u["id"],
                                "username": u["username"],
                                "name": u["name"],
                                "role": u["role"],
                                "is_allowed": bool(u["is_allowed"]),
                                "avatar_color": u.get("avatar_color") or "#3b82f6",
                            },
                            "profiles": users_public,
                        }
                    )
                else:
                    self._send_json(
                        {
                            "authenticated": False,
                            "is_allowed": False,
                            "user": None,
                            "profiles": users_public,
                        }
                    )
            finally:
                db.close()
            return

        if path == "/api/calendar":
            db = StateDB()
            try:
                self._send_json(CalendarService.get_calendar_payload(db))
            finally:
                db.close()
            return

        if path == "/api/calendar/ics":
            db = StateDB()
            try:
                raw = CalendarService.export_ics(db)
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="utp_calendario.ics"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            finally:
                db.close()
            return

        if path == "/api/sync-status":
            with SYNC_LOCK:
                self._send_json(dict(SYNC_STATE))
            return

        if path == "/api/presence":
            db = StateDB()
            try:
                self._send_json(get_presence_snapshot(db))
            finally:
                db.close()
            return

        if path == "/api/gdrive/status":
            db = StateDB()
            try:
                status_info = check_gdrive_status(db)
                status_info["files"] = db.list_gdrive_files()[:300]
                self._send_json(status_info)
            finally:
                db.close()
            return

        # 5. Overview (Courses + Pending Tasks + Active UTP Profile + Google Drive Status)
        if path == "/api/overview":
            all_cycles = qs.get("all_cycles", ["0"])[0] == "1"
            force_live = qs.get("live", ["0"])[0] == "1"
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if ver and not ver["is_allowed"]:
                    self._send_json(
                        {
                            "error": "Tu perfil ha sido desconectado por el Administrador.",
                            "disconnected_by_admin": True,
                        },
                        status=403,
                    )
                    return

                active_utp = db.get_active_utp_profile()
                gdrive_folder_id = extract_folder_id(
                    db.get_setting("gdrive_folder_id", DEFAULT_GDRIVE_FOLDER_ID)
                )
                gdrive_url = get_folder_url(gdrive_folder_id)

                # Ensure local files are indexed in gdrive_files on first run
                if not db.list_gdrive_files():
                    db.replace_gdrive_files(_index_local_files_as_drive_mirror(gdrive_folder_id))

                global_feed = CatalogService.get_global_feed(db)
                courses_out = global_feed["courses"]
                tasks_out = global_feed["pending_tasks"]
                all_evaluations = global_feed["all_evaluations"]
                all_announcements = global_feed["all_announcements"]
                data_source = "Google Drive / Local Cache"
                student_info = {
                    "name": (active_utp.get("student_name") or active_utp.get("label") or "Datos Google Drive")
                    if active_utp
                    else "Modo Google Drive Cloud",
                    "username": active_utp.get("username", "Drive") if active_utp else "Drive",
                    "student_id": active_utp.get("student_id", "") if active_utp else "",
                    "utp_connected": bool(active_utp and active_utp.get("is_connected")),
                }

                # Try live UTP+class API if requested OR if local catalog is empty and active UTP profile is connected
                if active_utp and active_utp.get("is_connected") and (force_live or not courses_out):
                    try:
                        client = UTPClassClient()
                        courses_raw = client.get_dashboard_courses(only_current=not all_cycles)
                        live_courses = []
                        for c in courses_raw:
                            cdir = get_course_dir(c)
                            first = c.get("teacherFirstName") or ""
                            last = c.get("teacherLastName") or ""
                            dl_cnt = count_local_files(cdir)
                            live_courses.append(
                                {
                                    "courseId": c.get("courseId"),
                                    "sectionId": c.get("sectionId"),
                                    "name": c.get("name"),
                                    "classNumber": c.get("classNumber"),
                                    "sectionCode": c.get("sectionCode"),
                                    "period": c.get("period"),
                                    "modality": c.get("modality"),
                                    "progress": c.get("progress", 0),
                                    "teacher": f"{first} {last}".strip() or "Por asignar",
                                    "teacherEmail": c.get("teacherEmail") or "",
                                    "localFolder": str(cdir),
                                    "downloadedCount": dl_cnt,
                                    "driveCount": dl_cnt,
                                    "source": "utp_live",
                                }
                            )
                        courses_out = live_courses
                        tasks_out = check_and_notify_tasks(client, db, notify_alerts=False)
                        student_info = {
                            "name": client.session_info.get("name") or student_info["name"],
                            "username": client.session_info.get("username") or student_info["username"],
                            "student_id": client.student_id,
                            "utp_connected": True,
                        }
                        data_source = "UTP+class En Vivo + Google Drive"
                    except Exception as live_exc:
                        # Fallback gracefully to Google Drive / Local catalog
                        data_source = f"Google Drive / Local (UTP offline: {live_exc})"

                with SYNC_LOCK:
                    st = dict(SYNC_STATE)

                self._send_json(
                    {
                        "student": student_info,
                        "data_source": data_source,
                        "gdrive": {
                            "folder_id": gdrive_folder_id,
                            "folder_url": gdrive_url,
                        },
                        "courses": courses_out,
                        "tasks": tasks_out,
                        "evaluations": all_evaluations,
                        "announcements": all_announcements,
                        "sync": st,
                        "presence": get_presence_snapshot(db),
                        "download_dir": str(DOWNLOAD_DIR),
                    }
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            finally:
                db.close()
            return

        # 6. Course Detail (Fast Local/Drive parser + optional Live UTP+ API)
        if path == "/api/course":
            course_id = qs.get("courseId", [""])[0]
            section_id = qs.get("sectionId", [""])[0]
            course_name = qs.get("name", ["Curso"])[0]
            class_num = qs.get("classNumber", [""])[0]
            force_live = qs.get("live", ["0"])[0] == "1"

            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if ver and not ver["is_allowed"]:
                    self._send_json(
                        {"error": "Usuario desconectado por el Administrador.", "disconnected_by_admin": True},
                        status=403,
                    )
                    return

                # Always parse local/Drive catalog first (takes <10ms!)
                local_detail = parse_course_detail_from_local(course_name, class_num, db)
                active_utp = db.get_active_utp_profile()

                if not force_live and (local_detail["weeks"] or local_detail["localFiles"]):
                    self._send_json(local_detail)
                    return

                if not active_utp or not active_utp.get("is_connected"):
                    self._send_json(local_detail)
                    return

                # Live UTP+class fetch when requested or when course isn't downloaded yet
                client = UTPClassClient()
                full = client.get_course_full(course_id, section_id)
                syl = {}
                try:
                    syl = client.get_course_syllabus(course_id, section_id) or {}
                except Exception:
                    pass

                weeks_map: Dict[int, List[Dict[str, Any]]] = {}
                for u in full.get("unities", []):
                    for t in u.get("themes", []):
                        if not t.get("isVisible", True):
                            continue
                        w = int(t.get("weekNumber") or 0)
                        items_out = []
                        for item in t.get("contents", []):
                            if not item.get("isVisible", True):
                                continue
                            meta = item.get("metadata") or {}
                            items_out.append(
                                {
                                    "contentId": item.get("contentId"),
                                    "title": item.get("title"),
                                    "type": item.get("type"),
                                    "url": meta.get("url"),
                                    "filename": meta.get("filename"),
                                    "html": meta.get("contentHtml"),
                                    "status": meta.get("status"),
                                }
                            )
                        weeks_map.setdefault(w, []).append(
                            {
                                "themeId": t.get("themeId"),
                                "name": t.get("name"),
                                "weekNumber": w,
                                "contents": items_out,
                            }
                        )

                weeks_list = [
                    {
                        "weekNumber": w,
                        "label": f"Semana {w:02d}" if w > 0 else "Información General",
                        "themes": themes,
                    }
                    for w, themes in sorted(weeks_map.items(), key=lambda x: x[0])
                ]

                tasks_out = []
                for kind, fetcher in (
                    ("HOMEWORK", client.get_course_homework),
                    ("EVALUATION", client.get_course_evaluations),
                ):
                    try:
                        data = fetcher(course_id, section_id)
                        for item in data.get("content", []):
                            meta = item.get("metadata") or {}
                            tasks_out.append(
                                {
                                    "id": item.get("id"),
                                    "kind": kind,
                                    "title": item.get("title"),
                                    "status": item.get("statusActivityStudent") or item.get("status"),
                                    "visibleFrom": item.get("visibleFrom"),
                                    "visibleTo": item.get("visibleTo"),
                                    "score": meta.get("score"),
                                    "topScore": meta.get("topScore"),
                                    "isQualified": meta.get("isQualified"),
                                    "weekNumber": (item.get("contentDetail") or {}).get("weekNumber"),
                                    "description": strip_html(item.get("description") or ""),
                                    "descriptionHtml": item.get("description") or "",
                                }
                            )
                    except Exception:
                        pass

                anns_out = []
                try:
                    for entry in client.get_course_announcements(course_id, section_id):
                        ann = entry.get("announcement") or {}
                        anns_out.append(
                            {
                                "id": ann.get("id"),
                                "title": ann.get("title"),
                                "publishAt": ann.get("publishAt"),
                                "author": ann.get("authorName"),
                                "descriptionHtml": ann.get("description") or "",
                                "descriptionText": strip_html(ann.get("description") or ""),
                            }
                        )
                except Exception:
                    pass

                cdir = get_course_dir({"name": course_name, "classNumber": class_num})
                self._send_json(
                    {
                        "courseId": course_id,
                        "sectionId": section_id,
                        "currentWeek": full.get("currentWeek"),
                        "numWeeks": full.get("numWeeks"),
                        "syllabusUrl": syl.get("syllabusUrl") if isinstance(syl, dict) else local_detail["syllabusUrl"],
                        "weeks": weeks_list or local_detail["weeks"],
                        "tasks": tasks_out or local_detail["tasks"],
                        "announcements": anns_out or local_detail["announcements"],
                        "localFolder": str(cdir),
                        "localFiles": list_local_files(cdir),
                        "driveFiles": local_detail["driveFiles"],
                        "source": "utp_live",
                    }
                )
            except Exception:
                # Fallback to local/Drive detail
                self._send_json(local_detail)
            finally:
                db.close()
            return

        # 7. Serve Local / Synced Files
        if path.startswith("/files/"):
            rel = urllib.parse.unquote(path[len("/files/"):])
            target = (DOWNLOAD_DIR / rel).resolve()
            if not str(target).startswith(str(DOWNLOAD_DIR.resolve())) or not target.is_file():
                self.send_error(404, "Archivo no encontrado")
                return
            ctype, _ = mimetypes.guess_type(str(target))
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_error(404, "Ruta no encontrada")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        # 1. Login / Switch Profile
        if path == "/api/auth/login":
            username = (body.get("username") or "").strip()
            access_code = (body.get("access_code") or "").strip()
            db = StateDB()
            try:
                user = db.get_web_user_by_username(username)
                if not user:
                    self._send_json({"ok": False, "error": "Perfil no encontrado."}, status=404)
                    return
                if user["access_code"] and user["access_code"] != access_code:
                    self._send_json({"ok": False, "error": "Clave / PIN incorrecto."}, status=401)
                    return
                if not user["is_allowed"]:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "Este usuario está DESCONECTADO por el Administrador.",
                            "disconnected_by_admin": True,
                        },
                        status=403,
                    )
                    return

                db.touch_web_user(user["id"])
                token = secrets.token_hex(20)
                now_str = datetime.now().strftime("%H:%M:%S")
                with SESSIONS_LOCK:
                    SESSIONS[token] = {
                        "token": token,
                        "user_id": user["id"],
                        "username": user["username"],
                        "name": user["name"],
                        "role": user["role"],
                        "avatar_color": user.get("avatar_color") or "#3b82f6",
                        "ip": self._client_ip(),
                        "connected_at": now_str,
                        "last_seen": now_str,
                        "last_seen_ts": time.time(),
                        "current_view": "Cursos",
                    }
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                self._send_json(
                    {
                        "ok": True,
                        "token": token,
                        "user": {
                            "id": user["id"],
                            "username": user["username"],
                            "name": user["name"],
                            "role": user["role"],
                            "is_allowed": bool(user["is_allowed"]),
                            "avatar_color": user.get("avatar_color") or "#3b82f6",
                        },
                    }
                )
            finally:
                db.close()
            return

        # 2. Logout
        if path == "/api/auth/logout":
            tok = self._get_token(qs) or body.get("token", "")
            with SESSIONS_LOCK:
                SESSIONS.pop(tok, None)
            broadcast_event("presence_update", get_presence_snapshot())
            self._send_json({"ok": True})
            return

        # 3. Heartbeat / View Activity Update
        if path == "/api/presence/ping":
            tok = self._get_token(qs) or body.get("token", "")
            view_name = body.get("view", "Cursos")
            db = StateDB()
            try:
                with SESSIONS_LOCK:
                    sess = SESSIONS.get(tok)
                    if sess:
                        sess["last_seen_ts"] = time.time()
                        sess["last_seen"] = datetime.now().strftime("%H:%M:%S")
                        sess["current_view"] = view_name
                        uid = sess["user_id"]
                    else:
                        uid = None
                if uid:
                    db.touch_web_user(uid)
                    u = db.get_web_user_by_id(uid)
                    if u and not u["is_allowed"]:
                        self._send_json({"ok": False, "disconnected_by_admin": True}, status=403)
                        return
                self._send_json({"ok": True})
            finally:
                db.close()
            return

        # 4. Admin: Connect / Disconnect Web User in Real Time
        if path == "/api/admin/users/toggle":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                target_id = int(body.get("user_id", 0))
                is_allowed = bool(body.get("is_allowed", False))
                updated = db.set_web_user_allowed(target_id, is_allowed)
                if not updated:
                    self._send_json({"ok": False, "error": "Usuario no encontrado."}, status=404)
                    return

                # If disconnected, immediately notify all connected tabs & mark sessions
                if not is_allowed:
                    with SESSIONS_LOCK:
                        for tok, s in list(SESSIONS.items()):
                            if s.get("user_id") == target_id:
                                s["is_allowed"] = False

                broadcast_event(
                    "user_access_changed",
                    {
                        "user_id": target_id,
                        "username": updated["username"],
                        "name": updated["name"],
                        "is_allowed": bool(updated["is_allowed"]),
                    },
                )
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                self._send_json({"ok": True, "user": updated, "presence": snap})
            finally:
                db.close()
            return

        # 5. Admin: Create or Update Web User Profile
        if path == "/api/admin/users/save":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                username = (body.get("username") or "").strip()
                name = (body.get("name") or username).strip()
                access_code = (body.get("access_code") or "1234").strip()
                role = body.get("role") if body.get("role") in ("admin", "user") else "user"
                is_allowed = bool(body.get("is_allowed", True))
                avatar_color = body.get("avatar_color") or "#3b82f6"
                user_id = body.get("id")
                if not username:
                    self._send_json({"ok": False, "error": "El usuario es obligatorio."}, status=400)
                    return
                saved = db.create_or_update_web_user(
                    username=username,
                    name=name,
                    access_code=access_code,
                    role=role,
                    is_allowed=is_allowed,
                    avatar_color=avatar_color,
                    user_id=int(user_id) if user_id else None,
                )
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                self._send_json({"ok": True, "user": saved, "presence": snap})
            finally:
                db.close()
            return

        # 6. Admin: Delete Web User Profile
        if path == "/api/admin/users/delete":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                target_id = int(body.get("user_id", 0))
                if target_id == ver["user"]["id"]:
                    self._send_json({"ok": False, "error": "No puedes eliminar tu propio perfil Admin activo."}, status=400)
                    return
                db.delete_web_user(target_id)
                with SESSIONS_LOCK:
                    for tok, s in list(SESSIONS.items()):
                        if s.get("user_id") == target_id:
                            SESSIONS.pop(tok, None)
                broadcast_event("user_access_changed", {"user_id": target_id, "is_allowed": False})
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                self._send_json({"ok": True, "presence": snap})
            finally:
                db.close()
            return

        # 7. Admin: Connect / Disconnect / Switch UTP+class Account Profile
        if path == "/api/admin/utp-profiles/toggle":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                pid = int(body.get("profile_id", 0))
                is_connected = bool(body.get("is_connected", True))
                make_active = body.get("make_active")
                updated = db.set_utp_profile_connected(
                    pid, is_connected, make_active=bool(make_active) if make_active is not None else None
                )
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                broadcast_event("data_updated", {"source": "utp_profile_toggle"})
                self._send_json({"ok": True, "profile": updated, "presence": snap})
            finally:
                db.close()
            return

        # 8. Admin: Add / Update UTP+class Account Profile
        if path == "/api/admin/utp-profiles/save":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                username = (body.get("username") or "").strip()
                password = (body.get("password") or "").strip()
                label = (body.get("label") or f"Cuenta {username}").strip()
                if not username:
                    self._send_json({"ok": False, "error": "El código UTP es obligatorio."}, status=400)
                    return
                saved = db.upsert_utp_profile(
                    username=username,
                    password=password,
                    label=label,
                    is_connected=True,
                    make_active=bool(body.get("make_active", True)),
                )
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                broadcast_event("data_updated", {"source": "utp_profile_saved"})
                self._send_json({"ok": True, "profile": saved, "presence": snap})
            finally:
                db.close()
            return

        # 9. Admin: Delete UTP+class Profile
        if path == "/api/admin/utp-profiles/delete":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                pid = int(body.get("profile_id", 0))
                db.delete_utp_profile(pid)
                snap = get_presence_snapshot(db)
                broadcast_event("presence_update", snap)
                self._send_json({"ok": True, "presence": snap})
            finally:
                db.close()
            return

        # 10. Google Drive Settings & Sync Trigger
        if path == "/api/gdrive/config":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                if not ver or ver["user"]["role"] != "admin":
                    self._send_json({"ok": False, "error": "Requiere permisos de Administrador."}, status=403)
                    return
                raw_folder = (body.get("folder_url_or_id") or DEFAULT_GDRIVE_FOLDER_ID).strip()
                fid = extract_folder_id(raw_folder)
                db.set_setting("gdrive_folder_id", fid)
                db.set_setting("gdrive_folder_url", get_folder_url(fid))
                if "api_key" in body:
                    db.set_setting("gdrive_api_key", (body.get("api_key") or "").strip())
                status_info = check_gdrive_status(db)
                broadcast_event("data_updated", {"source": "gdrive_config"})
                self._send_json({"ok": True, "status": status_info})
            finally:
                db.close()
            return

        if path == "/api/gdrive/sync":
            t = threading.Thread(target=run_gdrive_sync_background, daemon=True)
            t.start()
            self._send_json({"ok": True})
            return

        # 11. Local AI Assistant & Calendar Reminders
        if path == "/api/ai/ask":
            query = (body.get("query") or "").strip()
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                uname = ver["user"]["username"] if ver else "admin"
                result = answer_question(query, db, username=uname)
                if result.get("reminder_created"):
                    broadcast_event("calendar_updated", {"reminder": result["reminder_created"]})
                self._send_json({"ok": True, **result})
            finally:
                db.close()
            return

        if path == "/api/calendar/add":
            db = StateDB()
            try:
                ver = self._verify_session(db, qs)
                uname = ver["user"]["username"] if ver else "admin"
                title = (body.get("title") or "").strip()
                event_date = (body.get("event_date") or datetime.now().strftime("%Y-%m-%d")).strip()
                event_time = (body.get("event_time") or "09:00").strip()
                course_name = (body.get("course_name") or "").strip()
                category = (body.get("category") or "recordatorio").strip()
                recurrence = (body.get("recurrence") or "none").strip()
                notes = (body.get("notes") or "").strip()
                if not title:
                    self._send_json({"ok": False, "error": "El título es obligatorio."}, status=400)
                    return
                created = db.add_reminder(
                    title=title,
                    event_date=event_date,
                    event_time=event_time,
                    course_name=course_name,
                    category=category,
                    recurrence=recurrence,
                    notes=notes,
                    created_by=uname,
                )
                created["gcal_url"] = build_gcal_url(
                    created["title"], created["event_date"], created["event_time"], f"Curso: {course_name}"
                )
                broadcast_event("calendar_updated", {"reminder": created})
                self._send_json({"ok": True, "reminder": created})
            finally:
                db.close()
            return

        if path == "/api/calendar/toggle":
            db = StateDB()
            try:
                rid = int(body.get("id", 0))
                completed = bool(body.get("completed", True))
                updated = db.toggle_reminder(rid, completed)
                broadcast_event("calendar_updated", {"id": rid})
                self._send_json({"ok": True, "reminder": updated})
            finally:
                db.close()
            return

        if path == "/api/calendar/delete":
            db = StateDB()
            try:
                rid = int(body.get("id", 0))
                db.delete_reminder(rid)
                broadcast_event("calendar_updated", {"id": rid})
                self._send_json({"ok": True})
            finally:
                db.close()
            return

        # 12. UTP+class Sync Trigger
        if path == "/api/sync":
            course_filter = body.get("course", "")
            all_cycles = bool(body.get("all_cycles", False))
            t = threading.Thread(
                target=run_sync_background,
                args=(course_filter, all_cycles),
                daemon=True,
            )
            t.start()
            self._send_json({"ok": True})
            return

        # 12. Open Local Folder (Desktop helper; returns graceful message on Dokploy Linux container)
        if path == "/api/open-folder":
            folder_str = body.get("path", "")
            target = Path(folder_str).resolve() if folder_str else DOWNLOAD_DIR.resolve()
            target.mkdir(parents=True, exist_ok=True)
            try:
                if hasattr(os, "startfile"):
                    os.startfile(str(target))
                    self._send_json({"ok": True, "opened": str(target)})
                else:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "Ejecutando en servidor remoto (Dokploy). Usa el botón de Google Drive o abre los archivos desde la tabla web.",
                        }
                    )
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        self.send_error(404, "Ruta no encontrada")


def main() -> None:
    ensure_dirs()
    # Initialize DB & index existing local downloads for Google Drive mirror
    db = StateDB()
    try:
        fid = db.get_setting("gdrive_folder_id", DEFAULT_GDRIVE_FOLDER_ID)
        if not db.list_gdrive_files():
            db.replace_gdrive_files(_index_local_files_as_drive_mirror(fid))
    finally:
        db.close()

    server = ThreadedServer((HOST, PORT), DashboardHandler)
    # Also listen on common Dokploy/Nixpacks default ports (3000, 8080, 8000, 5000, 80)
    extra_ports = [p for p in (3000, 8080, 8000, 5000, 80) if p != PORT]
    for ep in extra_ports:
        try:
            extra_srv = ThreadedServer((HOST, ep), DashboardHandler)
            threading.Thread(target=extra_srv.serve_forever, daemon=True).start()
            print(f"  Listening (extra port for Dokploy): http://{HOST}:{ep}")
        except Exception:
            pass

    local_url = f"http://127.0.0.1:{PORT}"
    print("====================================================")
    print(f"  UTP+class Cloud & Admin Panel iniciado")
    print(f"  Listening: http://{HOST}:{PORT} (Local: {local_url})")
    print(f"  Google Drive ID: {DEFAULT_GDRIVE_FOLDER_ID}")
    print("====================================================")
    if os.name == "nt" and os.getenv("NO_BROWSER", "") != "1":
        try:
            webbrowser.open(local_url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()

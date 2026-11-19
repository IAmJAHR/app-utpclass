import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    DB_PATH,
    GDRIVE_API_KEY,
    GDRIVE_FOLDER_ID,
    GDRIVE_SERVICE_ACCOUNT_JSON,
    SESSION_PATH,
    UTP_PASSWORD,
    UTP_USERNAME,
    ensure_dirs,
)


class StateDB:
    def __init__(self) -> None:
        ensure_dirs()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        self._seed_defaults()

    def _init_tables(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS downloaded_files (
                file_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                etag TEXT,
                downloaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_activities (
                activity_id TEXT PRIMARY KEY,
                course_name TEXT NOT NULL,
                title TEXT NOT NULL,
                type TEXT NOT NULL,
                finish_at TEXT,
                status TEXT,
                first_seen_at TEXT NOT NULL,
                notified_new INTEGER DEFAULT 0,
                notified_24h INTEGER DEFAULT 0,
                notified_3h INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS seen_announcements (
                announcement_id TEXT PRIMARY KEY,
                course_name TEXT NOT NULL,
                title TEXT NOT NULL,
                publish_at TEXT,
                notified_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_themes (
                theme_id TEXT PRIMARY KEY,
                course_name TEXT NOT NULL,
                week_number INTEGER,
                name TEXT NOT NULL,
                notified_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                access_code TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_allowed INTEGER NOT NULL DEFAULT 1,
                avatar_color TEXT DEFAULT '#e11d48',
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS utp_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                label TEXT NOT NULL,
                student_name TEXT DEFAULT '',
                student_id TEXT DEFAULT '',
                is_connected INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 0,
                session_json TEXT DEFAULT '',
                last_sync_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gdrive_files (
                file_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                course_folder TEXT DEFAULT '',
                mime_type TEXT DEFAULT '',
                size_kb REAL DEFAULT 0,
                web_view_link TEXT DEFAULT '',
                download_link TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                course_name TEXT DEFAULT '',
                category TEXT DEFAULT 'recordatorio',
                event_date TEXT NOT NULL,
                event_time TEXT DEFAULT '09:00',
                recurrence TEXT DEFAULT 'none',
                notes TEXT DEFAULT '',
                completed INTEGER DEFAULT 0,
                created_by TEXT DEFAULT 'admin',
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def _seed_defaults(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        # 1. Seed default Admin and Student web users
        existing_users = self.conn.execute("SELECT COUNT(*) as c FROM web_users").fetchone()["c"]
        if existing_users == 0:
            admin_user = ADMIN_USERNAME or "admin"
            admin_pass = ADMIN_PASSWORD or "admin123"
            self.conn.execute(
                """
                INSERT OR IGNORE INTO web_users
                (username, name, access_code, role, is_allowed, avatar_color, created_at, last_seen_at)
                VALUES (?, ?, ?, 'admin', 1, '#e11d48', ?, ?)
                """,
                (admin_user, "Administrador (Tú)", admin_pass, now, now),
            )
            if UTP_USERNAME and UTP_USERNAME.lower() != admin_user.lower():
                student_name = UTP_USERNAME
                if SESSION_PATH.exists():
                    try:
                        sdata = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
                        student_name = sdata.get("name") or UTP_USERNAME
                    except Exception:
                        pass
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO web_users
                    (username, name, access_code, role, is_allowed, avatar_color, created_at, last_seen_at)
                    VALUES (?, ?, ?, 'admin', 1, '#3b82f6', ?, ?)
                    """,
                    (UTP_USERNAME, f"{student_name} (Admin)", UTP_PASSWORD or admin_pass, now, now),
                )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO web_users
                (username, name, access_code, role, is_allowed, avatar_color, created_at)
                VALUES ('invitado', 'Estudiante Invitado', 'utp2026', 'user', 1, '#10b981', ?)
                """,
                (now,),
            )
            self.conn.commit()

        # 2. Seed default UTP+class profile from .env / session.json
        existing_utp = self.conn.execute("SELECT COUNT(*) as c FROM utp_profiles").fetchone()["c"]
        if existing_utp == 0 and UTP_USERNAME:
            sess_raw = ""
            st_name = ""
            st_id = ""
            if SESSION_PATH.exists():
                try:
                    sess_raw = SESSION_PATH.read_text(encoding="utf-8")
                    sdata = json.loads(sess_raw)
                    st_name = sdata.get("name", "")
                    st_id = sdata.get("student_id", "")
                except Exception:
                    pass
            self.conn.execute(
                """
                INSERT OR IGNORE INTO utp_profiles
                (username, password, label, student_name, student_id, is_connected, is_active, session_json, created_at)
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    UTP_USERNAME,
                    UTP_PASSWORD,
                    st_name or f"Cuenta {UTP_USERNAME}",
                    st_name,
                    st_id,
                    sess_raw,
                    now,
                ),
            )
            self.conn.commit()

        # 3. Seed Google Drive settings
        default_settings = {
            "gdrive_folder_id": GDRIVE_FOLDER_ID,
            "gdrive_folder_url": f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}?hl=es-419",
            "gdrive_api_key": GDRIVE_API_KEY,
            "gdrive_service_account_json": GDRIVE_SERVICE_ACCOUNT_JSON,
            "gdrive_auto_sync": "1",
        }
        for k, v in default_settings.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        self.conn.commit()

        # 4. Seed gdrive_files from data/gdrive_manifest.json if table is empty
        existing_gfiles = self.conn.execute("SELECT COUNT(*) as c FROM gdrive_files").fetchone()["c"]
        manifest_path = DB_PATH.parent / "gdrive_manifest.json"
        if existing_gfiles == 0 and manifest_path.exists():
            try:
                mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
                files_list = mdata.get("files", [])
                if files_list:
                    self.replace_gdrive_files(files_list)
            except Exception:
                pass

    # --- Settings ---
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_all_settings(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- Web Users (Profiles & Admin Connect/Disconnect) ---
    def list_web_users(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, username, name, access_code, role, is_allowed, avatar_color, created_at, last_seen_at FROM web_users ORDER BY role ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_web_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM web_users WHERE lower(username) = lower(?)",
            (username.strip(),),
        ).fetchone()
        return dict(row) if row else None

    def get_web_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM web_users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_or_update_web_user(
        self,
        username: str,
        name: str,
        access_code: str,
        role: str = "user",
        is_allowed: bool = True,
        avatar_color: str = "#3b82f6",
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        username = username.strip()
        if user_id:
            self.conn.execute(
                """
                UPDATE web_users
                SET username = ?, name = ?, access_code = ?, role = ?, is_allowed = ?, avatar_color = ?
                WHERE id = ?
                """,
                (username, name, access_code, role, 1 if is_allowed else 0, avatar_color, user_id),
            )
            self.conn.commit()
            return self.get_web_user_by_id(user_id) or {}

        self.conn.execute(
            """
            INSERT INTO web_users (username, name, access_code, role, is_allowed, avatar_color, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                name = excluded.name,
                access_code = excluded.access_code,
                role = excluded.role,
                is_allowed = excluded.is_allowed,
                avatar_color = excluded.avatar_color
            """,
            (username, name, access_code, role, 1 if is_allowed else 0, avatar_color, now),
        )
        self.conn.commit()
        return self.get_web_user_by_username(username) or {}

    def set_web_user_allowed(self, user_id: int, is_allowed: bool) -> Optional[Dict[str, Any]]:
        self.conn.execute(
            "UPDATE web_users SET is_allowed = ? WHERE id = ?",
            (1 if is_allowed else 0, user_id),
        )
        self.conn.commit()
        return self.get_web_user_by_id(user_id)

    def touch_web_user(self, user_id: int) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE web_users SET last_seen_at = ? WHERE id = ?",
            (now, user_id),
        )
        self.conn.commit()

    def delete_web_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM web_users WHERE id = ?", (user_id,))
        self.conn.commit()

    # --- UTP+class Student Profiles ---
    def list_utp_profiles(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, username, label, student_name, student_id, is_connected, is_active, last_sync_at, created_at FROM utp_profiles ORDER BY is_active DESC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_utp_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM utp_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_active_utp_profile(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM utp_profiles WHERE is_active = 1 AND is_connected = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT * FROM utp_profiles WHERE is_connected = 1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def upsert_utp_profile(
        self,
        username: str,
        password: str,
        label: str = "",
        is_connected: bool = True,
        make_active: bool = True,
    ) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        username = username.strip()
        label = label.strip() or f"Perfil {username}"
        if make_active:
            self.conn.execute("UPDATE utp_profiles SET is_active = 0")
        self.conn.execute(
            """
            INSERT INTO utp_profiles (username, password, label, is_connected, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password = CASE WHEN excluded.password != '' THEN excluded.password ELSE utp_profiles.password END,
                label = excluded.label,
                is_connected = excluded.is_connected,
                is_active = excluded.is_active
            """,
            (username, password, label, 1 if is_connected else 0, 1 if make_active else 0, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM utp_profiles WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else {}

    def set_utp_profile_connected(
        self, profile_id: int, is_connected: bool, make_active: Optional[bool] = None
    ) -> Optional[Dict[str, Any]]:
        if make_active:
            self.conn.execute("UPDATE utp_profiles SET is_active = 0")
            self.conn.execute(
                "UPDATE utp_profiles SET is_connected = ?, is_active = 1 WHERE id = ?",
                (1 if is_connected else 0, profile_id),
            )
        else:
            self.conn.execute(
                "UPDATE utp_profiles SET is_connected = ?, is_active = CASE WHEN ? = 0 THEN 0 ELSE is_active END WHERE id = ?",
                (1 if is_connected else 0, 1 if is_connected else 0, profile_id),
            )
            # Ensure at least one connected profile is active if available
            active = self.conn.execute(
                "SELECT id FROM utp_profiles WHERE is_active = 1 AND is_connected = 1"
            ).fetchone()
            if not active:
                first_conn = self.conn.execute(
                    "SELECT id FROM utp_profiles WHERE is_connected = 1 ORDER BY id ASC LIMIT 1"
                ).fetchone()
                if first_conn:
                    self.conn.execute(
                        "UPDATE utp_profiles SET is_active = 1 WHERE id = ?",
                        (first_conn["id"],),
                    )
        self.conn.commit()
        return self.get_utp_profile(profile_id)

    def update_utp_profile_session(
        self, profile_id: int, session_data: Dict[str, Any]
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        st_name = session_data.get("name", "")
        st_id = session_data.get("student_id", "")
        self.conn.execute(
            """
            UPDATE utp_profiles
            SET session_json = ?,
                student_name = CASE WHEN ? != '' THEN ? ELSE student_name END,
                student_id = CASE WHEN ? != '' THEN ? ELSE student_id END,
                last_sync_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(session_data, ensure_ascii=False),
                st_name,
                st_name,
                st_id,
                st_id,
                now,
                profile_id,
            ),
        )
        self.conn.commit()

    def delete_utp_profile(self, profile_id: int) -> None:
        self.conn.execute("DELETE FROM utp_profiles WHERE id = ?", (profile_id,))
        self.conn.commit()

    # --- Google Drive Files Index ---
    def replace_gdrive_files(self, files: List[Dict[str, Any]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM gdrive_files")
        for f in files:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO gdrive_files
                (file_id, name, folder_path, course_folder, mime_type, size_kb, web_view_link, download_link, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f.get("file_id", ""),
                    f.get("name", ""),
                    f.get("folder_path", ""),
                    f.get("course_folder", ""),
                    f.get("mime_type", ""),
                    float(f.get("size_kb", 0)),
                    f.get("web_view_link", ""),
                    f.get("download_link", ""),
                    now,
                ),
            )
        self.conn.commit()

    def upsert_gdrive_file(self, f: Dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO gdrive_files
            (file_id, name, folder_path, course_folder, mime_type, size_kb, web_view_link, download_link, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f.get("file_id", ""),
                f.get("name", ""),
                f.get("folder_path", ""),
                f.get("course_folder", ""),
                f.get("mime_type", ""),
                float(f.get("size_kb", 0)),
                f.get("web_view_link", ""),
                f.get("download_link", ""),
                now,
            ),
        )
        self.conn.commit()

    def list_gdrive_files(self, course_folder: str = "") -> List[Dict[str, Any]]:
        if course_folder:
            rows = self.conn.execute(
                "SELECT * FROM gdrive_files WHERE lower(course_folder) = lower(?) ORDER BY folder_path, name",
                (course_folder,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM gdrive_files ORDER BY course_folder, folder_path, name"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Calendar & Reminders ---
    def list_reminders(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reminders ORDER BY event_date ASC, event_time ASC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_reminder(
        self,
        title: str,
        event_date: str,
        event_time: str = "09:00",
        course_name: str = "",
        category: str = "recordatorio",
        recurrence: str = "none",
        notes: str = "",
        created_by: str = "admin",
    ) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO reminders
            (title, course_name, category, event_date, event_time, recurrence, notes, completed, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                title.strip(),
                course_name.strip(),
                category.strip() or "recordatorio",
                event_date.strip(),
                event_time.strip() or "09:00",
                recurrence.strip() or "none",
                notes.strip(),
                created_by,
                now,
            ),
        )
        self.conn.commit()
        rid = cur.lastrowid
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else {}

    def toggle_reminder(self, reminder_id: int, completed: bool) -> Optional[Dict[str, Any]]:
        self.conn.execute(
            "UPDATE reminders SET completed = ? WHERE id = ?",
            (1 if completed else 0, reminder_id),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None

    def delete_reminder(self, reminder_id: int) -> None:
        self.conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        self.conn.commit()

    # --- Existing Downloader State Tracking ---
    def is_file_downloaded(self, file_key: str, etag: Optional[str] = None) -> bool:
        row = self.conn.execute(
            "SELECT etag, local_path FROM downloaded_files WHERE file_key = ?",
            (file_key,),
        ).fetchone()
        if not row:
            return False
        if etag and row["etag"] and row["etag"] != etag:
            return False
        return Path(row["local_path"]).exists()

    def mark_file_downloaded(
        self, file_key: str, url: str, local_path: str, etag: Optional[str] = None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO downloaded_files (file_key, url, local_path, etag, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_key) DO UPDATE SET
                url = excluded.url,
                local_path = excluded.local_path,
                etag = excluded.etag,
                downloaded_at = excluded.downloaded_at
            """,
            (file_key, url, local_path, etag, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def upsert_activity(
        self,
        activity_id: str,
        course_name: str,
        title: str,
        act_type: str,
        finish_at: Optional[str],
        status: Optional[str],
    ) -> sqlite3.Row:
        now = datetime.now().isoformat(timespec="seconds")
        existing = self.conn.execute(
            "SELECT * FROM seen_activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        if not existing:
            self.conn.execute(
                """
                INSERT INTO seen_activities
                (activity_id, course_name, title, type, finish_at, status, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (activity_id, course_name, title, act_type, finish_at, status, now),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM seen_activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()

        self.conn.execute(
            """
            UPDATE seen_activities
            SET course_name = ?, title = ?, type = ?, finish_at = ?, status = ?
            WHERE activity_id = ?
            """,
            (course_name, title, act_type, finish_at, status, activity_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM seen_activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()

    def mark_activity_notified(self, activity_id: str, field: str) -> None:
        if field not in ("notified_new", "notified_24h", "notified_3h"):
            raise ValueError(f"Invalid field: {field}")
        self.conn.execute(
            f"UPDATE seen_activities SET {field} = 1 WHERE activity_id = ?",
            (activity_id,),
        )
        self.conn.commit()

    def is_announcement_seen(self, announcement_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_announcements WHERE announcement_id = ?",
            (announcement_id,),
        ).fetchone()
        return row is not None

    def mark_announcement_seen(
        self, announcement_id: str, course_name: str, title: str, publish_at: Optional[str]
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_announcements
            (announcement_id, course_name, title, publish_at, notified_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                announcement_id,
                course_name,
                title,
                publish_at,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def is_theme_seen(self, theme_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_themes WHERE theme_id = ?", (theme_id,)
        ).fetchone()
        return row is not None

    def mark_theme_seen(
        self, theme_id: str, course_name: str, week_number: Optional[int], name: str
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_themes
            (theme_id, course_name, week_number, name, notified_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                theme_id,
                course_name,
                week_number,
                name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

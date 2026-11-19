import secrets
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..storage import StateDB
from .events import EventBus


class SessionRegistry:
    """
    Single Responsibility service managing authenticated web sessions,
    live user presence heartbeats, and instant Admin connect/disconnect enforcement.
    """

    def __init__(self, bus: EventBus) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._bus = bus

    def create_session(self, user: Dict[str, Any], client_ip: str) -> str:
        token = secrets.token_hex(20)
        now_str = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._sessions[token] = {
                "token": token,
                "user_id": user["id"],
                "username": user["username"],
                "name": user["name"],
                "role": user["role"],
                "avatar_color": user.get("avatar_color") or "#e11d48",
                "ip": client_ip,
                "connected_at": now_str,
                "last_seen": now_str,
                "last_seen_ts": time.time(),
                "current_view": "dashboard",
            }
        return token

    def remove_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def remove_user_sessions(self, user_id: int) -> None:
        with self._lock:
            for tok, sess in list(self._sessions.items()):
                if sess.get("user_id") == user_id:
                    self._sessions.pop(tok, None)

    def mark_user_allowed(self, user_id: int, is_allowed: bool) -> None:
        with self._lock:
            for sess in self._sessions.values():
                if sess.get("user_id") == user_id:
                    sess["is_allowed"] = is_allowed

    def update_ping(self, token: str, view_name: str) -> Optional[int]:
        with self._lock:
            sess = self._sessions.get(token)
            if not sess:
                return None
            sess["last_seen_ts"] = time.time()
            sess["last_seen"] = datetime.now().strftime("%H:%M:%S")
            if view_name:
                sess["current_view"] = view_name
            return sess.get("user_id")

    def verify(self, token: str, db: StateDB) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            sess = self._sessions.get(token)
        if not sess:
            return None
        user = db.get_web_user_by_id(sess["user_id"])
        if not user:
            return None
        with self._lock:
            sess["last_seen_ts"] = time.time()
            sess["last_seen"] = datetime.now().strftime("%H:%M:%S")
            sess["is_allowed"] = bool(user["is_allowed"])
            sess["role"] = user["role"]
            sess["name"] = user["name"]
        return {
            "token": token,
            "user": user,
            "is_allowed": bool(user["is_allowed"]),
        }

    def snapshot(self, db: Optional[StateDB] = None) -> Dict[str, Any]:
        close_db = False
        if db is None:
            db = StateDB()
            close_db = True
        try:
            users = db.list_web_users()
            utp_profiles = db.list_utp_profiles()
            now_ts = time.time()
            active_sse_tokens = self._bus.active_tokens()

            with self._lock:
                online_by_uid: Dict[int, List[Dict[str, Any]]] = {}
                for tok, sess in list(self._sessions.items()):
                    is_live_sse = tok in active_sse_tokens
                    if not is_live_sse and now_ts - sess.get("last_seen_ts", 0) > 120:
                        continue
                    uid = sess.get("user_id")
                    if uid:
                        online_by_uid.setdefault(uid, []).append(
                            {
                                "token_prefix": tok[:8],
                                "ip": sess.get("ip", ""),
                                "current_view": sess.get("current_view", "dashboard"),
                                "connected_at": sess.get("connected_at", ""),
                                "last_seen": sess.get("last_seen", ""),
                                "live_sse": is_live_sse,
                            }
                        )

            enriched_users = []
            online_count = 0
            for u in users:
                uid = u["id"]
                u_sessions = online_by_uid.get(uid, [])
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

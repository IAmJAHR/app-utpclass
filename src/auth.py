import base64
import json
import re
import time
from typing import Any, Dict, Optional

import requests
from playwright.sync_api import sync_playwright

from .config import (
    CLASS_URL,
    CLIENT_ID,
    HEADLESS,
    SESSION_PATH,
    TOKEN_URL,
    UTP_PASSWORD,
    UTP_USERNAME,
    ensure_dirs,
)
from .storage import StateDB


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _save_session(
    token_data: Dict[str, Any],
    student_id: str = "",
    tenant_id: str = "",
    username_fallback: str = "",
    profile_id: Optional[int] = None,
) -> Dict[str, Any]:
    ensure_dirs()
    access_token = token_data["access_token"]
    claims = _decode_jwt_payload(access_token)
    expires_in = int(token_data.get("expires_in", 300))
    session = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": int(time.time()) + expires_in,
        "student_id": student_id or claims.get("sub", ""),
        "tenant_id": tenant_id or "a5f469d2-3c0e-5c68-8d32-5265923a8e40",
        "username": claims.get("preferred_username", username_fallback or UTP_USERNAME),
        "name": claims.get("name", ""),
    }
    SESSION_PATH.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    if profile_id is not None:
        db = StateDB()
        try:
            db.update_utp_profile_session(profile_id, session)
        finally:
            db.close()
    return session


def _refresh_access_token(
    refresh_token: str,
    student_id: str = "",
    tenant_id: str = "",
    username_fallback: str = "",
    profile_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if not refresh_token:
        return None
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "access_token" in data:
                return _save_session(
                    data,
                    student_id=student_id,
                    tenant_id=tenant_id,
                    username_fallback=username_fallback,
                    profile_id=profile_id,
                )
    except Exception:
        pass
    return None


def login_with_playwright(
    username: Optional[str] = None,
    password: Optional[str] = None,
    profile_id: Optional[int] = None,
) -> Dict[str, Any]:
    user = (username or UTP_USERNAME).strip()
    pwd = (password or UTP_PASSWORD).strip()
    if not user or not pwd:
        raise RuntimeError(
            "Faltan credenciales UTP+class (usuario o contraseña)."
        )

    captured_token: Dict[str, Any] = {}
    captured_meta: Dict[str, str] = {}
    student_re = re.compile(r"api-pao\.utpxpedition\.com/(?:learning|course)/student/([0-9a-f-]{36})/")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="es-PE",
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        def on_request(req):
            if "api-pao.utpxpedition.com" in req.url:
                hdrs = req.headers
                if hdrs.get("x-tenant-id") and not captured_meta.get("tenant_id"):
                    captured_meta["tenant_id"] = hdrs["x-tenant-id"]
                if hdrs.get("user-id") and not captured_meta.get("student_id"):
                    captured_meta["student_id"] = hdrs["user-id"]

        def on_response(resp):
            if TOKEN_URL in resp.url and resp.request.method == "POST" and resp.status == 200:
                try:
                    data = resp.json()
                    if "access_token" in data:
                        captured_token.update(data)
                except Exception:
                    pass
            m = student_re.search(resp.url)
            if m and not captured_meta.get("student_id"):
                captured_meta["student_id"] = m.group(1)

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(CLASS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_url("**/auth.utp.edu.pe/**", timeout=25000)

        page.locator("input#username, input[name='username']").first.fill(user)
        page.locator("input#password, input[name='password']").first.fill(pwd)
        page.locator("input#kc-login, button[type='submit'], input[type='submit']").first.click()

        page.wait_for_url("https://class.utp.edu.pe/**", timeout=30000)
        for _ in range(40):
            if "access_token" in captured_token and "student_id" in captured_meta:
                break
            page.wait_for_timeout(500)

        browser.close()

    if "access_token" not in captured_token:
        raise RuntimeError(
            "No se pudo capturar el token de acceso tras iniciar sesión. "
            "Verifica las credenciales del perfil UTP+class."
        )
    return _save_session(
        captured_token,
        student_id=captured_meta.get("student_id", ""),
        tenant_id=captured_meta.get("tenant_id", ""),
        username_fallback=user,
        profile_id=profile_id,
    )


def get_session(force_relogin: bool = False, profile_id: Optional[int] = None) -> Dict[str, Any]:
    db = StateDB()
    profile = None
    try:
        if profile_id is not None:
            profile = db.get_utp_profile(profile_id)
        else:
            profile = db.get_active_utp_profile()
    finally:
        db.close()

    user = profile["username"] if profile else UTP_USERNAME
    pwd = profile["password"] if profile else UTP_PASSWORD
    pid = profile["id"] if profile else None

    if profile and not profile.get("is_connected", 1):
        raise RuntimeError("El perfil UTP+class está desconectado por el Administrador.")

    # Check stored session inside profile or SESSION_PATH
    candidate_sessions = []
    if profile and profile.get("session_json"):
        try:
            candidate_sessions.append(json.loads(profile["session_json"]))
        except Exception:
            pass
    if SESSION_PATH.exists():
        try:
            s = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            if not user or str(s.get("username", "")).lower() == user.lower():
                candidate_sessions.append(s)
        except Exception:
            pass

    if not force_relogin:
        for session in candidate_sessions:
            if (
                session.get("expires_at", 0) > time.time() + 60
                and session.get("access_token")
                and session.get("student_id")
            ):
                return session
            refreshed = _refresh_access_token(
                session.get("refresh_token", ""),
                student_id=session.get("student_id", ""),
                tenant_id=session.get("tenant_id", ""),
                username_fallback=user,
                profile_id=pid,
            )
            if refreshed and refreshed.get("student_id"):
                return refreshed

    return login_with_playwright(username=user, password=pwd, profile_id=pid)

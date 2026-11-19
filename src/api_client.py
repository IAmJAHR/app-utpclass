import uuid
from typing import Any, Dict, List, Optional
import requests

from .auth import get_session
from .config import API_BASE


class UTPClassClient:
    def __init__(self) -> None:
        self.session_info = get_session()
        self.http = requests.Session()
        self.http.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://class.utp.edu.pe",
                "Referer": "https://class.utp.edu.pe/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )

    @property
    def student_id(self) -> str:
        return self.session_info["student_id"]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.session_info['access_token']}",
            "user-role": "STUDENT",
            "user-role-to-access": "",
            "user-id-to-access": "",
            "user-id": self.student_id,
            "x-tenant-id": self.session_info.get(
                "tenant_id", "a5f469d2-3c0e-5c68-8d32-5265923a8e40"
            ),
            "transaction-id": str(uuid.uuid4()),
        }

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{API_BASE}{path}"
        resp = self.http.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 401:
            self.session_info = get_session(force_relogin=True)
            resp = self.http.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get_academic_periods(self) -> List[Dict[str, Any]]:
        return self.get(f"/course/student/{self.student_id}/academicperiods") or []

    def get_dashboard_courses(self, only_current: bool = True) -> List[Dict[str, Any]]:
        courses = self.get(f"/learning/student/{self.student_id}/dashboard-courses") or []
        if not only_current or not courses:
            return courses
        numeric_periods = sorted(
            {
                str(c.get("period"))
                for c in courses
                if str(c.get("period", "")).isdigit() and str(c.get("period")) != "9999"
            },
            reverse=True,
        )
        if numeric_periods:
            current_codes = {numeric_periods[0], "9999"}
            return [c for c in courses if str(c.get("period")) in current_codes]
        return courses

    def get_pending_activities(self) -> List[Dict[str, Any]]:
        return self.get("/course/student/activities/pending/resume") or []

    def get_course_full(self, course_id: str, section_id: str) -> Dict[str, Any]:
        return self.get(
            f"/course/student/courses/{course_id}/sections/{section_id}/full"
        ) or {}

    def get_course_homework(self, course_id: str, section_id: str) -> Dict[str, Any]:
        return self.get(
            f"/course/student/courses/{course_id}/sections/{section_id}/contents/homework/"
        ) or {}

    def get_course_evaluations(self, course_id: str, section_id: str) -> Dict[str, Any]:
        return self.get(
            f"/course/student/courses/{course_id}/sections/{section_id}/contents/evaluation/"
        ) or {}

    def get_course_syllabus(self, course_id: str, section_id: str) -> Dict[str, Any]:
        return self.get(
            f"/course/student/courses/{course_id}/sections/{section_id}/syllabus"
        ) or {}

    def get_course_announcements(self, course_id: str, section_id: str) -> List[Dict[str, Any]]:
        return self.get(
            f"/communication/student/announcement/reader/{self.student_id}",
            params={"course": course_id, "sectionId": section_id},
        ) or []

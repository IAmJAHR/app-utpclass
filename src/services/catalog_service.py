from typing import Any, Dict, List

from ..gdrive import parse_course_detail_from_local, parse_local_catalog
from ..storage import StateDB


class CatalogService:
    """
    Facade & Strategy Service providing separated domain projections:
    - Courses & Weekly Syllabus
    - Global Homework, Evaluations & Grades
    - Global Teacher Announcements Feed
    """

    @staticmethod
    def get_global_feed(db: StateDB) -> Dict[str, Any]:
        """
        Aggregates all evaluations/homework and announcements across all courses
        so each module in the Sidebar has its own dedicated, decoupled dataset.
        """
        cat = parse_local_catalog(db)
        courses = cat["courses"]
        pending_tasks = cat["tasks"]

        all_evaluations: List[Dict[str, Any]] = []
        all_announcements: List[Dict[str, Any]] = []

        for c in courses:
            det = parse_course_detail_from_local(c["name"], c["classNumber"], db)
            for tk in det.get("tasks", []):
                all_evaluations.append(
                    {
                        **tk,
                        "courseName": c["name"],
                        "classNumber": c["classNumber"],
                        "teacher": c["teacher"],
                    }
                )
            for ann in det.get("announcements", []):
                all_announcements.append(
                    {
                        **ann,
                        "courseName": c["name"],
                        "classNumber": c["classNumber"],
                        "teacher": c["teacher"],
                    }
                )

        # Sort announcements newest first
        all_announcements.sort(
            key=lambda a: str(a.get("publishAt") or ""),
            reverse=True,
        )

        return {
            "courses": courses,
            "pending_tasks": pending_tasks,
            "all_evaluations": all_evaluations,
            "all_announcements": all_announcements,
        }

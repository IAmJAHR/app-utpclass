from typing import Any, Dict, List

from ..gdrive import parse_local_catalog
from ..local_ai import build_gcal_url
from ..storage import StateDB


class ICalendarBuilder:
    """
    Builder Pattern for constructing RFC-5545 iCalendar (.ics) files.
    """

    def __init__(self, calendar_name: str = "UTP+ Clases y Entregas") -> None:
        self._lines: List[str] = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//UTP+ Cloud Architecture//ES",
            "CALSCALE:GREGORIAN",
            f"X-WR-CALNAME:{calendar_name}",
        ]

    def add_event(
        self,
        uid: str,
        date_str: str,
        time_str: str,
        summary: str,
        description: str = "",
    ) -> "ICalendarBuilder":
        d_clean = date_str.replace("-", "")[:8]
        t_clean = (time_str or "09:00").replace(":", "")[:4] + "00"
        self._lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@utpcloud",
                f"DTSTART:{d_clean}T{t_clean}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                "END:VEVENT",
            ]
        )
        return self

    def build(self) -> bytes:
        return ("\r\n".join(self._lines + ["END:VCALENDAR"])).encode("utf-8")


class CalendarService:
    """
    Application service managing custom reminders, recurring class schedules,
    UTP+class deadlines, and iCalendar exports.
    """

    @staticmethod
    def get_calendar_payload(db: StateDB) -> Dict[str, Any]:
        reminders = db.list_reminders()
        for r in reminders:
            r["gcal_url"] = build_gcal_url(
                r["title"],
                r["event_date"],
                r["event_time"],
                f"Curso: {r.get('course_name', '')} | {r.get('notes', '')}",
            )
        cat = parse_local_catalog(db)
        deadlines: List[Dict[str, Any]] = []
        for t in cat["tasks"]:
            fin = (t.get("finishAt") or "").strip()
            if len(fin) >= 10 and fin[0].isdigit():
                d_str = fin[:10]
                t_str = fin[11:16] if len(fin) >= 16 else "23:59"
                deadlines.append(
                    {
                        "id": f"utp-{t.get('activityTitle','')}",
                        "title": f"[{t.get('type','TAREA')}] {t.get('activityTitle','')}",
                        "course_name": t.get("courseName", ""),
                        "category": "tarea",
                        "event_date": d_str,
                        "event_time": t_str,
                        "source": "utp",
                        "gcal_url": build_gcal_url(
                            f"[{t.get('type','TAREA')}] {t.get('activityTitle','')}",
                            d_str,
                            t_str,
                            f"Curso UTP+: {t.get('courseName', '')}",
                        ),
                    }
                )
        return {
            "reminders": reminders,
            "deadlines": deadlines,
        }

    @staticmethod
    def export_ics(db: StateDB) -> bytes:
        builder = ICalendarBuilder()
        for r in db.list_reminders():
            builder.add_event(
                uid=f"rem-{r['id']}",
                date_str=r["event_date"],
                time_str=r["event_time"],
                summary=f"{r['title']} ({r.get('course_name', 'UTP+')})",
                description=r.get("notes", ""),
            )
        cat = parse_local_catalog(db)
        for idx, t in enumerate(cat["tasks"]):
            fin = (t.get("finishAt") or "").strip()
            if len(fin) >= 10 and fin[0].isdigit():
                builder.add_event(
                    uid=f"utptask-{idx}",
                    date_str=fin[:10],
                    time_str=fin[11:16] if len(fin) >= 16 else "23:59",
                    summary=f"[{t.get('type','UTP')}] {t.get('activityTitle','')} - {t.get('courseName','')}",
                )
        return builder.build()

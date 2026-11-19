import html
import re
import unicodedata
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DOWNLOAD_DIR
from .gdrive import parse_course_detail_from_local, parse_local_catalog
from .storage import StateDB

STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "a", "en",
    "con", "por", "para", "sin", "sobre", "entre", "hasta", "desde", "que", "qué", "cual",
    "cuál", "como", "cómo", "donde", "dónde", "cuando", "cuándo", "quien", "quién", "me",
    "mi", "mis", "tu", "tus", "su", "sus", "se", "lo", "le", "les", "nos", "es", "son",
    "fue", "paso", "pasó", "hizo", "hicimos", "hay", "tengo", "tiene", "dime", "diga",
    "decir", "puedes", "quiero", "saber", "ver", "mostrar", "muestrame", "muéstrame",
    "hola", "porfa", "xfa", "porfavor", "favor", "curso", "cursos", "utp", "clase", "clases",
}


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def strip_html_clean(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<script[^>]*>.*?</script>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>|</p>|</li>|</h[1-6]>|</div>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines if ln and len(ln) > 2]
    return "\n".join(lines)


def build_gcal_url(title: str, date_str: str, time_str: str = "09:00", details: str = "") -> str:
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now() + timedelta(days=1)
    dt_end = dt + timedelta(hours=1)
    dates_param = f"{dt.strftime('%Y%m%dT%H%M00')}/{dt_end.strftime('%Y%m%dT%H%M00')}"
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": dates_param,
        "details": details or "Recordatorio creado desde UTP+ Cloud Panel",
    }
    return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"


def match_course(query_norm: str, courses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best_course = None
    best_score = 0
    q_tokens = [t for t in re.findall(r"[a-z0-9]+", query_norm) if len(t) > 2 and t not in STOPWORDS]

    for c in courses:
        cname_norm = normalize_text(c.get("name", ""))
        cnum = str(c.get("classNumber", "")).lower()
        if cnum and cnum in query_norm:
            return c
        score = 0
        for tok in q_tokens:
            if tok in cname_norm:
                score += len(tok) * 2
        if score > best_score:
            best_score = score
            best_course = c

    return best_course if best_score >= 5 else None


def parse_natural_reminder(query: str, courses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    q_norm = normalize_text(query)
    reminder_triggers = (
        "recuerdame",
        "recordatorio",
        "agendar",
        "agenda ",
        "agregar al calendario",
        "agrega al calendario",
        "anota en el calendario",
        "programar clase",
        "agregar clase",
    )
    if not any(tr in q_norm for tr in reminder_triggers):
        return None

    now = datetime.now()
    target_date = now.date()

    # Date extraction
    m_iso = re.search(r"\b(202\d-\d{2}-\d{2})\b", q_norm)
    m_dmy = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(202\d))?\b", q_norm)
    if m_iso:
        try:
            target_date = datetime.strptime(m_iso.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    elif m_dmy:
        day, month = int(m_dmy.group(1)), int(m_dmy.group(2))
        year = int(m_dmy.group(3)) if m_dmy.group(3) else now.year
        try:
            target_date = datetime(year, month, day).date()
        except ValueError:
            pass
    elif "pasado manana" in q_norm:
        target_date = (now + timedelta(days=2)).date()
    elif "manana" in q_norm:
        target_date = (now + timedelta(days=1)).date()
    else:
        weekdays = {
            "lunes": 0,
            "martes": 1,
            "miercoles": 2,
            "jueves": 3,
            "viernes": 4,
            "sabado": 5,
            "domingo": 6,
        }
        for wname, widx in weekdays.items():
            if f" {wname}" in f" {q_norm}":
                days_ahead = (widx - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                target_date = (now + timedelta(days=days_ahead)).date()
                break

    # Time extraction (e.g. "18:30", "6pm", "10 am", "a las 19")
    target_time = "09:00"
    m_time = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", q_norm)
    m_24h = re.search(r"\b(?:a las\s+)?(\d{1,2}):(\d{2})\b", q_norm)
    m_hour_only = re.search(r"\ba las\s+(\d{1,2})\b", q_norm)
    if m_time:
        hh = int(m_time.group(1))
        mm = int(m_time.group(2) or 0)
        ampm = m_time.group(3)
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        target_time = f"{hh:02d}:{mm:02d}"
    elif m_24h:
        hh = min(23, int(m_24h.group(1)))
        mm = min(59, int(m_24h.group(2)))
        target_time = f"{hh:02d}:{mm:02d}"
    elif m_hour_only:
        hh = min(23, int(m_hour_only.group(1)))
        target_time = f"{hh:02d}:00"

    matched_course = match_course(q_norm, courses)
    course_name = matched_course["name"] if matched_course else "General UTP+"

    category = "recordatorio"
    if "examen" in q_norm or "practica" in q_norm or "evaluacion" in q_norm:
        category = "examen"
    elif "clase" in q_norm or "sesion" in q_norm or "zoom" in q_norm:
        category = "clase"
    elif "tarea" in q_norm or "avance" in q_norm or "entrega" in q_norm:
        category = "tarea"

    # Clean title
    clean_title = re.sub(
        r"^(?:por favor\s+|hola\s+)?(?:agrega(?:r)?|crea(?:r)?|programa(?:r)?|agenda(?:r)?|anota(?:r)?|recuerdame|recuérdame)\s+(?:un\s+|una\s+)?(?:recordatorio\s+|clase\s+|evento\s+)?(?:para\s+|de\s+|sobre\s+|que\s+)?",
        "",
        query.strip(),
        flags=re.I,
    )
    clean_title = re.sub(r"\b(?:hoy|mañana|manana|pasado mañana)\b", "", clean_title, flags=re.I).strip(" ,.-")
    if not clean_title:
        clean_title = f"Estudiar / Revisar {course_name}"

    return {
        "title": clean_title[:120],
        "course_name": course_name,
        "category": category,
        "event_date": target_date.strftime("%Y-%m-%d"),
        "event_time": target_time,
        "recurrence": "weekly" if "cada semana" in q_norm or "semanal" in q_norm else "none",
    }


def answer_question(query: str, db: StateDB, username: str = "admin") -> Dict[str, Any]:
    """
    Local University AI Assistant: answers questions about courses, specific weeks,
    files, announcements, deadlines, teachers, and creates calendar reminders.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return {
            "answer_html": "<p>Escribe una pregunta sobre tus cursos, semanas, tareas o pídele que agende un recordatorio.</p>",
            "suggestions": [
                "¿Qué pasó en la semana 14?",
                "¿Qué tareas pendientes tengo?",
                "Últimos anuncios de mis profesores",
                "Recuérdame mañana a las 6pm estudiar Seguridad Informática",
            ],
        }

    q_norm = normalize_text(q_raw)
    catalog = parse_local_catalog(db)
    courses = catalog["courses"]
    pending_tasks = catalog["tasks"]
    matched_course = match_course(q_norm, courses)

    # 1. Check if the user wants to create a Reminder / Calendar Event
    rem_intent = parse_natural_reminder(q_raw, courses)
    if rem_intent:
        created = db.add_reminder(
            title=rem_intent["title"],
            event_date=rem_intent["event_date"],
            event_time=rem_intent["event_time"],
            course_name=rem_intent["course_name"],
            category=rem_intent["category"],
            recurrence=rem_intent["recurrence"],
            notes=f"Creado por IA Local desde: '{q_raw}'",
            created_by=username,
        )
        gcal_url = build_gcal_url(
            created["title"],
            created["event_date"],
            created["event_time"],
            f"Curso: {created['course_name']}",
        )
        return {
            "answer_html": f"""
                <div style="margin-bottom:8px;">✅ <strong>¡Recordatorio agendado en tu Calendario!</strong></div>
                <div style="background:rgba(16,185,129,0.12); border:1px solid rgba(16,185,129,0.35); padding:10px 12px; border-radius:8px; margin-bottom:10px;">
                  <div>📌 <strong>{html.escape(created['title'])}</strong></div>
                  <div style="font-size:12px; opacity:0.85; margin-top:4px;">
                    🎓 Curso: <strong>{html.escape(created['course_name'])}</strong> ·
                    📅 Fecha: <strong>{created['event_date']}</strong> a las <strong>{created['event_time']}</strong>
                  </div>
                </div>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                  <a class="btn btn-drive btn-sm" href="{gcal_url}" target="_blank">📅 Añadir a Google Calendar</a>
                  <button class="btn-sm" onclick="switchView('calendar')">Ver en mi Calendario UTP+</button>
                </div>
            """,
            "reminder_created": created,
            "suggestions": [
                "Ver mis recordatorios y tareas pendientes",
                "¿Qué pasó en la semana 15?",
                "Últimos anuncios de los docentes",
            ],
        }

    # 2. Check if the user is asking about a specific WEEK ("que paso en la semana 14", "semana 12", etc.)
    m_week = re.search(r"\bsemana\s*0?(\d{1,2})\b", q_norm)
    if m_week or "ultima semana" in q_norm or "semana actual" in q_norm:
        week_num = int(m_week.group(1)) if m_week else 15
        return _answer_about_week(week_num, matched_course, courses, db)

    # 3. Check if the user is asking about PENDING TASKS / DEADLINES / GRADES / REMINDERS
    if any(k in q_norm for k in ("pendiente", "tarea", "entrega", "vence", "evaluacion", "examen", "practica", "nota", "recordatorio", "agenda")):
        return _answer_about_tasks_and_reminders(matched_course, courses, pending_tasks, db)

    # 4. Check if the user is asking about ANNOUNCEMENTS ("anuncios", "que dijo el profe", "aviso", "zoom")
    if any(k in q_norm for k in ("anuncio", "aviso", "dijo el profe", "mensaje", "comunicado", "zoom", "enlace de clase")):
        return _answer_about_announcements(matched_course, courses, db, q_norm)

    # 5. Check if the user is asking about TEACHERS / COURSES / SYLLABUS
    if any(k in q_norm for k in ("profesor", "docente", "correo", "email", "silabo", "quien ensena", "quien dicta", "mis cursos")):
        return _answer_about_teachers_and_courses(matched_course, courses, db)

    # 6. Deep Semantic / Keyword Search across all course materials, HTML guides, tasks, announcements & Drive files
    return _deep_search_answer(q_raw, q_norm, matched_course, courses, db)


def _answer_about_week(
    week_num: int,
    matched_course: Optional[Dict[str, Any]],
    courses: List[Dict[str, Any]],
    db: StateDB,
) -> Dict[str, Any]:
    target_courses = [matched_course] if matched_course else courses
    blocks: List[str] = []

    for c in target_courses:
        det = parse_course_detail_from_local(c["name"], c["classNumber"], db)
        wk_obj = next((w for w in det["weeks"] if w["weekNumber"] == week_num), None)
        if not wk_obj:
            continue

        themes_html_parts: List[str] = []
        for th in wk_obj["themes"]:
            summary_snippets: List[str] = []
            files_links: List[str] = []

            for item in th["contents"]:
                if item.get("html"):
                    cleaned = strip_html_clean(item["html"])
                    # Remove duplicate title line
                    lines = [ln for ln in cleaned.splitlines() if normalize_text(ln) != normalize_text(item["title"])]
                    snippet = " ".join(lines)[:280]
                    if snippet:
                        summary_snippets.append(f"<em>{html.escape(item['title'])}:</em> {html.escape(snippet)}...")
                elif item.get("url"):
                    icon = "☁️" if "drive.google.com" in str(item["url"]) else "📄"
                    files_links.append(
                        f'<a class="btn btn-sm" style="margin:2px 4px 2px 0;" href="{item["url"]}" target="_blank">{icon} {html.escape(item["title"])}</a>'
                    )

            # Check Drive files specifically in Semana_XX for this course
            w_prefix = f"Semana_{week_num:02d}"
            for df in det.get("driveFiles", []):
                if w_prefix.lower() in df.get("folder_path", "").lower():
                    link_html = f'<a class="btn btn-drive btn-sm" style="margin:2px 4px 2px 0;" href="{df["web_view_link"]}" target="_blank">☁️ {html.escape(df["name"])}</a>'
                    if link_html not in files_links and df["name"] not in "".join(files_links):
                        files_links.append(link_html)

            snip_block = "<br/>".join(summary_snippets[:2]) if summary_snippets else ""
            files_block = "".join(files_links[:6]) if files_links else "<span style='font-size:11.5px; opacity:0.7;'>Sin archivos adjuntos</span>"

            themes_html_parts.append(
                f"""
                <div style="margin-top:6px; padding-left:8px; border-left:2px solid var(--blue);">
                  <div style="font-weight:700; font-size:12.5px;">📌 {html.escape(th['name'])}</div>
                  {f'<div style="font-size:12px; opacity:0.9; margin:4px 0;">{snip_block}</div>' if snip_block else ''}
                  <div style="margin-top:4px; display:flex; flex-wrap:wrap;">{files_block}</div>
                </div>
                """
            )

        blocks.append(
            f"""
            <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:11px 13px; margin-bottom:10px;">
              <div style="font-weight:800; font-size:13.5px; color:var(--text);">🎓 {html.escape(c['name'])} <span style="font-weight:500; font-size:11.5px; color:var(--muted);">(#{c['classNumber']} · {html.escape(c['teacher'])})</span></div>
              {''.join(themes_html_parts)}
            </div>
            """
        )

    if not blocks:
        cname_txt = f" en <strong>{html.escape(matched_course['name'])}</strong>" if matched_course else ""
        return {
            "answer_html": f"<p>No encontré materiales registrados para la <strong>Semana {week_num:02d}</strong>{cname_txt}. Prueba con otra semana (1 al 18).</p>",
            "suggestions": [
                "¿Qué pasó en la semana 14?",
                "¿Qué pasó en la semana 12?",
                "¿Qué tareas pendientes tengo?",
            ],
        }

    header = (
        f"📅 <strong>Resumen de la Semana {week_num:02d} — {html.escape(matched_course['name'])}</strong>:"
        if matched_course
        else f"📅 <strong>Lo que se vio en la Semana {week_num:02d} en tus cursos</strong>:"
    )
    return {
        "answer_html": f"<div style='margin-bottom:10px;'>{header}</div>" + "".join(blocks),
        "suggestions": [
            f"¿Qué pasó en la semana {max(1, week_num - 1)}?",
            f"Agendar recordatorio para repasar Semana {week_num:02d} mañana a las 18:00",
            "¿Qué tareas pendientes tengo?",
        ],
    }


def _answer_about_tasks_and_reminders(
    matched_course: Optional[Dict[str, Any]],
    courses: List[Dict[str, Any]],
    pending_tasks: List[Dict[str, Any]],
    db: StateDB,
) -> Dict[str, Any]:
    reminders = [r for r in db.list_reminders() if not r.get("completed")]
    html_parts: List[str] = []

    # 1. Pending UTP+class deadlines
    filtered_pending = [
        t for t in pending_tasks
        if not matched_course or normalize_text(matched_course["name"]) in normalize_text(t.get("courseName", ""))
    ]
    if filtered_pending:
        html_parts.append("<div style='font-weight:700; margin-bottom:6px;'>⏰ Actividades Pendientes en UTP+class:</div>")
        for t in filtered_pending:
            hl = t.get("hours_left")
            urg = f"🔴 {hl:.1f}h restantes" if hl and 0 < hl <= 24 else (f"🟡 {hl/24:.1f} días" if hl and hl > 24 else "📅 Programada")
            date_only = (t.get("finishAt") or "")[:10]
            time_only = (t.get("finishAt") or "")[11:16] or "23:59"
            gcal = build_gcal_url(f"[{t.get('type')}] {t.get('activityTitle')}", date_only, time_only, t.get("courseName", ""))
            html_parts.append(
                f"""
                <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:9px 12px; margin-bottom:6px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                  <div>
                    <div style="font-weight:700; font-size:13px;">{html.escape(t.get('activityTitle', ''))} <span class="badge badge-amber">{urg}</span></div>
                    <div style="font-size:11.5px; color:var(--muted);">{html.escape(t.get('courseName', ''))} · Semana {t.get('weekNumber', '-')} · Cierre: <strong>{(t.get('finishAt') or '-')[:16]}</strong></div>
                  </div>
                  <a class="btn btn-drive btn-sm" href="{gcal}" target="_blank">📅 Google Calendar</a>
                </div>
                """
            )
    else:
        html_parts.append("<div style='margin-bottom:8px;'>🎉 No tienes entregas pendientes urgentes en la agenda general.</div>")

    # 2. Custom reminders in Calendar
    if reminders:
        html_parts.append("<div style='font-weight:700; margin:10px 0 6px;'>📌 Tus Recordatorios y Clases Agendadas:</div>")
        for r in reminders[:6]:
            html_parts.append(
                f"""
                <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:8px 12px; margin-bottom:5px; font-size:12.5px;">
                  <strong>{r['event_date']} {r['event_time']}</strong> — {html.escape(r['title'])}
                  <span class="badge badge-blue">{html.escape(r['course_name'] or r['category'])}</span>
                </div>
                """
            )

    # 3. If a specific course was asked, also show its graded/registered evaluations
    if matched_course:
        det = parse_course_detail_from_local(matched_course["name"], matched_course["classNumber"], db)
        if det["tasks"]:
            html_parts.append(f"<div style='font-weight:700; margin:10px 0 6px;'>📝 Evaluaciones registradas en {html.escape(matched_course['name'])}:</div>")
            for tk in det["tasks"][:8]:
                sc = f"{tk['score']}/{tk['topScore']}" if tk.get("score") is not None else f"-/{tk.get('topScore', 20)}"
                html_parts.append(
                    f"<div style='font-size:12px; padding:5px 0; border-bottom:1px solid var(--border);'>• <strong>{html.escape(tk['title'])}</strong> ({tk['status']}) — Nota: <strong>{sc}</strong> · Cierre: {tk.get('visibleTo', '-')}</div>"
                )

    return {
        "answer_html": "".join(html_parts),
        "suggestions": [
            "Recuérdame mañana a las 17:00 avanzar las tareas pendientes",
            "¿Qué pasó en la semana 15?",
            "Últimos anuncios de mis profesores",
        ],
    }


def _answer_about_announcements(
    matched_course: Optional[Dict[str, Any]],
    courses: List[Dict[str, Any]],
    db: StateDB,
    q_norm: str,
) -> Dict[str, Any]:
    target_courses = [matched_course] if matched_course else courses
    ann_cards: List[str] = []

    for c in target_courses:
        det = parse_course_detail_from_local(c["name"], c["classNumber"], db)
        for a in det["announcements"][: (4 if matched_course else 2)]:
            snippet = (a.get("descriptionText") or "")[:240]
            ann_cards.append(
                f"""
                <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:8px;">
                  <div style="font-size:11px; color:var(--muted); display:flex; justify-content:space-between;">
                    <span>🎓 <strong>{html.escape(c['name'])}</strong> · 📣 {html.escape(a.get('author') or c['teacher'])}</span>
                    <span>{html.escape((a.get('publishAt') or '')[:16])}</span>
                  </div>
                  <div style="font-weight:700; font-size:13px; margin:4px 0;">{html.escape(a.get('title', 'Anuncio'))}</div>
                  <div style="font-size:12px; opacity:0.9; white-space:pre-line;">{html.escape(snippet)}{'...' if len(a.get('descriptionText','')) > 240 else ''}</div>
                </div>
                """
            )

    if not ann_cards:
        return {
            "answer_html": "<p>No encontré anuncios registrados para esa consulta.</p>",
            "suggestions": ["¿Qué pasó en la semana 14?", "¿Qué tareas pendientes tengo?"],
        }

    title = f"📢 <strong>Últimos anuncios en {html.escape(matched_course['name'])}</strong>:" if matched_course else "📢 <strong>Anuncios más recientes de tus docentes</strong>:"
    return {
        "answer_html": f"<div style='margin-bottom:8px;'>{title}</div>" + "".join(ann_cards[:10]),
        "suggestions": [
            "¿Qué pasó en la semana 15?",
            "¿Quiénes son mis profesores y sus correos?",
            "¿Qué tareas pendientes tengo?",
        ],
    }


def _answer_about_teachers_and_courses(
    matched_course: Optional[Dict[str, Any]],
    courses: List[Dict[str, Any]],
    db: StateDB,
) -> Dict[str, Any]:
    target = [matched_course] if matched_course else courses
    rows: List[str] = []
    for c in target:
        det = parse_course_detail_from_local(c["name"], c["classNumber"], db)
        syl_btn = f'<a class="btn btn-sm" href="{det["syllabusUrl"]}" target="_blank">📄 Sílabo</a>' if det.get("syllabusUrl") else ""
        rows.append(
            f"""
            <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:7px; display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;">
              <div>
                <div style="font-weight:700; font-size:13px;">{html.escape(c['name'])} <span style="color:var(--muted); font-size:11.5px;">(Clase #{c['classNumber']})</span></div>
                <div style="font-size:12px; color:var(--muted);">👨‍🏫 Docente: <strong style="color:var(--text);">{html.escape(c['teacher'])}</strong> {f'· ✉️ {html.escape(c["teacherEmail"])}' if c.get('teacherEmail') else ''}</div>
                <div style="font-size:11.5px; color:var(--green);">☁️ {c['downloadedCount']} archivos en Drive/Server · Avance: {c['progress']}%</div>
              </div>
              <div>{syl_btn}</div>
            </div>
            """
        )
    return {
        "answer_html": "<div style='margin-bottom:8px;'>🎓 <strong>Información de tus Cursos, Docentes y Sílabos:</strong></div>" + "".join(rows),
        "suggestions": [
            "¿Qué pasó en la semana 14?",
            "¿Qué tareas pendientes tengo?",
            "Últimos anuncios de mis profesores",
        ],
    }


def _deep_search_answer(
    q_raw: str,
    q_norm: str,
    matched_course: Optional[Dict[str, Any]],
    courses: List[Dict[str, Any]],
    db: StateDB,
) -> Dict[str, Any]:
    tokens = [t for t in re.findall(r"[a-z0-9]+", q_norm) if len(t) > 2 and t not in STOPWORDS]
    if not tokens:
        return _answer_about_teachers_and_courses(matched_course, courses, db)

    scored_hits: List[Tuple[int, str]] = []
    target_courses = [matched_course] if matched_course else courses

    for c in target_courses:
        det = parse_course_detail_from_local(c["name"], c["classNumber"], db)

        # Search in weeks themes & HTML contents
        for w in det["weeks"]:
            for th in w["themes"]:
                th_norm = normalize_text(th["name"])
                th_score = sum(4 for tok in tokens if tok in th_norm)
                for item in th["contents"]:
                    title_norm = normalize_text(item.get("title", ""))
                    body_clean = strip_html_clean(item.get("html", "")) if item.get("html") else ""
                    body_norm = normalize_text(body_clean)
                    score = th_score + sum(6 for tok in tokens if tok in title_norm) + sum(2 for tok in tokens if tok in body_norm)
                    if score > 0:
                        link = f'<a class="btn btn-sm" href="{item["url"]}" target="_blank">↗ Abrir material</a>' if item.get("url") else ""
                        excerpt = body_clean[:240] + "..." if body_clean else f"Material en {w['label']} -> {th['name']}"
                        card = f"""
                        <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:7px;">
                          <div style="font-size:11px; color:var(--muted);">🎓 {html.escape(c['name'])} · {html.escape(w['label'])} · {html.escape(th['name'])}</div>
                          <div style="font-weight:700; font-size:13px; margin:3px 0;">{html.escape(item.get('title', ''))}</div>
                          <div style="font-size:12px; opacity:0.9; margin-bottom:6px;">{html.escape(excerpt)}</div>
                          {link}
                        </div>
                        """
                        scored_hits.append((score, card))

        # Search in Tasks
        for tk in det["tasks"]:
            t_norm = normalize_text(f"{tk['title']} {tk.get('description', '')}")
            score = sum(5 for tok in tokens if tok in t_norm)
            if score > 0:
                card = f"""
                <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:7px;">
                  <div style="font-size:11px; color:var(--muted);">📝 {html.escape(c['name'])} · {tk['kind']} ({tk['status']})</div>
                  <div style="font-weight:700; font-size:13px; margin:3px 0;">{html.escape(tk['title'])}</div>
                  <div style="font-size:12px; opacity:0.9;">{html.escape((tk.get('description') or '')[:240])}</div>
                </div>
                """
                scored_hits.append((score, card))

        # Search in Drive Files
        for df in det.get("driveFiles", []):
            df_norm = normalize_text(f"{df['name']} {df.get('folder_path', '')}")
            score = sum(5 for tok in tokens if tok in df_norm)
            if score > 0:
                card = f"""
                <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; padding:9px 12px; margin-bottom:6px; display:flex; justify-content:space-between; align-items:center;">
                  <div>
                    <div style="font-weight:700; font-size:12.5px;">☁️ {html.escape(df['name'])}</div>
                    <div style="font-size:11px; color:var(--muted);">{html.escape(c['name'])} · {html.escape(df.get('folder_path', ''))}</div>
                  </div>
                  <a class="btn btn-drive btn-sm" href="{df['web_view_link']}" target="_blank">☁️ Abrir en Drive</a>
                </div>
                """
                scored_hits.append((score, card))

    scored_hits.sort(key=lambda x: x[0], reverse=True)
    if not scored_hits:
        return {
            "answer_html": f"<p>No encontré resultados exactos para <strong>\"{html.escape(q_raw)}\"</strong> en tus materiales. Prueba preguntando por una semana (ej. <em>\"¿Qué pasó en la semana 14?\"</em>), curso, anuncio o tarea.</p>",
            "suggestions": [
                "¿Qué pasó en la semana 14?",
                "¿Qué tareas pendientes tengo?",
                "Últimos anuncios de mis profesores",
                "Mis cursos y docentes",
            ],
        }

    top_cards = [card for _, card in scored_hits[:8]]
    return {
        "answer_html": f"<div style='margin-bottom:8px;'>🔍 <strong>Encontré {len(scored_hits)} resultado(s) en tus cursos de la UTP para \"{html.escape(q_raw)}\":</strong></div>" + "".join(top_cards),
        "suggestions": [
            "¿Qué pasó en la semana 15?",
            "¿Qué tareas pendientes tengo?",
            f"Recuérdame mañana a las 18:00 repasar {q_raw[:30]}",
        ],
    }
